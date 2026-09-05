#!/usr/bin/env python3
"""Tests for the credential proxy client shim.

The shim is what every `kubectl`/`gcloud`/`gh`/`git` in the agent container
actually is, so what it puts in the request body decides whether a command
reaches the right cluster - or is rejected outright.

Run:  python3 agents/platform/scripts/test_credential_proxy_client.py
"""

import base64
import io
import json
import shutil
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import credential_proxy_client


class RecordingResponse(io.BytesIO):
    """Stand-in for the urlopen context manager the client reads."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# A well-formed GKE context name, which is the only thing the broker accepts.
GKE_CONTEXT = "gke_acme-prod_us-central1_ka-cluster-a"


def write_kubeconfig(directory: Path, context: str = GKE_CONTEXT) -> Path:
    path = directory / "kubeconfig.yaml"
    path.write_text(
        f"apiVersion: v1\nkind: Config\ncurrent-context: {context}\n", encoding="utf-8"
    )
    return path


class SubmittedPayloadTestCase(unittest.TestCase):
    # The broker's Service. There is no other endpoint: it is always a Pod of
    # its own, so nothing the shim sends may name a path.
    LOCAL_ENDPOINT = "http://agent-credential-proxy.kubeagents-system.svc.cluster.local:8765"

    def send(self, argv, environ, endpoint=LOCAL_ENDPOINT, response=None):
        """Run the client against a stubbed proxy, returning the whole request.

        The stub replaces `open_broker_request` rather than `urlopen`: the
        client sends through its own opener so that the connect is bounded
        while the response is not.
        """
        captured = {}
        body = {"exitCode": 0} if response is None else response

        def fake_open(request, *args, **kwargs):
            captured["request"] = request
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return RecordingResponse(json.dumps(body).encode("utf-8"))

        with patch.dict("os.environ", environ, clear=False):
            with patch.object(credential_proxy_client, "open_broker_request", fake_open):
                with patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=io.StringIO()):
                    captured["exit_code"] = credential_proxy_client.execute(endpoint, argv)
        return captured

    def submit(self, argv, environ, endpoint=LOCAL_ENDPOINT):
        """Run the client against a stubbed proxy, returning the request body."""
        return self.send(argv, environ, endpoint)["payload"]


class TestKubeconfigResolution(SubmittedPayloadTestCase):
    """The pin crosses as a cluster name, because the file itself cannot.

    The broker is in another pod: a path sent from here names nothing there, or
    something else. So the shim reads `current-context` out of the file it can
    see and sends that, and the broker regenerates the kubeconfig from the name.
    """

    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.pinned = write_kubeconfig(directory)

    def test_kubectl_carries_the_context_and_never_the_path(self):
        payload = self.submit(["kubectl", "get", "pods"], {"KUBECONFIG": str(self.pinned)})
        self.assertEqual(payload["kubeconfigContext"], GKE_CONTEXT)
        self.assertNotIn("kubeconfig", payload)

    def test_the_flag_is_translated_too(self):
        # kubectl prefers --kubeconfig over the environment, so a flag left as a
        # path would be the door the environment no longer is.
        payload = self.submit(
            ["kubectl", "--kubeconfig", str(self.pinned), "get", "pods"], {}
        )
        self.assertEqual(payload["argv"][2], GKE_CONTEXT)
        payload = self.submit([f"kubectl", f"--kubeconfig={self.pinned}", "get", "pods"], {})
        self.assertEqual(payload["argv"][1], f"--kubeconfig={GKE_CONTEXT}")

    def test_no_cwd_is_ever_sent(self):
        # The other path-valued field, and gone for the same reason.
        payload = self.submit(["kubectl", "get", "pods"], {})
        self.assertNotIn("cwd", payload)

    def test_git_and_gh_do_not(self):
        # Neither reads KUBECONFIG, and an unreadable one is now a hard failure
        # - so resolving it here would refuse a command with nothing to do with
        # Kubernetes.
        for argv in (["git", "status"], ["gh", "pr", "list"]):
            with self.subTest(argv=argv):
                payload = self.submit(argv, {"KUBECONFIG": "/nowhere/at/all.yaml"})
                self.assertNotIn("kubeconfigContext", payload)

    def test_absent_when_unset(self):
        payload = self.submit(["kubectl", "get", "pods"], {"KUBECONFIG": ""})
        self.assertNotIn("kubeconfigContext", payload)

    def test_trailing_newline_is_stripped(self):
        # Profile .env files routinely carry one, and an unstripped value is a
        # path that does not exist.
        payload = self.submit(
            ["kubectl", "get", "pods"], {"KUBECONFIG": str(self.pinned) + "\n"}
        )
        self.assertEqual(payload["kubeconfigContext"], GKE_CONTEXT)


class TestAnUnusablePinFailsLoudly(SubmittedPayloadTestCase):
    """The alternative is a command that quietly runs against another cluster.

    Dropping an unreadable KUBECONFIG leaves the broker falling back to its own
    default cluster, which is the failure nobody notices until it has written
    something. Each of these exits 1 without sending a request at all.
    """

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)

    def assertRefused(self, kubeconfig):
        captured = self.send(["kubectl", "get", "pods"], {"KUBECONFIG": kubeconfig})
        self.assertEqual(captured["exit_code"], 1)
        self.assertNotIn("payload", captured, "no request should have been sent")

    def test_a_missing_file(self):
        self.assertRefused(str(self.directory / "absent.yaml"))

    def test_a_kubeconfig_naming_no_context(self):
        empty = self.directory / "empty.yaml"
        empty.write_text("apiVersion: v1\nkind: Config\n", encoding="utf-8")
        self.assertRefused(str(empty))

    def test_a_context_that_is_not_a_gke_name(self):
        # The broker can only regenerate a kubeconfig it can name a cluster
        # from, so anything else is refused here rather than 400ed there.
        self.assertRefused(str(write_kubeconfig(self.directory, "minikube")))

    def test_a_merged_list(self):
        # kubectl would flatten two files into one view and there is no sound
        # way to regenerate a merge.
        first = write_kubeconfig(self.directory)
        self.assertRefused(f"{first}:{first}")


class TestGetCredentialsWritesTheFileOnThisSide(SubmittedPayloadTestCase):
    """The one command that authors a kubeconfig, across a pod boundary.

    gcloud runs in the broker's pod and the destination is a path in this one,
    so the flag comes off the argv, the broker returns what gcloud wrote, and
    the shim puts it where the caller asked.
    """

    ARGV = ["gcloud", "container", "clusters", "get-credentials", "ka-cluster-a"]
    GENERATED = f"apiVersion: v1\nkind: Config\ncurrent-context: {GKE_CONTEXT}\n"

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.destination = self.directory / "profiles" / "cluster-a" / "kubeconfig.yaml"

    def run_it(self, argv, environ):
        return self.send(
            argv,
            environ,
            response={"exitCode": 0, "kubeconfig": self.GENERATED},
        )

    def test_the_flag_comes_off_and_the_file_lands_here(self):
        captured = self.run_it([*self.ARGV, "--kubeconfig", str(self.destination)], {})
        self.assertNotIn("--kubeconfig", captured["payload"]["argv"])
        self.assertTrue(captured["payload"]["wantsKubeconfig"])
        self.assertEqual(self.destination.read_text(encoding="utf-8"), self.GENERATED)

    def test_the_joined_spelling_too(self):
        captured = self.run_it([*self.ARGV, f"--kubeconfig={self.destination}"], {})
        self.assertEqual(
            [token for token in captured["payload"]["argv"] if token.startswith("--kubeconfig")],
            [],
        )
        self.assertEqual(self.destination.read_text(encoding="utf-8"), self.GENERATED)

    def test_the_environment_is_the_fallback_destination(self):
        # How a Cluster Agent scaffold pins itself: no flag, just $KUBECONFIG.
        self.run_it(self.ARGV, {"KUBECONFIG": str(self.destination)})
        self.assertEqual(self.destination.read_text(encoding="utf-8"), self.GENERATED)

    def test_no_destination_asks_for_nothing_back(self):
        captured = self.send(self.ARGV, {"KUBECONFIG": ""})
        self.assertNotIn("wantsKubeconfig", captured["payload"])


class TestContextGrammar(unittest.TestCase):
    """The grammar both sides hold the name to, tested where it now lives."""

    def test_a_gke_context_round_trips(self):
        target = credential_proxy_client.parse_gke_context(GKE_CONTEXT)
        self.assertEqual(target.project, "acme-prod")
        self.assertEqual(target.location, "us-central1")
        self.assertEqual(target.cluster, "ka-cluster-a")
        self.assertEqual(target.context_name, GKE_CONTEXT)

    def test_anything_else_is_refused(self):
        for context in (
            "minikube",
            "gke_only_three",
            "gke_proj_us-central1_cluster\nevil",
            "gke_Proj_us-central1_cluster",
            "gke__us-central1_cluster",
            "gke_../etc_us-central1_cluster",
        ):
            with self.subTest(context=context):
                self.assertIsNone(credential_proxy_client.parse_gke_context(context))


class StdinGateTest(unittest.TestCase):
    """`-f -` has never worked in any topology. These bind the narrow fix."""

    def test_recognises_an_explicit_request_for_stdin(self):
        for argv in (
            ["kubectl", "apply", "-f", "-"],
            ["kubectl", "apply", "--filename", "-"],
            ["kubectl", "apply", "--filename=-"],
            ["kubectl", "patch", "deploy/x", "--patch-file", "-"],
            ["gh", "pr", "create", "--title", "t", "--body-file", "-"],
            ["gh", "issue", "create", "--body-file=-"],
        ):
            with self.subTest(argv=argv):
                self.assertTrue(credential_proxy_client.reads_stdin(argv))

    def test_leaves_every_other_argv_alone(self):
        """The MCP protocol-stream hazard is why this list stays short."""
        for argv in (
            ["kubectl", "get", "ns"],
            ["kubectl", "apply", "-f", "manifest.yaml"],
            ["gh", "pr", "list"],
            ["git", "log", "-"],
            ["kubectl", "logs", "-f", "pod/x"],
            ["gh", "pr", "create", "--body", "-"],
        ):
            with self.subTest(argv=argv):
                self.assertFalse(credential_proxy_client.reads_stdin(argv))

    def test_a_terminal_on_fd_zero_is_not_read(self):
        """Otherwise an interactive `-f -` hangs and reads as the proxy being down."""

        class Tty(io.StringIO):
            def isatty(self):
                return True

        with patch.object(sys, "stdin", Tty("ignored")):
            self.assertIsNone(
                credential_proxy_client.read_stdin_if_requested(
                    ["kubectl", "apply", "-f", "-"]
                )
            )

    def test_a_pipe_on_fd_zero_is_forwarded(self):
        with patch.object(sys, "stdin", io.StringIO("kind: ConfigMap\n")):
            self.assertEqual(
                credential_proxy_client.read_stdin_if_requested(
                    ["kubectl", "apply", "-f", "-"]
                ),
                "kind: ConfigMap\n",
            )

    def test_stdin_reaches_the_request_body(self):
        captured = {}

        def fake_open(request, *args, **kwargs):
            captured["body"] = json.loads(request.data)
            return RecordingResponse(json.dumps({"exitCode": 0}).encode())

        with patch.object(credential_proxy_client, "open_broker_request", fake_open):
            credential_proxy_client.execute(
                "http://127.0.0.1:8765", ["kubectl", "apply", "-f", "-"], stdin="kind: X\n"
            )
        self.assertEqual(captured["body"]["stdin"], "kind: X\n")


class WorkspaceClientTest(unittest.TestCase):
    """The client half of content-passing. No path crosses this boundary."""

    def setUp(self):
        self.endpoint = "http://127.0.0.1:8765"
        self.calls = []

    def _serve(self, answers):
        # `open_broker_request`, not `urlopen`: the workspace routes go through
        # the client's own opener so that a clone that legitimately runs for
        # minutes is not cut off by a total socket timeout.
        def fake_open(request, *args, **kwargs):
            body = json.loads(request.data)
            self.calls.append((request.full_url, body))
            verb = request.full_url.rsplit("/", 1)[-1]
            return RecordingResponse(json.dumps(answers[verb]).encode())

        return patch.object(credential_proxy_client, "open_broker_request", fake_open)

    def test_open_commit_push_close(self):
        answers = {
            "open": {
                "handle": "a" * 32,
                "repo": "acme/infra",
                "base": "main",
                "baseSha": "b" * 40,
            },
            "commit": {
                "committed": True,
                "branch": "fix/x",
                "base": "main",
                "baseSha": "c" * 40,
                "commit": "d" * 40,
            },
            "push": {"pushed": True, "branch": "fix/x", "commit": "d" * 40},
            "close": {"closed": True},
        }
        with self._serve(answers):
            with credential_proxy_client.Workspace.open(
                self.endpoint, "acme/infra"
            ) as workspace:
                workspace.commit(
                    branch="fix/x",
                    message="m",
                    changes={"a.yaml": b"kind: X\n", "gone.yaml": None},
                    expected_base_sha=workspace.base_sha,
                )
                workspace.push()

        verbs = [url.rsplit("/", 1)[-1] for url, _ in self.calls]
        self.assertEqual(verbs, ["open", "commit", "push", "close"])
        commit_body = self.calls[1][1]
        self.assertEqual(commit_body["expectedBaseSha"], "b" * 40)
        entries = {entry["path"]: entry for entry in commit_body["changes"]}
        self.assertEqual(
            base64.b64decode(entries["a.yaml"]["contentBase64"]), b"kind: X\n"
        )
        self.assertTrue(entries["gone.yaml"]["delete"])
        self.assertNotIn("contentBase64", entries["gone.yaml"])

    def test_the_branch_lease_follows_the_workspace_across_rounds(self):
        """Round two must expect what round one pushed, not what `open` saw.

        The broker defaults the expectation, so the client sends nothing; what
        it owes is the tracked value, which a caller reads to decide whether
        the branch it is about to write is the one it last saw.
        """
        answers = {
            "open": {
                "handle": "a" * 32,
                "repo": "acme/infra",
                "base": "main",
                "baseSha": "b" * 40,
                "branchSha": "e" * 40,
            },
            "commit": {
                "committed": True,
                "branch": "fix/x",
                "base": "main",
                "baseSha": "c" * 40,
                "branchSha": "e" * 40,
                "commit": "d" * 40,
            },
            "push": {
                "pushed": True,
                "branch": "fix/x",
                "commit": "d" * 40,
                "branchSha": "d" * 40,
            },
            "close": {"closed": True},
        }
        with self._serve(answers):
            with credential_proxy_client.Workspace.open(
                self.endpoint, "acme/infra", branch="fix/x"
            ) as workspace:
                self.assertEqual("e" * 40, workspace.branch_sha)
                workspace.commit(
                    branch="fix/x", message="m", changes={"a.yaml": b"kind: X\n"}
                )
                self.assertNotIn("expectedBranchSha", self.calls[1][1])
                workspace.push()
                self.assertEqual("d" * 40, workspace.branch_sha)

        # A caller that learned the sha elsewhere overrides the broker's default.
        self.calls.clear()
        with self._serve(answers):
            with credential_proxy_client.Workspace.open(
                self.endpoint, "acme/infra"
            ) as workspace:
                workspace.commit(
                    branch="fix/x",
                    message="m",
                    changes={"a.yaml": b"kind: X\n"},
                    expected_branch_sha="f" * 40,
                )
        self.assertEqual("f" * 40, self.calls[1][1]["expectedBranchSha"])

    def test_a_disabled_broker_is_distinguishable_from_a_refusal(self):
        """Callers that can do either need to tell "off" from "no"."""

        def disabled(request, *args, **kwargs):
            raise urllib.error.HTTPError(
                request.full_url,
                404,
                "Not Found",
                {},
                io.BytesIO(
                    json.dumps(
                        {"error": "not enabled", "code": "CONTENT_WORKSPACES_DISABLED"}
                    ).encode()
                ),
            )

        with patch.object(credential_proxy_client, "open_broker_request", disabled):
            with self.assertRaises(credential_proxy_client.WorkspaceUnavailable):
                credential_proxy_client.Workspace.open(self.endpoint, "acme/infra")
            self.assertFalse(credential_proxy_client.workspaces_available(self.endpoint))

    def test_an_unauthenticated_caller_is_not_told_workspaces_are_armed(self):
        """401 answers about the caller, not about the route.

        The broker rejects an unauthenticated request before it looks at the
        path, so treating any non-404 as proof the feature exists reports armed
        workspaces on a broker that never reached the question. A sandbox with no
        token file did exactly that live, then failed on the first real verb.
        """

        def unauthorized(request, *args, **kwargs):
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                {},
                io.BytesIO(
                    json.dumps({"error": "caller could not be authenticated"}).encode()
                ),
            )

        with patch.object(credential_proxy_client, "open_broker_request", unauthorized):
            self.assertFalse(
                credential_proxy_client.workspaces_available(self.endpoint)
            )

    def test_a_refusal_carries_the_brokers_answer_through(self):
        def conflict(request, *args, **kwargs):
            raise urllib.error.HTTPError(
                request.full_url,
                409,
                "Conflict",
                {},
                io.BytesIO(
                    json.dumps(
                        {
                            "error": "the base branch moved",
                            "code": "BASE_MOVED",
                            "paths": ["manifests/app.yaml"],
                        }
                    ).encode()
                ),
            )

        with patch.object(credential_proxy_client, "open_broker_request", conflict):
            with self.assertRaises(
                credential_proxy_client.WorkspaceRequestError
            ) as caught:
                credential_proxy_client.Workspace.open(self.endpoint, "acme/infra")
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.payload["code"], "BASE_MOVED")
        self.assertEqual(
            caught.exception.payload["paths"], ["manifests/app.yaml"]
        )

    def test_push_before_commit_is_refused_client_side(self):
        answers = {
            "open": {
                "handle": "a" * 32,
                "repo": "acme/infra",
                "base": "main",
                "baseSha": "b" * 40,
            },
            "close": {"closed": True},
        }
        with self._serve(answers):
            workspace = credential_proxy_client.Workspace.open(
                self.endpoint, "acme/infra"
            )
            with self.assertRaises(ValueError):
                workspace.push()


class WorkspaceReadVerbsTest(unittest.TestCase):
    """The read half: what the client sends, and what it refuses to hide.

    Every verb here can answer partially -- a listing that stopped at the
    broker's ceiling, a batch that dropped a file, a search that hit its match
    cap. A client that returns only the payload and drops the "and there is
    more" flag turns each of those into a wrong conclusion about the
    repository, so each test asserts the flag survives the call.
    """

    HANDLE = "a" * 32

    def setUp(self):
        self.endpoint = "http://127.0.0.1:8765"
        self.calls = []

    def _workspace(self, answers, **opened):
        def fake_open(request, *args, **kwargs):
            body = json.loads(request.data)
            verb = request.full_url.rsplit("/", 1)[-1]
            self.calls.append((verb, body))
            answer = answers[verb]
            if isinstance(answer, list):
                answer = answer[sum(1 for call in self.calls if call[0] == verb) - 1]
            return RecordingResponse(json.dumps(answer).encode())

        answers.setdefault(
            "open",
            {
                "handle": self.HANDLE,
                "repo": "acme/infra",
                "base": "main",
                "baseSha": "b" * 40,
                **opened,
            },
        )
        patcher = patch.object(
            credential_proxy_client, "open_broker_request", fake_open
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return credential_proxy_client.Workspace.open(self.endpoint, "acme/infra")

    def test_a_shallow_open_names_its_depth_and_says_it_is_shallow(self):
        workspace = self._workspace({}, shallow=True, startedFrom="origin/main")
        self.assertTrue(workspace.shallow)
        self.assertEqual("origin/main", workspace.started_from)

        # `depth` and `branch` reach the wire only when asked for; an ordinary
        # open must stay the payload the broker has always accepted.
        self.assertEqual({"repo": "acme/infra"}, self.calls[0][1])
        credential_proxy_client.Workspace.open(
            self.endpoint, "acme/infra", branch="fix/x", depth=1
        )
        self.assertEqual(
            {"repo": "acme/infra", "branch": "fix/x", "depth": 1}, self.calls[1][1]
        )

    def test_a_batch_read_hands_back_what_it_did_not_read(self):
        workspace = self._workspace(
            {
                "read": {
                    "files": [
                        {
                            "path": "a.yaml",
                            "contentBase64": base64.b64encode(b"kind: A\n").decode(),
                        }
                    ],
                    "skipped": [{"path": "big.yaml", "reason": "tooLarge", "size": 9999}],
                }
            }
        )
        files, skipped = workspace.read_many(["a.yaml", "big.yaml"])
        self.assertEqual({"a.yaml": b"kind: A\n"}, files)
        self.assertEqual([{"path": "big.yaml", "reason": "tooLarge", "size": 9999}], skipped)
        # One round trip, on the same verb as the single read, keyed on `paths`.
        self.assertEqual(
            ("read", {"handle": self.HANDLE, "paths": ["a.yaml", "big.yaml"]}),
            self.calls[1],
        )

    def test_a_truncated_listing_says_so_and_pages_from_its_last_entry(self):
        workspace = self._workspace(
            {
                "list": [
                    {"entries": ["a/0.yaml", "a/1.yaml"], "total": 3, "truncated": True},
                    {"entries": ["a/2.yaml"], "total": 1, "truncated": False},
                ]
            }
        )
        first = workspace.list(prefix="a")
        self.assertEqual(["a/0.yaml", "a/1.yaml"], list(first))
        self.assertTrue(first.truncated)
        self.assertEqual(3, first.total)

        second = workspace.list(prefix="a", after=first[-1])
        self.assertFalse(second.truncated)
        self.assertEqual(
            {"handle": self.HANDLE, "prefix": "a", "after": "a/1.yaml"},
            self.calls[2][1],
        )

    def test_grep_returns_the_ceiling_alongside_the_matches(self):
        workspace = self._workspace(
            {
                "grep": {
                    "matches": [{"path": "a.yaml", "line": 3, "text": "nginx"}],
                    "total": 1,
                    "truncated": True,
                }
            }
        )
        result = workspace.grep("nginx")
        self.assertTrue(result["truncated"])
        # Fixed-string is the default, so neither flag is sent unless asked for.
        self.assertEqual({"handle": self.HANDLE, "pattern": "nginx"}, self.calls[1][1])

        workspace.grep("kind: (Deployment|Service)", prefix="manifests", regex=True, ignore_case=True)
        self.assertEqual(
            {
                "handle": self.HANDLE,
                "pattern": "kind: (Deployment|Service)",
                "prefix": "manifests",
                "regex": True,
                "ignoreCase": True,
            },
            self.calls[2][1],
        )


class TestCallerCredential(SubmittedPayloadTestCase):
    """The client half of the broker's authentication.

    The server-side tests prove an unauthenticated call is refused. Nothing
    proved the client attaches a valid one: deleting the
    `headers.update(authorization_headers())` line left the entire Python suite
    green while the split deployment was completely broken — every command a
    401 — and the sidecar deployment, which sends no header at all, looked
    exactly the same.
    """

    def token_file(self, contents):
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        path = Path(directory) / "token"
        path.write_text(contents, encoding="utf-8")
        return path

    def test_no_header_when_the_token_file_is_not_configured(self):
        # The sidecar deployment. The broker is on the Pod's own loopback
        # behind a socket only its container can open and asks for nothing, and
        # this is half of why the gate-off behaviour is unchanged.
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual({}, credential_proxy_client.authorization_headers())

        sent = self.send(["kubectl", "get", "pods"], {"CREDENTIAL_PROXY_TOKEN_FILE": ""})
        self.assertIsNone(sent["request"].get_header("Authorization"))

    def test_the_configured_token_is_sent_as_a_bearer_credential(self):
        path = self.token_file("a-projected-service-account-token")
        headers = self.send(
            ["kubectl", "get", "pods"], {"CREDENTIAL_PROXY_TOKEN_FILE": str(path)}
        )["request"]
        self.assertEqual(
            "Bearer a-projected-service-account-token",
            headers.get_header("Authorization"),
        )

    def test_the_projected_newline_is_stripped(self):
        # A projected token file has no trailing newline today, but a Secret or
        # a hand-written one does, and " \n" inside the header value is a
        # malformed credential rather than a rejected one.
        path = self.token_file("token-with-newline\n")
        headers = self.send(
            ["kubectl", "get", "pods"], {"CREDENTIAL_PROXY_TOKEN_FILE": str(path)}
        )["request"]
        self.assertEqual("Bearer token-with-newline", headers.get_header("Authorization"))

    def test_an_unreadable_token_file_fails_with_its_own_message(self):
        # Sending the request anyway would earn an undifferentiated 401 and
        # point the operator at the broker, when the fault is the projection.
        captured = {}

        def fake_open(request, *args, **kwargs):
            captured["sent"] = True
            return RecordingResponse(b"{}")

        stderr = io.StringIO()
        environ = {"CREDENTIAL_PROXY_TOKEN_FILE": "/nonexistent/token"}
        with patch.dict("os.environ", environ, clear=False):
            with patch.object(credential_proxy_client, "open_broker_request", fake_open):
                with patch("sys.stderr", new=stderr):
                    exit_code = credential_proxy_client.execute(
                        "http://proxy", ["kubectl", "get", "pods"]
                    )

        self.assertEqual(1, exit_code)
        self.assertNotIn("sent", captured, "a request with no credential must not be sent")
        self.assertIn("credential proxy token unavailable", stderr.getvalue())

    def test_an_empty_token_file_is_a_failure_not_an_empty_header(self):
        # The kubelet writes a projected token atomically, but a Secret mounted
        # before its data exists is empty, and "Bearer " is a 401 with no clue.
        path = self.token_file("")
        with patch.dict("os.environ", {"CREDENTIAL_PROXY_TOKEN_FILE": str(path)}, clear=False):
            with self.assertRaises(credential_proxy_client.TokenUnavailable):
                credential_proxy_client.authorization_headers()


class TestConnectTimeout(unittest.TestCase):
    """A bounded connect, and a response that is not bounded.

    Envoy routes /v1/exec with `timeout: 0s` on purpose: a proxied
    `get-credentials` or a large clone runs for minutes. A total timeout would
    cap the command; no timeout at all leaves the agent's kubectl blocked
    forever against a broker Pod that is Pending. So the connect is bounded and
    nothing else is.
    """

    def test_the_socket_timeout_is_cleared_once_connected(self):
        connection = credential_proxy_client.BrokerConnection("broker", 8765)
        observed = {}

        class FakeSocket:
            def settimeout(self, value):
                observed["after_connect"] = value

        def fake_connect(self):
            observed["during_connect"] = self.timeout
            self.sock = FakeSocket()

        with patch.object(
            credential_proxy_client.http.client.HTTPConnection, "connect", fake_connect
        ):
            connection.connect()

        self.assertEqual(
            credential_proxy_client.BROKER_CONNECT_TIMEOUT_SECONDS,
            observed["during_connect"],
            "reaching a Pending broker Pod must not block forever",
        )
        self.assertIsNone(
            observed["after_connect"],
            "a long-running proxied command must not be cut off by a client timeout",
        )

    def test_the_opener_does_not_follow_a_redirect(self):
        # urllib re-sends Authorization across a cross-host redirect, so a 302
        # from a compromised broker would hand the projected token to whatever
        # the Location names.
        handlers = [
            handler
            for handler in credential_proxy_client._BROKER_OPENER.handlers
            if isinstance(handler, credential_proxy_client._NoRedirect)
        ]
        self.assertEqual(1, len(handlers))
        self.assertIsNone(
            handlers[0].redirect_request(
                None, None, 302, "Found", {}, "http://elsewhere.invalid/"
            ),
            "a redirect out of the broker must not be followed",
        )

    def test_the_opener_uses_that_connection(self):
        # Building the opener with the wrong handler would silently restore the
        # stdlib connection and its unbounded connect.
        handlers = [
            handler
            for handler in credential_proxy_client._BROKER_OPENER.handlers
            if isinstance(handler, credential_proxy_client._BrokerHTTPHandler)
        ]
        self.assertEqual(1, len(handlers))


if __name__ == "__main__":
    unittest.main(verbosity=2)
