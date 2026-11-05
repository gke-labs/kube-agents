#!/usr/bin/env python3
"""Tests for the credential proxy client shim.

The shim is what every `kubectl`/`gcloud` in the sandbox actually is, so what it puts in the request body decides whether a command
reaches the right cluster - or is rejected outright.

Run:  python3 agents/platform/scripts/test_credential_proxy_client.py
"""

import http.client
import http.server
import io
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import command_policy
import credential_proxy
import credential_proxy_client

try:
    import tools.terminal_hints as terminal_hints
    HAS_HERMES = True
except ImportError:
    terminal_hints = None  # type: ignore[assignment]
    HAS_HERMES = False


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


class TestTruncatedReplyExitsNonZero(SubmittedPayloadTestCase):
    """A cut payload is not the command's answer: the shim exits
    EXIT_OUTPUT_TRUNCATED on every reply that says `truncated`, whatever the
    command itself exited, and the stderr marker keeps the prefix the collectors
    match while adding the counts the broker gave."""

    ARGV = ["kubectl", "get", "pods", "-A", "-o", "json"]

    def run_with(self, response):
        captured = {}

        def fake_open(request, *args, **kwargs):
            return RecordingResponse(json.dumps(response).encode("utf-8"))

        # An empty KUBECONFIG: the shim then names no context and resolves no file.
        with patch.dict("os.environ", {"KUBECONFIG": ""}, clear=False):
            with patch.object(credential_proxy_client, "open_broker_request", fake_open):
                with patch("sys.stdout", new=io.StringIO()) as out, patch("sys.stderr", new=io.StringIO()) as err:
                    captured["exit_code"] = credential_proxy_client.execute(self.LOCAL_ENDPOINT, self.ARGV)
        captured["stdout"], captured["stderr"] = out.getvalue(), err.getvalue()
        return captured

    def test_a_cut_reply_exits_74_with_the_counts_on_the_marker(self):
        got = self.run_with({"exitCode": 0, "stdout": '{"items": [{"kind": "Pod"', "stderr": "",
                             "truncated": True, "bytesWritten": 8388608, "limit": 8388608})
        self.assertEqual(credential_proxy_client.EXIT_OUTPUT_TRUNCATED, got["exit_code"])
        self.assertEqual(74, got["exit_code"])
        # What was kept still reaches stdout: a caller that pages wants it.
        self.assertEqual('{"items": [{"kind": "Pod"', got["stdout"])
        self.assertIn("credential proxy output truncated at 8388608 bytes (kept 8388608 bytes of stdout; exit 74)", got["stderr"])

    def test_the_marker_keeps_its_prefix_without_counts_from_an_older_broker(self):
        got = self.run_with({"exitCode": 0, "stdout": "{", "truncated": True})
        self.assertEqual(74, got["exit_code"])
        self.assertEqual("credential proxy output truncated\n", got["stderr"])

    def test_a_failed_and_cut_command_still_reports_the_cut(self):
        # The cut is the fact that decides what the caller holds; the child's
        # own exit code is in the output it already printed.
        got = self.run_with({"exitCode": 1, "stdout": "", "stderr": "error: x\n", "truncated": True,
                             "bytesWritten": 0, "limit": 4096})
        self.assertEqual(74, got["exit_code"])
        self.assertTrue(got["stderr"].startswith("error: x\n"))

    def test_a_whole_reply_passes_the_exit_code_through(self):
        for exit_code in (0, 1, 3):
            with self.subTest(exit_code=exit_code):
                got = self.run_with({"exitCode": exit_code, "stdout": "{}", "truncated": False,
                                     "bytesWritten": 2, "limit": 8388608})
                self.assertEqual(exit_code, got["exit_code"])
                self.assertNotIn("truncated", got["stderr"])

    def test_counts_that_are_not_integers_fall_back_to_the_bare_marker(self):
        for kept, limit in (("8", None), (True, 8388608), (8388608, False)):
            with self.subTest(kept=kept, limit=limit):
                got = self.run_with({"exitCode": 0, "stdout": "", "truncated": True, "bytesWritten": kept, "limit": limit})
                self.assertEqual(74, got["exit_code"])
                self.assertEqual("credential proxy output truncated\n", got["stderr"])

    def test_a_cut_that_fell_on_stderr_alone_keeps_the_commands_exit_code(self):
        # `kubectl -v=9` from the shell logs every body to stderr; stdout is
        # whole, so the payload is the command's answer and the exit code is its.
        got = self.run_with({"exitCode": 0, "stdout": "{}", "stderr": "I0101 ...", "truncated": True,
                             "stdoutTruncated": False, "bytesWritten": 2, "limit": 8388608})
        self.assertEqual(0, got["exit_code"])
        self.assertIn("credential proxy output truncated (stderr only; stdout is complete)", got["stderr"])
        got = self.run_with({"exitCode": 3, "stdout": "", "truncated": True, "stdoutTruncated": False})
        self.assertEqual(3, got["exit_code"])

    def test_a_broker_without_the_stream_field_is_read_as_a_cut_payload(self):
        got = self.run_with({"exitCode": 0, "stdout": "{", "truncated": True, "bytesWritten": 1, "limit": 4096})
        self.assertEqual(74, got["exit_code"])


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

    def test_a_refetch_replaces_the_pin_and_leaves_nothing_staged(self):
        # The Cluster Agent's pin is read by every kubectl of that profile, so
        # it is staged and renamed like the implicit file, never truncated.
        self.destination.parent.mkdir(parents=True)
        self.destination.write_text("stale", encoding="utf-8")
        with patch.object(
            credential_proxy_client, "_replace_file", wraps=credential_proxy_client._replace_file
        ) as writer:
            captured = self.run_it(self.ARGV, {"KUBECONFIG": str(self.destination)})
        self.assertEqual(0, captured["exit_code"])
        writer.assert_called_once_with(self.destination, self.GENERATED)
        self.assertEqual(self.destination.read_text(encoding="utf-8"), self.GENERATED)
        self.assertEqual([self.destination], list(self.destination.parent.iterdir()))

    def test_an_unwritable_destination_fails_the_command(self):
        # Unlike the implicit file, the caller named this path, so not landing
        # it is a failure -- and that still holds with the staged writer.
        blocker = self.directory / "not-a-directory"
        blocker.write_text("", encoding="utf-8")
        captured = self.run_it(self.ARGV, {"KUBECONFIG": str(blocker / "kubeconfig.yaml")})
        self.assertEqual(1, captured["exit_code"])

    def test_no_destination_still_asks_for_the_file_back(self):
        # Before #1968 nothing came back, and the context-less kubectl that
        # followed read the host cluster (#1852's default) and reported a
        # seeded fixture absent.
        home = self.directory / "home"
        captured = self.run_it(self.ARGV, {"KUBECONFIG": "", "HERMES_HOME": str(home)})
        self.assertTrue(captured["payload"]["wantsKubeconfig"])


# A second GKE cluster, standing in for a seeded fleet member.
SEEDED_CONTEXT = "gke_acme-evals_us-central1-a_seeded-a"
SEEDED_KUBECONFIG = f"apiVersion: v1\nkind: Config\ncurrent-context: {SEEDED_CONTEXT}\n"
CARD = "t_0123abcd"


class ContextLessTestCase(SubmittedPayloadTestCase):
    """A shell with no KUBECONFIG, as the platform profile's is (#1968)."""

    GET_CREDENTIALS = [
        "gcloud", "container", "clusters", "get-credentials", "seeded-a",
        "--location", "us-central1-a",
    ]

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        self.expected_file = (
            self.home / ".kubeconfigs" / "kubeconfig_acme-evals_seeded-a_us-central1-a.yaml"
        )

    def environ(self, **extra):
        base = {"KUBECONFIG": "", "HERMES_HOME": str(self.home)}
        base.update(extra)
        return base

    @staticmethod
    def in_shell(*keys):
        """Run as though this process's ancestors were `keys`, nearest first.

        Each key is `<pid>-<start time>`; an empty list is a process with no
        shell to pin to.
        """
        return patch.object(credential_proxy_client, "_shell_keys", return_value=list(keys))

    def fetch(self, environ):
        """Run get-credentials and return (captured, stderr text)."""
        stderr = io.StringIO()
        captured = {}

        def fake_open(request, *args, **kwargs):
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            body = {"exitCode": 0, "kubeconfig": SEEDED_KUBECONFIG}
            return RecordingResponse(json.dumps(body).encode("utf-8"))

        with patch.dict("os.environ", environ, clear=False):
            with patch.object(credential_proxy_client, "open_broker_request", fake_open):
                with patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=stderr):
                    captured["exit_code"] = credential_proxy_client.execute(
                        self.LOCAL_ENDPOINT, list(self.GET_CREDENTIALS)
                    )
        return captured, stderr.getvalue()


class TestAContextLessGetCredentialsLandsAFile(ContextLessTestCase):
    """The minimal half: the file lands where AGENTS.md says, and the shell is told."""

    def test_the_file_lands_at_the_per_target_path(self):
        captured, _ = self.fetch(self.environ())
        self.assertEqual(0, captured["exit_code"])
        self.assertEqual(SEEDED_KUBECONFIG, self.expected_file.read_text(encoding="utf-8"))

    def test_the_path_matches_the_mcp_servers_convention(self):
        # _thread_kubeconfig_path owns the name; the two must not drift.
        target = credential_proxy_client.parse_gke_context(SEEDED_CONTEXT)
        with patch.dict("os.environ", {"HERMES_HOME": str(self.home)}, clear=False):
            path = credential_proxy_client.per_target_kubeconfig_path(target)
        self.assertEqual(self.expected_file, path)

    def test_with_no_shell_it_says_the_host_is_still_the_default_and_how_to_move(self):
        with self.in_shell():
            _, stderr = self.fetch(self.environ())
        self.assertIn("still reads the host cluster", stderr)
        self.assertIn(f"export KUBECONFIG={self.expected_file}", stderr)
        self.assertIn(f"kubectl --context={SEEDED_CONTEXT}", stderr)

    def test_in_a_shell_it_says_how_long_the_default_lasts_and_how_to_move(self):
        with self.in_shell("100-5"):
            _, stderr = self.fetch(self.environ())
        self.assertIn(f"For the rest of this command line, a kubectl that names no cluster reads {SEEDED_CONTEXT}", stderr)
        self.assertIn("Later commands read the host cluster", stderr)
        self.assertIn(f"export KUBECONFIG={self.expected_file}", stderr)
        self.assertIn(f"kubectl --context={SEEDED_CONTEXT}", stderr)

    def test_the_exported_file_then_reaches_the_cluster(self):
        # The instruction it prints has to work: the file resolves through the
        # existing KUBECONFIG path to the fetched cluster's name.
        self.fetch(self.environ())
        payload = self.submit(
            ["kubectl", "get", "clusterrolebinding"],
            self.environ(KUBECONFIG=str(self.expected_file)),
        )
        self.assertEqual(SEEDED_CONTEXT, payload["kubeconfigContext"])

    def test_an_unwritable_home_does_not_fail_the_command(self):
        # gcloud succeeded, and --context still reaches the cluster.
        blocker = self.home / "not-a-directory"
        blocker.write_text("", encoding="utf-8")
        captured, stderr = self.fetch(self.environ(HERMES_HOME=str(blocker)))
        self.assertEqual(0, captured["exit_code"])
        self.assertIn("could not write", stderr)
        self.assertIn(f"kubectl --context={SEEDED_CONTEXT}", stderr)
        self.assertNotIn("export KUBECONFIG", stderr)

    def test_an_explicit_destination_is_unchanged(self):
        destination = self.home / "mine.yaml"
        self.fetch(self.environ(KUBECONFIG=str(destination)))
        self.assertTrue(destination.is_file())
        self.assertFalse(self.expected_file.exists())

    def test_a_second_fetch_replaces_the_file_and_leaves_nothing_staged(self):
        # Staged beside the destination and renamed over it, so a reader never
        # sees it half-written, and no staging file is left behind.
        self.expected_file.parent.mkdir(parents=True)
        self.expected_file.write_text("stale", encoding="utf-8")
        self.fetch(self.environ())
        self.assertEqual(SEEDED_KUBECONFIG, self.expected_file.read_text(encoding="utf-8"))
        files = [p for p in self.expected_file.parent.iterdir() if p.is_file()]
        self.assertEqual([self.expected_file], files)


class TestKubectlContextFlagNamesTheCluster(ContextLessTestCase):
    """`--context` used to end in "context was not found" (#1968, step 37)."""

    def test_a_gke_context_flag_is_forwarded_as_the_cluster_name(self):
        for argv in (
            ["kubectl", "get", "crb", "--context", SEEDED_CONTEXT],
            ["kubectl", f"--context={SEEDED_CONTEXT}", "get", "crb"],
        ):
            with self.subTest(argv=argv):
                payload = self.submit(argv, self.environ())
                self.assertEqual(SEEDED_CONTEXT, payload["kubeconfigContext"])
                # The flag itself stays: the generated file's one context is it.
                self.assertIn(SEEDED_CONTEXT, " ".join(payload["argv"]))

    def test_a_non_gke_context_is_left_to_kubectl(self):
        payload = self.submit(["kubectl", "--context", "minikube", "get", "pods"], self.environ())
        self.assertNotIn("kubeconfigContext", payload)

    def test_words_after_the_separator_are_not_kubectls(self):
        argv = ["kubectl", "exec", "pod/x", "--", "tool", "--context", SEEDED_CONTEXT]
        payload = self.submit(argv, self.environ())
        self.assertNotIn("kubeconfigContext", payload)

    def test_kubeconfig_keeps_precedence(self):
        # A Cluster Agent's pin is never overridden from here.
        pinned = write_kubeconfig(self.home)
        payload = self.submit(
            ["kubectl", "--context", SEEDED_CONTEXT, "get", "pods"],
            self.environ(KUBECONFIG=str(pinned)),
        )
        self.assertEqual(GKE_CONTEXT, payload["kubeconfigContext"])

    def test_gcloud_is_not_given_a_context(self):
        payload = self.submit(
            ["gcloud", "container", "clusters", "list", "--context", SEEDED_CONTEXT],
            self.environ(),
        )
        self.assertNotIn("kubeconfigContext", payload)


class TestGetCredentialsWithFlagsBetweenItsWords(ContextLessTestCase):
    """A flag between the command words is still get-credentials to the shim."""

    GET_CREDENTIALS = [
        "gcloud", "container", "clusters", "--location", "us-central1-a",
        "get-credentials", "seeded-a",
    ]

    def test_the_file_is_asked_for_and_lands(self):
        captured, _ = self.fetch(self.environ())
        self.assertEqual(0, captured["exit_code"])
        self.assertTrue(captured["payload"]["wantsKubeconfig"])
        self.assertEqual(SEEDED_KUBECONFIG, self.expected_file.read_text(encoding="utf-8"))

    def test_the_detector_reads_the_words_past_the_flags(self):
        cases = [
            (["gcloud", "container", "clusters", "get-credentials", "c"], True),
            (["gcloud", "container", "clusters", "--location=l", "get-credentials", "c"], True),
            (["gcloud", "container", "clusters", "--location", "l", "get-credentials", "c"], True),
            (["gcloud", "container", "--project=p", "clusters", "get-credentials", "c"], True),
            (["gcloud", "container", "--project", "p", "clusters", "get-credentials", "c"], True),
            (["gcloud", "--project", "p", "container", "clusters", "get-credentials", "c"], True),
            (["gcloud", "container", "--quiet", "clusters", "--zone", "l", "get-credentials", "c"], True),
            (["gcloud", "beta", "container", "clusters", "get-credentials", "c"], True),
            (["gcloud", "container", "clusters", "describe", "get-credentials"], False),
            (["gcloud", "container", "clusters", "list", "--location", "l"], False),
            (["gcloud", "container", "clusters", "get-credentials"][:3], False),
            (["kubectl", "container", "clusters", "get-credentials"], False),
            ([], False),
        ]
        for argv, want in cases:
            with self.subTest(argv=argv):
                self.assertEqual(want, credential_proxy_client.is_get_credentials(argv))


class TestTheShimAndTheBrokerAgreeOnGetCredentials(unittest.TestCase):
    """The shim's get-credentials walk reads argv as the broker's does.

    The shim can't import `command_policy` in the sandbox, so it keeps its own
    walk. This holds the two to one table of spellings, and holds the shim's
    short valued flags to the policy's, so a short valued flag added to the
    policy fails here until the shim learns it too.
    """

    GC = ["gcloud", "container", "clusters"]

    AGREED = [
        (GC + ["get-credentials", "c"], True),
        (GC + ["--location=l", "get-credentials", "c"], True),
        (GC + ["--location", "l", "get-credentials", "c"], True),
        (GC + ["--zone", "l", "get-credentials", "c"], True),
        (GC + ["-z", "l", "get-credentials", "c"], True),
        (GC + ["-z=l", "get-credentials", "c"], True),
        (["gcloud", "container", "-z", "l", "clusters", "get-credentials", "c"], True),
        (["gcloud", "-z", "l", "container", "clusters", "get-credentials", "c"], True),
        (["gcloud", "container", "--project=p", "clusters", "get-credentials", "c"], True),
        (["gcloud", "container", "--project", "p", "clusters", "get-credentials", "c"], True),
        (["gcloud", "--project", "p", "container", "clusters", "get-credentials", "c"], True),
        (["gcloud", "--project=p", "container", "clusters", "get-credentials", "c"], True),
        (["gcloud", "container", "--quiet", "clusters", "--zone", "l", "get-credentials", "c"], True),
        (["gcloud", "alpha", "container", "clusters", "get-credentials", "c"], True),
        (["gcloud", "beta", "container", "clusters", "get-credentials", "c"], True),
        (["gcloud", "beta", "container", "clusters", "-z", "l", "get-credentials", "c"], True),
        (GC + ["describe", "get-credentials"], False),
        (GC + ["list", "--location", "l"], False),
        (GC + ["-z", "l", "list"], False),
        (GC + ["describe", "c", "-z", "l"], False),
        (["gcloud", "projects", "list"], False),
        (["gcloud", "compute", "instances", "list", "--format=json"], False),
        (GC, False),
        (["kubectl", "container", "clusters", "get-credentials"], False),
        (["kubectl", "get", "pods", "-n", "default"], False),
        ([], False),
    ]

    # Known and harmless: the shim never lets a bare `--flag` take one of the
    # three command words as its value, so it reads `get-credentials` here as
    # the verb where the broker reads it as `--format`'s value. The shim asks
    # for a file the broker doesn't send, and nothing lands. (argv, shim, broker)
    KNOWN_DISAGREEMENTS = [
        (GC + ["--format", "get-credentials", "list"], True, False),
        # Malformed argv the read-only policy refuses before routing: the broker
        # can't read them and falls back to "carries the verb", the shim reads
        # them its own way. Pinned so any other disagreement fails.
        (["gcloud", "--", "container", "clusters", "get-credentials", "c"], False, True),
        (GC + ["-qz", "l", "get-credentials", "c"], False, True),
        (GC + ["--format", "-z", "get-credentials", "c"], False, True),
    ]

    def test_the_release_tracks_are_the_policys(self):
        self.assertEqual(command_policy._GCLOUD_RELEASE_TRACKS, credential_proxy_client._GCLOUD_RELEASE_TRACKS)

    def test_both_read_every_spelling_the_same(self):
        for argv, want in self.AGREED:
            with self.subTest(argv=argv):
                self.assertEqual(want, credential_proxy._is_get_credentials(argv))
                self.assertEqual(want, credential_proxy_client.is_get_credentials(argv))

    def test_the_known_disagreements_are_the_only_ones(self):
        for argv, shim, broker in self.KNOWN_DISAGREEMENTS:
            with self.subTest(argv=argv):
                self.assertEqual(shim, credential_proxy_client.is_get_credentials(argv))
                self.assertEqual(broker, credential_proxy._is_get_credentials(argv))

    def test_the_shim_knows_every_short_valued_flag_the_policy_does(self):
        policy_short = {
            flag for flag in command_policy._GCLOUD_FLAGS_WITH_VALUE
            if flag.startswith("-") and not flag.startswith("--")
        }
        self.assertEqual(policy_short, set(credential_proxy_client._GCLOUD_SHORT_FLAGS_WITH_VALUE))


class TestAShellFollowsItsOwnGetCredentials(ContextLessTestCase):
    """gcloud's "just fetched is current", for the rest of one command line only."""

    KUBECTL = ["kubectl", "get", "clusterrolebinding", "debug-binding"]

    def fetch_as(self, cluster, *keys):
        """A context-less get-credentials for `cluster`, run from `keys`."""
        context = f"gke_acme-evals_us-central1-a_{cluster}"
        kubeconfig = f"apiVersion: v1\nkind: Config\ncurrent-context: {context}\n"
        stderr = io.StringIO()

        def fake_open(request, *args, **kwargs):
            body = {"exitCode": 0, "kubeconfig": kubeconfig}
            return RecordingResponse(json.dumps(body).encode("utf-8"))

        argv = ["gcloud", "container", "clusters", "get-credentials", cluster,
                "--location", "us-central1-a"]
        # The keys are made up, so none is a live process and pruning would
        # remove the other fakes' pins; TestShellAncestry covers pruning.
        with self.in_shell(*keys), patch.dict("os.environ", self.environ(), clear=False), \
             patch.object(credential_proxy_client, "_prune_shell_contexts"):
            with patch.object(credential_proxy_client, "open_broker_request", fake_open):
                with patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=stderr):
                    credential_proxy_client.execute(self.LOCAL_ENDPOINT, argv)
        return context

    def kubectl_as(self, *keys, argv=None, **extra):
        with self.in_shell(*keys):
            return self.submit(argv or list(self.KUBECTL), self.environ(**extra))

    def test_the_same_line_kubectl_reaches_the_fetched_cluster(self):
        # `get-credentials seeded-a && kubectl get crb debug-binding`, the
        # exact shape every #1838 rep ran: both are children of one shell.
        self.fetch_as("seeded-a", "100-5")
        payload = self.kubectl_as("100-5")
        self.assertEqual(SEEDED_CONTEXT, payload["kubeconfigContext"])

    def test_it_works_in_process_too(self):
        # The real ancestry, not a patched one: a fetch and a kubectl from this
        # process share a parent, as two commands of one line do.
        if not credential_proxy_client._shell_keys():
            self.skipTest("this test process was not started from a shell")
        self.fetch(self.environ())
        payload = self.submit(list(self.KUBECTL), self.environ())
        self.assertEqual(SEEDED_CONTEXT, payload["kubeconfigContext"])

    def test_the_next_command_line_reads_the_host(self):
        # A new `bash -c` per command: a later turn, a resumed card, another
        # worker. #1799: the default must not drift beyond the fetching line.
        self.fetch_as("seeded-a", "100-5")
        self.assertNotIn("kubeconfigContext", self.kubectl_as("101-9"))

    def test_a_recycled_pid_is_not_the_same_shell(self):
        self.fetch_as("seeded-a", "100-5")
        self.assertNotIn("kubeconfigContext", self.kubectl_as("100-6"))

    def test_a_kubectl_nested_below_the_shell_still_finds_it(self):
        # `$(kubectl ...)`, a pipeline stage or a subshell puts a process
        # between the fetching shell and kubectl.
        self.fetch_as("seeded-a", "100-5")
        payload = self.kubectl_as("300-7", "100-5")
        self.assertEqual(SEEDED_CONTEXT, payload["kubeconfigContext"])

    def test_parallel_subshells_each_keep_their_own(self):
        # `(get-credentials a && kubectl) & (get-credentials b && kubectl) &`:
        # each fetch pins its own subshell, so neither moves the other's.
        a = self.fetch_as("seeded-a", "201-1", "100-5")
        b = self.fetch_as("seeded-b", "202-1", "100-5")
        self.assertEqual(a, self.kubectl_as("201-1", "100-5")["kubeconfigContext"])
        self.assertEqual(b, self.kubectl_as("202-1", "100-5")["kubeconfigContext"])
        # And the parent shell, which fetched nothing, still reads the host.
        self.assertNotIn("kubeconfigContext", self.kubectl_as("100-5"))

    def test_the_nearest_pin_wins(self):
        outer = self.fetch_as("seeded-a", "100-5")
        inner = self.fetch_as("seeded-b", "201-1", "100-5")
        self.assertEqual(inner, self.kubectl_as("201-1", "100-5")["kubeconfigContext"])
        self.assertEqual(outer, self.kubectl_as("100-5")["kubeconfigContext"])

    def test_a_second_fetch_in_the_same_line_moves_it(self):
        self.fetch_as("seeded-a", "100-5")
        b = self.fetch_as("seeded-b", "100-5")
        self.assertEqual(b, self.kubectl_as("100-5")["kubeconfigContext"])

    def test_no_shell_means_no_pin_is_written(self):
        self.fetch_as("seeded-a")
        self.assertFalse((self.home / ".kubeconfigs" / "shells").exists())

    def test_an_explicit_context_beats_the_pin(self):
        self.fetch_as("seeded-a", "100-5")
        payload = self.kubectl_as("100-5", argv=["kubectl", "--context", GKE_CONTEXT, "get", "pods"])
        self.assertEqual(GKE_CONTEXT, payload["kubeconfigContext"])

    def test_kubeconfig_beats_the_pin(self):
        self.fetch_as("seeded-a", "100-5")
        pinned = write_kubeconfig(self.home)
        payload = self.kubectl_as("100-5", KUBECONFIG=str(pinned))
        self.assertEqual(GKE_CONTEXT, payload["kubeconfigContext"])

    def test_a_kubeconfig_after_the_separator_is_the_remote_commands(self):
        # `kubectl exec pod -- tool --kubeconfig f` names no kubeconfig of
        # kubectl's: it neither unpins the shell nor is read from this pod.
        self.fetch_as("seeded-a", "100-5")
        remote = "/nowhere/remote.yaml"
        with self.in_shell("100-5"):
            captured = self.send(
                ["kubectl", "exec", "pod/x", "--", "tool", "--kubeconfig", remote],
                self.environ(),
            )
        self.assertEqual(0, captured["exit_code"])
        self.assertEqual(SEEDED_CONTEXT, captured["payload"]["kubeconfigContext"])
        self.assertEqual(remote, captured["payload"]["argv"][-1])

    def test_an_explicit_destination_records_no_pin(self):
        # gcloud leaves the default kubeconfig alone when told to write
        # elsewhere, and so does this.
        destination = self.home / "mine.yaml"
        with self.in_shell("100-5"):
            self.fetch(self.environ(KUBECONFIG=str(destination)))
        self.assertNotIn("kubeconfigContext", self.kubectl_as("100-5"))

    def test_a_pin_that_is_not_a_gke_name_is_ignored(self):
        # The file is the agent's to write; it only ever crosses as a name the
        # grammar accepts.
        shells = self.home / ".kubeconfigs" / "shells"
        shells.mkdir(parents=True)
        for content in ("minikube", "gke_../x_y_z", SEEDED_CONTEXT + "x" * 600):
            with self.subTest(content=content[:20]):
                (shells / "100-5.context").write_text(content, encoding="utf-8")
                self.assertNotIn("kubeconfigContext", self.kubectl_as("100-5"))


class TestShellAncestry(unittest.TestCase):
    """Reading /proc: the key, the walk, and pruning pins of exited shells."""

    def setUp(self):
        self.proc = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.proc, ignore_errors=True)
        patcher = patch.object(credential_proxy_client, "PROC_ROOT", self.proc)
        patcher.start()
        self.addCleanup(patcher.stop)

    def process(self, pid, ppid, start, comm="bash"):
        # Fields 3..22 of /proc/<pid>/stat: state, ppid, then 17 others, then
        # the start time; the values between do not matter here.
        fields = ["S", str(ppid)] + ["0"] * 17 + [str(start), "0"]
        (self.proc / str(pid)).mkdir()
        (self.proc / str(pid) / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}\n")

    def test_the_walk_is_nearest_first_and_stops_at_init(self):
        self.process(300, 200, 33)
        self.process(200, 100, 22)
        self.process(100, 1, 11)
        with patch.object(credential_proxy_client.os, "getppid", return_value=300):
            keys = credential_proxy_client._shell_keys()
        self.assertEqual(["300-33", "200-22", "100-11"], keys)

    def test_a_command_name_with_spaces_and_parentheses_is_parsed(self):
        self.process(300, 1, 44, comm="odd) (name x")
        self.assertEqual((1, "44", "odd) (name x"), credential_proxy_client._process_stat(300))

    def test_the_walk_keys_shells_and_steps_over_everything_else(self):
        # `get-credentials x && timeout 30 kubectl ...`: kubectl's parent is
        # timeout, and the line's shell is above it; sshd is never a key.
        self.process(300, 200, 33, comm="timeout")
        self.process(200, 100, 22)
        self.process(100, 1, 11, comm="sshd")
        with patch.object(credential_proxy_client.os, "getppid", return_value=300):
            self.assertEqual(["200-22"], credential_proxy_client._shell_keys())
        with patch.object(credential_proxy_client.os, "getppid", return_value=100):
            self.assertEqual([], credential_proxy_client._shell_keys())

    def test_a_pin_named_for_a_process_that_is_not_a_shell_is_not_read(self):
        # sshd outlives every command line on a ControlMaster connection; a
        # file keyed on it must not become every later command's default.
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        shells = home / ".kubeconfigs" / "shells"
        shells.mkdir(parents=True)
        (shells / "100-11.context").write_text(SEEDED_CONTEXT)
        self.process(300, 200, 33, comm="kubectl")
        self.process(200, 100, 22)
        self.process(100, 1, 11, comm="sshd")
        with patch.dict("os.environ", {"HERMES_HOME": str(home)}, clear=False), \
             patch.object(credential_proxy_client.os, "getpid", return_value=300), \
             patch.object(credential_proxy_client.os, "getppid", return_value=200):
            self.assertIsNone(credential_proxy_client.shell_context())

    def _pinned_shell(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        shells = home / ".kubeconfigs" / "shells"
        shells.mkdir(parents=True)
        self.process(300, 200, 33, comm="kubectl")
        self.process(200, 1, 22)
        env = patch.dict("os.environ", {"HERMES_HOME": str(home)}, clear=False)
        pid = patch.object(credential_proxy_client.os, "getpid", return_value=300)
        ppid = patch.object(credential_proxy_client.os, "getppid", return_value=200)
        return shells / "200-22.context", env, pid, ppid

    def test_a_pin_another_uid_owns_is_not_read(self):
        # hermes runs with HERMES_HOME=/opt/data, which the agent owns: a pin
        # the agent planted under a hermes shell's key must not steer it.
        path, env, pid, ppid = self._pinned_shell()
        path.write_text(SEEDED_CONTEXT)
        with env, pid, ppid:
            self.assertEqual(SEEDED_CONTEXT, credential_proxy_client.shell_context())
            with patch.object(credential_proxy_client.os, "geteuid", return_value=os.geteuid() + 1):
                self.assertIsNone(credential_proxy_client.shell_context())

    def test_a_symlinked_or_hard_linked_pin_is_not_read(self):
        path, env, pid, ppid = self._pinned_shell()
        target = path.parent / "elsewhere"
        target.write_text(SEEDED_CONTEXT)
        path.symlink_to(target)
        with env, pid, ppid:
            self.assertIsNone(credential_proxy_client.shell_context())
            path.unlink()
            os.link(target, path)
            self.assertIsNone(credential_proxy_client.shell_context())
            path.unlink()
            path.write_text(SEEDED_CONTEXT)
            self.assertEqual(SEEDED_CONTEXT, credential_proxy_client.shell_context())

    def test_a_fifo_pin_is_refused_without_blocking(self):
        import threading

        path, env, pid, ppid = self._pinned_shell()
        os.mkfifo(path)
        result = []
        with env, pid, ppid:
            reader = threading.Thread(
                target=lambda: result.append(credential_proxy_client.shell_context()), daemon=True
            )
            reader.start()
            reader.join(timeout=5)
        self.assertFalse(reader.is_alive(), "shell_context() blocked opening a FIFO")
        self.assertEqual([None], result)

    def test_an_execd_kubectl_reads_its_own_key_first(self):
        # `get-credentials x ; kubectl` -- bash execs the last command, so the
        # kubectl is the process the pin was recorded under.
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        (home / ".kubeconfigs" / "shells").mkdir(parents=True)
        (home / ".kubeconfigs" / "shells" / "300-33.context").write_text(SEEDED_CONTEXT)
        self.process(300, 200, 33, comm="python3")
        self.process(200, 1, 22, comm="sshd")
        with patch.dict("os.environ", {"HERMES_HOME": str(home)}, clear=False), \
             patch.object(credential_proxy_client.os, "getpid", return_value=300), \
             patch.object(credential_proxy_client.os, "getppid", return_value=200):
            self.assertEqual(SEEDED_CONTEXT, credential_proxy_client.shell_context())

    def test_an_exited_process_has_no_key(self):
        self.assertIsNone(credential_proxy_client._process_stat(999))
        with patch.object(credential_proxy_client.os, "getppid", return_value=999):
            self.assertEqual([], credential_proxy_client._shell_keys())

    def test_the_walk_is_bounded(self):
        # A cycle cannot happen in a real process table; the bound holds anyway.
        self.process(300, 300, 1)
        with patch.object(credential_proxy_client.os, "getppid", return_value=300):
            keys = credential_proxy_client._shell_keys()
        self.assertEqual(credential_proxy_client.MAX_SHELL_ANCESTORS, len(keys))

    def test_recording_prunes_the_pins_of_exited_shells(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        shells = home / ".kubeconfigs" / "shells"
        shells.mkdir(parents=True)
        self.process(300, 1, 33)  # this command's shell
        self.process(400, 1, 44)  # another live shell
        self.process(500, 1, 56)  # pid 500 was recycled since its pin
        for name in ("400-44.context", "500-55.context", "600-66.context", "notes.txt"):
            (shells / name).write_text(SEEDED_CONTEXT, encoding="utf-8")
        with patch.dict("os.environ", {"HERMES_HOME": str(home)}, clear=False), \
             patch.object(credential_proxy_client.os, "getppid", return_value=300):
            self.assertTrue(credential_proxy_client.record_shell_context(SEEDED_CONTEXT))
        self.assertEqual(
            ["300-33.context", "400-44.context", "notes.txt"],
            sorted(p.name for p in shells.iterdir()),
        )


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
            # gh's flag, gone with its shim.
            ["gh", "pr", "create", "--body-file", "-"],
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


class VcsCallErrorTest(unittest.TestCase):
    """`vcs_call` hands a refusal through whole, and tells "not built" from "no"."""

    endpoint = "http://127.0.0.1:8765"

    @staticmethod
    def _refuse(status, answer):
        def refuse(request, *args, **kwargs):
            raise urllib.error.HTTPError(
                request.full_url, status, "refused", {}, io.BytesIO(json.dumps(answer).encode())
            )

        return patch.object(credential_proxy_client, "open_broker_request", refuse)

    def test_a_refusal_carries_the_brokers_answer_through(self):
        answer = {"error": "the base branch moved", "code": "BASE_MOVED", "paths": ["manifests/app.yaml"]}
        with self._refuse(409, answer), self.assertRaises(credential_proxy_client.WorkspaceRequestError) as caught:
            credential_proxy_client.vcs_call(self.endpoint, "publish", {})
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.payload, answer)
        self.assertEqual(str(caught.exception), "the base branch moved")

    def test_a_broker_without_version_control_is_distinguishable_from_a_refusal(self):
        with self._refuse(404, {"error": "not available", "code": "VCS_UNAVAILABLE"}):
            with self.assertRaises(credential_proxy_client.WorkspaceUnavailable):
                credential_proxy_client.vcs_call(self.endpoint, "snapshot", {})


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


class TestTaskCredential(SubmittedPayloadTestCase):
    """A profile worker's shim sends its task credential and honours no redirecting env.

    The broker confines a profile call to the cluster in the task credential
    whatever the shim sends; these hold the shim to its half: the header on
    every call, read fresh, and no environment choosing the cluster, project
    or account.
    """

    TOKEN = "eyJ0eXAiOiJKV1QifQ.eyJqdGkiOiJKIn0.c2ln"

    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, ignore_errors=True)
        self.directory = directory
        self.token_path = directory / "task-token"
        self.token_path.write_text(self.TOKEN + "\n", encoding="utf-8")
        self.kubeconfig = write_kubeconfig(directory)

    def run_shim(self, argv, environ):
        captured = {}
        stderr = io.StringIO()

        def fake_open(request, *args, **kwargs):
            captured["request"] = request
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return RecordingResponse(json.dumps({"exitCode": 0}).encode("utf-8"))

        with patch.dict("os.environ", environ, clear=False):
            with patch.object(credential_proxy_client, "open_broker_request", fake_open):
                with patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=stderr):
                    captured["exit_code"] = credential_proxy_client.execute(self.LOCAL_ENDPOINT, argv)
        captured["stderr"] = stderr.getvalue()
        return captured

    def task_env(self, **extra):
        return {credential_proxy_client.TASK_TOKEN_FILE_ENV: str(self.token_path), "KUBECONFIG": "", **extra}

    def test_the_credential_is_sent_in_its_header(self):
        sent = self.run_shim(["kubectl", "get", "pods"], self.task_env())
        self.assertEqual(self.TOKEN, sent["request"].get_header("X-kube-agents-task-credential"))
        self.assertNotIn(self.TOKEN, json.dumps(sent["payload"]), "the credential goes in a header, never the body")

    def test_the_credential_is_read_on_every_call(self):
        # A warm pod's next task writes its own credential to the same file.
        self.run_shim(["kubectl", "get", "pods"], self.task_env())
        self.token_path.write_text("a.b.next", encoding="utf-8")
        sent = self.run_shim(["kubectl", "get", "pods"], self.task_env())
        self.assertEqual("a.b.next", sent["request"].get_header("X-kube-agents-task-credential"))

    def test_no_header_outside_a_task_or_after_it(self):
        sent = self.run_shim(["kubectl", "get", "pods"], {credential_proxy_client.TASK_TOKEN_FILE_ENV: ""})
        self.assertIsNone(sent["request"].get_header("X-kube-agents-task-credential"))
        # The adapter removed the file at the task's end: no task, no header,
        # and the broker answers local-only commands only.
        self.token_path.unlink()
        sent = self.run_shim(["kubectl", "version", "--client"], self.task_env())
        self.assertEqual(0, sent["exit_code"])
        self.assertIsNone(sent["request"].get_header("X-kube-agents-task-credential"))

    def test_a_malformed_credential_is_not_sent(self):
        self.token_path.write_text("not a token\r\nX-Injected: 1", encoding="utf-8")
        sent = self.run_shim(["kubectl", "get", "pods"], self.task_env())
        self.assertEqual(1, sent["exit_code"])
        self.assertNotIn("request", sent)
        self.assertIn("does not hold a task credential", sent["stderr"])

    def test_kubeconfig_does_not_choose_the_cluster_in_a_task(self):
        sent = self.run_shim(["kubectl", "get", "pods"], self.task_env(KUBECONFIG=str(self.kubeconfig)))
        self.assertNotIn("kubeconfigContext", sent["payload"])
        self.assertIn("KUBECONFIG", sent["stderr"])
        self.assertIn("not honoured for a profile task", sent["stderr"])
        # Outside a task the same variable still pins the cluster.
        sent = self.run_shim(
            ["kubectl", "get", "pods"],
            {credential_proxy_client.TASK_TOKEN_FILE_ENV: "", "KUBECONFIG": str(self.kubeconfig)},
        )
        self.assertEqual(GKE_CONTEXT, sent["payload"]["kubeconfigContext"])

    def test_the_shell_pin_does_not_choose_the_cluster_in_a_task(self):
        with patch.object(credential_proxy_client, "shell_context", return_value=GKE_CONTEXT):
            sent = self.run_shim(["kubectl", "get", "pods"], self.task_env())
            self.assertNotIn("kubeconfigContext", sent["payload"])
            sent = self.run_shim(["kubectl", "get", "pods"], {credential_proxy_client.TASK_TOKEN_FILE_ENV: "", "KUBECONFIG": ""})
            self.assertEqual(GKE_CONTEXT, sent["payload"]["kubeconfigContext"])

    def test_get_credentials_does_not_write_to_kubeconfig_in_a_task(self):
        sent = self.run_shim(
            ["gcloud", "container", "clusters", "get-credentials", "c", "--location=l"],
            self.task_env(KUBECONFIG=str(self.kubeconfig)),
        )
        # No destination from the environment: the file comes back to the
        # implicit landing, never to the path KUBECONFIG names.
        self.assertTrue(sent["payload"]["wantsKubeconfig"])
        self.assertEqual(
            ["gcloud", "container", "clusters", "get-credentials", "c", "--location=l"], sent["payload"]["argv"]
        )

    def test_get_credentials_in_a_task_lands_no_file_and_pins_nothing(self):
        # The broker hands the generated kubeconfig back; in a task nothing
        # reads it (every call is held to the task's cluster), so it is not
        # filed, no shell context is recorded, and the shim says so in one
        # line instead of telling the model to export a KUBECONFIG it ignores.
        generated = write_kubeconfig(self.directory).read_text(encoding="utf-8")
        stderr = io.StringIO()

        def fake_open(request, *args, **kwargs):
            return RecordingResponse(json.dumps({"exitCode": 0, "kubeconfig": generated}).encode("utf-8"))

        pin = Mock(return_value=True)
        write = Mock()
        with patch.dict("os.environ", self.task_env(), clear=False):
            with patch.object(credential_proxy_client, "open_broker_request", fake_open), \
                    patch.object(credential_proxy_client, "record_shell_context", pin), \
                    patch.object(credential_proxy_client, "_replace_file", write):
                with patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=stderr):
                    exit_code = credential_proxy_client.execute(
                        self.LOCAL_ENDPOINT, ["gcloud", "container", "clusters", "get-credentials", "c", "--location=l"]
                    )
        self.assertEqual(0, exit_code)
        pin.assert_not_called()
        write.assert_not_called()
        said = stderr.getvalue()
        self.assertNotIn("export KUBECONFIG", said)
        self.assertNotIn("host cluster", said)
        self.assertIn(GKE_CONTEXT, said)
        self.assertIn("nothing to export", said)
        self.assertEqual(1, len(said.strip().splitlines()), said)

    def test_cloudsdk_settings_do_not_redirect_a_task(self):
        redirecting = {
            "CLOUDSDK_CORE_PROJECT": "other-project",
            "CLOUDSDK_CORE_ACCOUNT": "someone@example.com",
            "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT": "sa@other-project.iam.gserviceaccount.com",
            "CLOUDSDK_ACTIVE_CONFIG_NAME": "other",
            "CLOUDSDK_API_ENDPOINT_OVERRIDES_CONTAINER": "https://evil.invalid/",
            "CLOUDSDK_PYTHON": "/usr/bin/python3",
        }
        argv = ["gcloud", "container", "clusters", "list"]
        sent = self.run_shim(argv, self.task_env(**redirecting))
        # Nothing about the environment crosses: the argv is the caller's, and
        # the body carries no field that could name a project or account.
        self.assertEqual(argv, sent["payload"]["argv"])
        self.assertEqual({"requestId", "argv"}, set(sent["payload"]))
        body = json.dumps(sent["payload"])
        for value in redirecting.values():
            self.assertNotIn(value, body)
        for name in redirecting:
            if name == "CLOUDSDK_PYTHON":
                self.assertNotIn(name, sent["stderr"])
            else:
                self.assertIn(name, sent["stderr"])


class TestExecutePolicyBlocked(unittest.TestCase):
    """When the credential proxy blocks a command under security policy.

    The shim must return EXIT_SECURITY_POLICY_BLOCKED (77, EX_NOPERM from
    sysexits.h) rather than 126. Exit 126 is treated by the agent runtime as
    'file found but not executable - chmod +x it' and attaches a hint that
    invites the model to seek local workarounds for a policy boundary (#2179).
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.token_path = self.tmp / "token"
        self.token_path.write_text("valid-token\n")

    def test_security_policy_blocked_returns_77_and_prints_rule(self):
        refusal_payload = {
            "code": "SECURITY_POLICY_BLOCKED",
            "message": "kubectl delete is not permitted.",
            "rule": "kubernetes.read-only",
            "verb": ["delete", "pod"],
            "local_render": False,
        }
        body = json.dumps(refusal_payload).encode("utf-8")
        error_file = io.BytesIO(body)
        http_error = urllib.error.HTTPError(
            url="http://proxy/v1/exec",
            code=403,
            msg="Forbidden",
            hdrs=http.client.HTTPMessage(),
            fp=error_file,
        )

        def fake_open(request, *args, **kwargs):
            raise http_error

        stderr = io.StringIO()
        environ = {
            "CREDENTIAL_PROXY_TOKEN_FILE": str(self.token_path),
            "KUBECONFIG": "",
            "HERMES_HOME": str(self.tmp),
        }
        with patch.dict("os.environ", environ, clear=False):
            with patch.object(credential_proxy_client, "open_broker_request", fake_open):
                with patch("sys.stderr", new=stderr):
                    exit_code = credential_proxy_client.execute(
                        "http://proxy", ["kubectl", "delete", "pod", "mypod"]
                    )

        self.assertEqual(77, exit_code)
        self.assertEqual(credential_proxy_client.EXIT_SECURITY_POLICY_BLOCKED, exit_code)
        err = stderr.getvalue()
        self.assertIn("kubectl delete is not permitted.", err)
        self.assertIn("policy rule: kubernetes.read-only", err)
        self.assertIn("policy verb: delete pod", err)
        self.assertNotIn("policy render:", err)

    def test_a_local_render_refusal_prints_the_render_line(self):
        refusal_payload = {
            "status": "blocked", "code": "SECURITY_POLICY_BLOCKED", "message": "refused",
            "rule": "kubernetes.read-only", "verb": ["create"], "local_render": True,
        }
        body = json.dumps(refusal_payload).encode("utf-8")
        stderr = io.StringIO()

        def fake_open(req, *args, **kwargs):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(body))

        environ = {
            "CREDENTIAL_PROXY_TOKEN_FILE": str(self.token_path),
            "KUBECONFIG": "",
            "HERMES_HOME": str(self.tmp),
        }
        with patch.dict("os.environ", environ, clear=False), patch.object(credential_proxy_client, "open_broker_request", fake_open):
            with patch("sys.stderr", new=stderr):
                credential_proxy_client.execute("http://proxy", ["kubectl", "create", "deployment", "x", "--image=y", "--dry-run=client"])
        err = stderr.getvalue()
        self.assertIn("policy verb: create\n", err)
        self.assertIn("policy render: local", err)

    def test_a_verb_word_cannot_break_the_line_protocol(self):
        refusal_payload = {
            "status": "blocked", "code": "SECURITY_POLICY_BLOCKED", "message": "refused",
            "rule": "kubernetes.read-only", "verb": ["rollout", "restart\npolicy render: local"],
            "local_render": False,
        }
        body = json.dumps(refusal_payload).encode("utf-8")
        stderr = io.StringIO()

        def fake_open(req, *args, **kwargs):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(body))

        environ = {
            "CREDENTIAL_PROXY_TOKEN_FILE": str(self.token_path),
            "KUBECONFIG": "",
            "HERMES_HOME": str(self.tmp),
        }
        with patch.dict("os.environ", environ, clear=False), patch.object(credential_proxy_client, "open_broker_request", fake_open):
            with patch("sys.stderr", new=stderr):
                credential_proxy_client.execute("http://proxy", ["kubectl", "rollout", "restart\npolicy render: local"])
        err = stderr.getvalue()
        self.assertIn("policy verb: rollout restart_policy_render:_local", err)
        self.assertNotIn("\npolicy render:", err)

    @unittest.skipUnless(HAS_HERMES, "needs hermes-agent runtime (tools.terminal_hints) importable")
    def test_exit_77_receives_no_runtime_execution_hint(self):
        # Hermes Agent's tools.terminal_hints defines hints for failed commands.
        # Exit 126 was annotated with:
        #   "Exit 126: the file was found but is not executable — `chmod +x` it..."
        # Exit 77 must not trigger any runtime hint on the refusal output.
        assert terminal_hints is not None
        output = "Command blocked for security reasons.\npolicy rule: kubernetes.read-only\n"
        hint = terminal_hints.annotate_failure("kubectl delete pod mypod", 77, output)
        self.assertIsNone(
            hint,
            f"Exit 77 must not produce an execution/file-permission hint, got: {hint!r}",
        )
        # Verify sabotage: 126 WOULD have produced the misleading hint
        hint_126 = terminal_hints.annotate_failure("kubectl delete pod mypod", 126, output)
        self.assertIsNotNone(hint_126)
        self.assertTrue(
            any(term in hint_126.lower() for term in ("chmod", "executable", "interpreter")),
            f"Exit 126 should advise execution or file permission resolution, got: {hint_126!r}",
        )


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


class ApiSessionTest(unittest.TestCase):
    """The requests-shaped session that sends a Google API read through the broker."""

    ENDPOINT = "http://agent-credential-proxy.kubeagents-system.svc.cluster.local:8765"
    URL = "https://monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries"

    class FakeHttp:
        def __init__(self):
            self.calls = []

        def get(self, url, *, params=None, headers=None, timeout=None):
            self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
            return "upstream-response"

    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        token_file = directory / "token"
        token_file.write_text("caller-token\n", encoding="utf-8")
        environ = {
            "CREDENTIAL_PROXY_URL": self.ENDPOINT + "/",
            "CREDENTIAL_PROXY_TOKEN_FILE": str(token_file),
        }
        patcher = patch.dict(os.environ, environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.http = self.FakeHttp()

    def test_get_rewrites_the_url_and_keeps_params_timeout_and_the_caller_token(self):
        session = credential_proxy_client.ApiSession(http=self.http)
        params = {"filter": 'metric.type="kubernetes.io/container/cpu/core_usage_time"', "pageSize": 1000}
        self.assertEqual("upstream-response", session.get(self.URL, params=params, timeout=120))
        self.assertEqual(
            [{
                "url": f"{self.ENDPOINT}/v1/gcp/monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries",
                "params": params,
                "headers": {"Authorization": "Bearer caller-token"},
                "timeout": 120,
            }],
            self.http.calls,
        )
        self.assertEqual(credential_proxy_client.authorization_headers(), self.http.calls[0]["headers"])

    def test_the_relay_prefix_is_the_shared_constant(self):
        session = credential_proxy_client.ApiSession(http=self.http)
        self.assertEqual(
            f"{self.ENDPOINT}{credential_proxy_client.API_RELAY_PREFIX}monitoring.googleapis.com/v3/x",
            session.relay_url("https://monitoring.googleapis.com/v3/x"),
        )

    def test_a_query_written_into_the_url_is_kept(self):
        session = credential_proxy_client.ApiSession(http=self.http)
        session.get(self.URL + "?pageToken=abc%3D%3D&pageSize=10")
        self.assertTrue(self.http.calls[0]["url"].endswith("/timeSeries?pageToken=abc%3D%3D&pageSize=10"))
        self.assertIsNone(self.http.calls[0]["params"])
        self.assertIsNone(self.http.calls[0]["timeout"])

    def test_an_explicit_endpoint_wins_and_loses_its_trailing_slash(self):
        session = credential_proxy_client.ApiSession("http://127.0.0.1:8765///", http=self.http)
        self.assertEqual(
            "http://127.0.0.1:8765/v1/gcp/monitoring.googleapis.com/v3/x",
            session.relay_url("https://monitoring.googleapis.com/v3/x"),
        )

    def test_no_broker_url_is_a_clear_error(self):
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": ""}):
            with self.assertRaises(RuntimeError) as raised:
                credential_proxy_client.ApiSession(http=self.http)
        self.assertIn("CREDENTIAL_PROXY_URL", str(raised.exception))

    def test_a_missing_caller_token_fails_the_call_not_the_construction(self):
        session = credential_proxy_client.ApiSession(http=self.http)
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_TOKEN_FILE": "/nonexistent/token"}):
            with self.assertRaises(credential_proxy_client.TokenUnavailable):
                session.get(self.URL)
        self.assertEqual([], self.http.calls)

    def test_the_default_transport_is_a_requests_session(self):
        try:
            import requests
        except ImportError:  # pragma: no cover - environment without requests
            self.skipTest("requests is not installed here; the sandbox image has it")
        session = credential_proxy_client.ApiSession()
        self.assertIsInstance(session._http, requests.Session)

    def test_the_module_imports_where_requests_is_absent(self):
        # `requests` is imported inside ApiSession, not at module scope: the
        # broker and every other importer of this module must not need it.
        script = (
            "import sys; sys.modules['requests'] = None; "
            "import credential_proxy_client as c; "
            "s = c.ApiSession('http://b', http=object()); print(s.relay_url('https://h/p'))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(Path(__file__).parent),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("http://b/v1/gcp/h/p", completed.stdout.strip())


def busy_error(bound, url="http://broker/v1/exec"):
    """A 503 CREDENTIAL_PROXY_BUSY refusal as the broker sends it, `bound` omitted when None."""
    reply = {"status": "busy", "code": credential_proxy_client.BUSY_CODE, "error": f"busy at {bound}"}
    if bound is not None:
        reply[credential_proxy_client.BUSY_BOUND_KEY] = bound
    return urllib.error.HTTPError(
        url=url, code=503, msg="Service Unavailable", hdrs=http.client.HTTPMessage(),
        fp=io.BytesIO(json.dumps(reply).encode("utf-8")),
    )


def unreachable():
    """What the opener raises when no connection to the broker could be made."""
    return urllib.error.URLError(credential_proxy_client.BrokerUnreachable("[Errno 111] Connection refused"))


class TestExecTierRouting(unittest.TestCase):
    """Exec-role calls go to the exec tier, fall back when it cannot be reached,
    and retry a refusal at admission on the shim's schedule, with or without
    exec routing. docs/designs/credential-broker-exec-tier.md §3.2, §4.3 and §12.
    The schedule itself (waits, deadline, Retry-After) is RetriesAdmissionRefusalsTest's."""

    EXEC = "http://agent-credential-proxy-exec-tier.kubeagents-system.svc.cluster.local:8765"
    STATEFUL = "http://agent-credential-proxy.kubeagents-system.svc.cluster.local:8765"
    OK = {"exitCode": 0, "stdout": "pods\n"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "token").write_text("caller-token\n", encoding="utf-8")
        self.environ = {
            "CREDENTIAL_PROXY_TOKEN_FILE": str(self.tmp / "token"),
            "KUBECONFIG": "",
            "HERMES_HOME": str(self.tmp),
        }
        self.sleeps = []

    def run_shim(self, answers, endpoint=EXEC, fallback=STATEFUL):
        """Send one kubectl through `execute`, answering each call from `answers` in turn."""
        calls = []
        answers = list(answers)

        def fake_open(request, *args, **kwargs):
            calls.append((request.full_url, json.loads(request.data)["requestId"]))
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return RecordingResponse(json.dumps(answer).encode("utf-8"))

        stderr = io.StringIO()
        with patch.dict("os.environ", self.environ, clear=False), \
                patch.object(credential_proxy_client, "open_broker_request", fake_open), \
                patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=stderr):
            code = credential_proxy_client.execute(
                endpoint, ["kubectl", "get", "pods"], fallback=fallback, sleep_fn=self.sleeps.append
            )
        self.assertEqual([], answers, "every planned answer is used")
        return code, calls, stderr.getvalue()

    def test_the_exec_tier_is_the_target_when_its_url_is_set(self):
        cases = {
            "both": ({"CREDENTIAL_PROXY_EXEC_URL": self.EXEC, "CREDENTIAL_PROXY_URL": self.STATEFUL}, (self.EXEC, self.STATEFUL)),
            "stateful only": ({"CREDENTIAL_PROXY_EXEC_URL": "", "CREDENTIAL_PROXY_URL": self.STATEFUL}, (self.STATEFUL, None)),
            "blank exec": ({"CREDENTIAL_PROXY_EXEC_URL": "  ", "CREDENTIAL_PROXY_URL": self.STATEFUL}, (self.STATEFUL, None)),
            "exec only": ({"CREDENTIAL_PROXY_EXEC_URL": self.EXEC}, (self.EXEC, None)),
        }
        for name, (environ, expected) in cases.items():
            with self.subTest(name), patch.dict("os.environ", environ, clear=False):
                if "CREDENTIAL_PROXY_URL" not in environ:
                    os.environ.pop("CREDENTIAL_PROXY_URL", None)
                self.assertEqual(expected, credential_proxy_client.exec_endpoints())

    def test_an_unreachable_exec_tier_falls_back_with_one_line_and_the_same_request(self):
        code, calls, stderr = self.run_shim([unreachable(), self.OK])
        self.assertEqual(0, code)
        self.assertEqual([self.EXEC + "/v1/exec", self.STATEFUL + "/v1/exec"], [url for url, _ in calls])
        self.assertEqual(1, len({request_id for _, request_id in calls}), "the fallback is the same request")
        self.assertEqual(1, stderr.count(credential_proxy_client.EXEC_FALLBACK_NOTICE), stderr)

    def test_with_no_fallback_an_unreachable_broker_is_reported_as_before(self):
        code, calls, stderr = self.run_shim([unreachable()], fallback=None)
        self.assertEqual(1, code)
        self.assertEqual(1, len(calls))
        self.assertIn("credential proxy unavailable", stderr)

    def test_a_failure_after_the_connection_is_made_does_not_fall_back(self):
        """A send that fails part way may have reached the broker."""
        code, calls, stderr = self.run_shim([urllib.error.URLError(BrokenPipeError("broken pipe"))])
        self.assertEqual(1, code)
        self.assertEqual(1, len(calls))
        self.assertIn("credential proxy unavailable", stderr)

    def test_each_retryable_bound_is_retried_on_a_new_connection_after_a_wait(self):
        for bound in sorted(credential_proxy_client.RETRYABLE_BUSY_BOUNDS):
            with self.subTest(bound=bound):
                self.sleeps.clear()
                code, calls, _ = self.run_shim([busy_error(bound), self.OK])
                self.assertEqual(0, code)
                self.assertEqual([self.EXEC + "/v1/exec"] * 2, [url for url, _ in calls])
                self.assertEqual(1, len({request_id for _, request_id in calls}))
                self.assertEqual(1, len(self.sleeps), "one jittered wait before the retry")

    def test_a_refusal_under_any_other_bound_is_not_retried(self):
        """A reply with no bound is an older broker's and is retried
        (RetriesAdmissionRefusalsTest); a bound the shim does not know, or one
        that is not a string, is reported."""
        for bound in ("session", "refresh-lock", "other", "scope", ["slot"], {"slot": 1}):
            with self.subTest(bound=bound):
                code, calls, stderr = self.run_shim([busy_error(bound)])
                self.assertEqual(1, code)
                self.assertEqual(1, len(calls))
                self.assertIn(f"busy at {bound}", stderr)
                self.assertEqual([], self.sleeps)

    def test_without_exec_routing_a_busy_refusal_is_retried_on_the_same_schedule(self):
        """With no exec tier, or a named endpoint, there is one stateful pod:
        the wait is what lets it admit the command once a slot frees."""
        code, calls, stderr = self.run_shim([busy_error("slot"), self.OK], endpoint=self.STATEFUL, fallback=None)
        self.assertEqual(0, code, stderr)
        self.assertEqual([self.STATEFUL + "/v1/exec"] * 2, [url for url, _ in calls])
        self.assertEqual(1, len(self.sleeps))
        self.assertIn("credential proxy busy (HTTP 503): busy at slot; retrying in", stderr)

    def test_a_drop_while_the_refusal_is_read_is_reported_not_raised(self):
        class TornBody(io.BytesIO):
            def read(self, *args):
                raise http.client.IncompleteRead(b'{"status": "bu')

        torn = urllib.error.HTTPError(
            url="http://broker/v1/exec", code=503, msg="Service Unavailable",
            hdrs=http.client.HTTPMessage(), fp=TornBody(),
        )
        code, calls, stderr = self.run_shim([torn])
        self.assertEqual(1, code)
        self.assertEqual(1, len(calls))
        self.assertIn("the connection dropped while the refusal was read", stderr)

    def test_a_second_refusal_is_retried_again_until_the_count_is_spent(self):
        code, calls, stderr = self.run_shim([busy_error("slot"), busy_error("slot"), self.OK])
        self.assertEqual(0, code, stderr)
        self.assertEqual(3, len(calls))
        self.assertEqual(2, len(self.sleeps))
        self.sleeps.clear()
        refusals = [busy_error("slot") for _ in range(credential_proxy_client.CREDENTIAL_PROXY_CLIENT_MAX_RETRIES + 1)]
        code, calls, stderr = self.run_shim(refusals)
        self.assertEqual(1, code)
        self.assertEqual(len(refusals), len(calls))
        self.assertEqual(credential_proxy_client.CREDENTIAL_PROXY_CLIENT_MAX_RETRIES, len(self.sleeps))
        self.assertIn(f"credential proxy throttled: gave up after {len(refusals)} attempts (HTTP 503): busy at slot", stderr)

    def test_a_fallback_is_not_a_retry_and_does_not_count_against_the_schedule(self):
        code, calls, stderr = self.run_shim([unreachable(), busy_error("slot"), self.OK])
        self.assertEqual(0, code)
        self.assertEqual(
            [self.EXEC + "/v1/exec", self.STATEFUL + "/v1/exec", self.STATEFUL + "/v1/exec"],
            [url for url, _ in calls],
        )
        self.assertEqual(1, len(self.sleeps), "the fallback waits for nothing; the retry does")
        self.assertIn("(attempt 2)", stderr, "the fallback is not an attempt")
        self.sleeps.clear()
        code, calls, _ = self.run_shim([busy_error("slot"), unreachable(), self.OK])
        self.assertEqual(0, code, "the retry may still fall back: nothing ran")
        self.assertEqual(3, len(calls))
        self.assertEqual(1, len(self.sleeps))
        self.sleeps.clear()
        refusals = [busy_error("budget") for _ in range(credential_proxy_client.CREDENTIAL_PROXY_CLIENT_MAX_RETRIES + 1)]
        code, calls, stderr = self.run_shim([unreachable(), *refusals])
        self.assertEqual(1, code)
        self.assertEqual(len(refusals) + 1, len(calls))
        self.assertIn(f"gave up after {len(refusals)} attempts", stderr)

    def test_a_503_that_is_not_the_brokers_is_not_retried(self):
        envoy = urllib.error.HTTPError(
            url="http://broker/v1/exec", code=503, msg="Service Unavailable",
            hdrs=http.client.HTTPMessage(), fp=io.BytesIO(b"<html>upstream connect error</html>"),
        )
        code, calls, stderr = self.run_shim([envoy])
        self.assertEqual(1, code)
        self.assertEqual(1, len(calls))
        self.assertIn("non-JSON response", stderr)

    def test_a_policy_block_still_exits_77(self):
        refusal = {"code": "SECURITY_POLICY_BLOCKED", "message": "no", "rule": "kubernetes.read-only"}
        blocked = urllib.error.HTTPError(
            url="http://broker/v1/exec", code=403, msg="Forbidden", hdrs=http.client.HTTPMessage(),
            fp=io.BytesIO(json.dumps(refusal).encode("utf-8")),
        )
        code, calls, stderr = self.run_shim([blocked])
        self.assertEqual(credential_proxy_client.EXIT_SECURITY_POLICY_BLOCKED, code)
        self.assertEqual(1, len(calls))
        self.assertIn("policy rule: kubernetes.read-only", stderr)

    def test_a_busy_body_on_another_status_is_not_retried(self):
        """Only the broker's admission refusal, a 503, is known not to have run."""
        reply = {"status": "busy", "code": credential_proxy_client.BUSY_CODE, "bound": "slot", "error": "odd"}
        odd = urllib.error.HTTPError(
            url="http://broker/v1/exec", code=500, msg="Internal Server Error", hdrs=http.client.HTTPMessage(),
            fp=io.BytesIO(json.dumps(reply).encode("utf-8")),
        )
        code, calls, stderr = self.run_shim([odd])
        self.assertEqual(1, code)
        self.assertEqual(1, len(calls))
        self.assertIn("odd", stderr)

    def test_the_two_new_lines_keep_kubectls_failure_classifiers_right(self):
        """A fallback line must not hide a real cluster connection failure that
        follows it, nor look like one; a dropped connection is the shim's own."""
        import cluster_agent_profile

        notice = credential_proxy_client.EXEC_FALLBACK_NOTICE
        self.assertIsNone(cluster_agent_profile.NOT_A_CONNECTION_FAILURE_RE.search(notice))
        self.assertIsNone(cluster_agent_profile.CONNECTIVITY_FAILURE_RE.search(notice))
        _, _, stderr = self.run_shim([ConnectionResetError(104, "Connection reset by peer")])
        self.assertIsNotNone(cluster_agent_profile.NOT_A_CONNECTION_FAILURE_RE.search(stderr), stderr)

    def test_a_connection_dropped_after_the_request_was_sent_is_never_sent_again(self):
        """The command may have run: no retry, and no fallback, whichever broker it was."""
        drops = (
            http.client.RemoteDisconnected("Remote end closed connection without response"),
            http.client.IncompleteRead(b"{"),
            ConnectionResetError(104, "Connection reset by peer"),
        )
        for drop in drops:
            with self.subTest(drop=type(drop).__name__):
                code, calls, stderr = self.run_shim([drop])
                self.assertEqual(1, code)
                self.assertEqual(1, len(calls))
                self.assertIn("the command may have run", stderr)

    def test_a_malformed_url_is_reported_not_raised_and_not_as_a_drop_with_no_fallback(self):
        """A bad port is raised bare by urllib, before a socket opens, and a URL
        with no scheme fails while the request is built: nothing was sent. With
        no stateful broker to fall back on, the line says so."""
        for url in ("http://broker:abc", "agent-credential-proxy-exec-tier"):
            with self.subTest(url=url):
                stderr = io.StringIO()
                with patch.dict("os.environ", self.environ, clear=False), \
                        patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=stderr):
                    code = credential_proxy_client.execute(url, ["kubectl", "get", "pods"], fallback=None)
                self.assertEqual(1, code)
                self.assertIn("credential proxy unavailable", stderr.getvalue())
                self.assertNotIn("may have run", stderr.getvalue())


class TestExecUrlShape(unittest.TestCase):
    """A malformed CREDENTIAL_PROXY_EXEC_URL is ignored for the stateful broker,
    with one line, rather than failing every exec-role call (#4534)."""

    STATEFUL = "http://agent-credential-proxy.kubeagents-system.svc.cluster.local:8765"
    BAD = {
        "a port that is not a number": "http://broker:abc",
        "a bare Service name": "agent-credential-proxy-exec-tier",
        "no scheme, with a port": "broker:8765",
        "https": "https://agent-credential-proxy-exec-tier:8765",
        "no host": "http://",
        "a port out of range": "http://broker:99999",
        "a bracketed host that is not IPv6": "http://[::1",
        "a space in the host": "http://bro ker:8765",
        "a tab in the host": "http://bro\tker:8765",
        "an escaped tab in the host": "http://bro%09ker:8765",
        "an empty label in the host": "http://name..svc:8765",
        "a label longer than 63 characters": "http://" + "a" * 64 + ".svc:8765",
        "a space in the path": "http://broker:8765/a b",
        "a query": "http://broker:8765?x=1",
        "a fragment": "http://broker:8765#f",
        "an empty query": "http://broker:8765?",
        "an empty fragment": "http://broker:8765#",
        "a host that is not latin-1": "http://brok\u00e9r\u4e2d:8765",
    }

    def resolve(self, exec_url, stateful=STATEFUL):
        environ = {"CREDENTIAL_PROXY_EXEC_URL": exec_url}
        if stateful:
            environ["CREDENTIAL_PROXY_URL"] = stateful
        stderr = io.StringIO()
        with patch.dict("os.environ", environ, clear=False), \
                patch.object(credential_proxy_client, "_exec_url_notice_written", False), \
                patch("sys.stderr", new=stderr):
            if not stateful:
                os.environ.pop("CREDENTIAL_PROXY_URL", None)
            resolved = credential_proxy_client.exec_endpoints()
        return resolved, stderr.getvalue()

    def test_each_malformed_url_is_ignored_for_the_stateful_broker_with_one_line(self):
        for name, url in self.BAD.items():
            with self.subTest(name):
                resolved, stderr = self.resolve(url)
                self.assertEqual((self.STATEFUL, None), resolved)
                self.assertEqual(1, stderr.count("CREDENTIAL_PROXY_EXEC_URL ignored ("), stderr)
                self.assertIn("commands go to the stateful broker", stderr)

    def test_a_valid_url_is_used_with_no_line(self):
        for url in ("http://agent-credential-proxy-exec-tier.kubeagents-system.svc.cluster.local:8765",
                    "http://agent-credential-proxy-exec-tier", "HTTP://exec:8765", "http://[::1]:8765",
                    "http://exec:8765/prefix"):
            with self.subTest(url=url):
                resolved, stderr = self.resolve(url)
                self.assertEqual((url, self.STATEFUL), resolved)
                self.assertEqual("", stderr)

    def test_the_line_is_written_once_per_process(self):
        stderr = io.StringIO()
        environ = {"CREDENTIAL_PROXY_EXEC_URL": "agent-credential-proxy-exec-tier", "CREDENTIAL_PROXY_URL": self.STATEFUL}
        with patch.dict("os.environ", environ, clear=False), \
                patch.object(credential_proxy_client, "_exec_url_notice_written", False), \
                patch("sys.stderr", new=stderr):
            for _ in range(3):
                self.assertEqual((self.STATEFUL, None), credential_proxy_client.exec_endpoints())
        self.assertEqual(1, stderr.getvalue().count("ignored"), stderr.getvalue())

    def test_with_no_stateful_broker_a_malformed_url_is_kept_as_before(self):
        """Nothing to use instead: the call reports the URL when it is made."""
        for name, url in self.BAD.items():
            with self.subTest(name):
                resolved, stderr = self.resolve(url, stateful=None)
                self.assertEqual((url, None), resolved)
                self.assertEqual("", stderr)

    def test_a_named_endpoint_writes_no_malformed_url_line(self):
        """The line says where commands go; with --endpoint they go there."""
        stderr = io.StringIO()
        environ = {"CREDENTIAL_PROXY_EXEC_URL": "http://broker:abc", "CREDENTIAL_PROXY_URL": self.STATEFUL}
        with patch.dict("os.environ", environ, clear=False), \
                patch.object(credential_proxy_client, "_exec_url_notice_written", False), \
                patch("sys.stderr", new=stderr), \
                patch("sys.argv", ["credential_proxy_client.py", "--endpoint", "http://named:8765", "kubectl", "get", "pods"]):
            args = credential_proxy_client.parse_args()
        self.assertEqual("http://named:8765", args.endpoint)
        self.assertEqual("", stderr.getvalue())

    def test_with_no_url_at_all_an_endpoint_is_required(self):
        with patch.dict("os.environ", {}, clear=False), \
                patch("sys.argv", ["credential_proxy_client.py", "kubectl", "get", "pods"]), \
                patch("sys.stderr", new=io.StringIO()):
            os.environ.pop("CREDENTIAL_PROXY_URL", None)
            os.environ.pop("CREDENTIAL_PROXY_EXEC_URL", None)
            with self.assertRaises(SystemExit):
                credential_proxy_client.parse_args()

    def test_the_line_keeps_kubectls_failure_classifiers_right(self):
        import cluster_agent_profile

        for name, url in self.BAD.items():
            with self.subTest(name):
                _, stderr = self.resolve(url)
                self.assertIsNone(cluster_agent_profile.NOT_A_CONNECTION_FAILURE_RE.search(stderr), stderr)
                self.assertIsNone(cluster_agent_profile.CONNECTIVITY_FAILURE_RE.search(stderr), stderr)


class TestExecPreSendFallback(unittest.TestCase):
    """A URL that fails before a byte is sent falls back whatever its shape,
    so a shape the check above does not know still reaches the stateful broker."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        (self.tmp / "token").write_text("caller-token\n", encoding="utf-8")
        self.environ = {"CREDENTIAL_PROXY_TOKEN_FILE": str(self.tmp / "token"), "KUBECONFIG": "", "HERMES_HOME": str(self.tmp)}

    def run_with(self, endpoint):
        calls = []
        real_open = credential_proxy_client.open_broker_request

        def open_or_answer(request, *args, **kwargs):
            if request.full_url.startswith("http://stateful:8765"):
                calls.append(request.full_url)
                return RecordingResponse(json.dumps({"exitCode": 0, "stdout": "ok\n"}).encode("utf-8"))
            return real_open(request, *args, **kwargs)

        stderr = io.StringIO()
        with patch.dict("os.environ", self.environ, clear=False), \
                patch.object(credential_proxy_client, "open_broker_request", open_or_answer), \
                patch("sys.stdout", new=io.StringIO()), patch("sys.stderr", new=stderr):
            code = credential_proxy_client.execute(endpoint, ["kubectl", "get", "pods"], fallback="http://stateful:8765")
        return code, calls, stderr.getvalue()

    def test_each_pre_send_failure_falls_back_once(self):
        for name, url in {
            "no scheme": "exec-tier",
            "a space in the host": "http://bro ker:8765",
            "a space in the path": "http://broker:8765/a b",
            "an empty label in the host": "http://name..svc:8765",
            "a host that is not latin-1": "http://brok\u00e9r\u4e2d:8765",
        }.items():
            with self.subTest(name):
                code, calls, stderr = self.run_with(url)
                self.assertEqual(0, code, stderr)
                self.assertEqual(["http://stateful:8765/v1/exec"], calls)
                self.assertEqual(1, stderr.count(credential_proxy_client.EXEC_FALLBACK_NOTICE), stderr)


class TestBrokerUnreachable(unittest.TestCase):
    """The one exact signal that no request reached the broker."""

    def test_a_failed_connect_raises_it_with_the_same_message(self):
        def refuse(self):
            raise ConnectionRefusedError(111, "Connection refused")

        connection = credential_proxy_client.BrokerConnection("broker", 8765)
        with patch.object(credential_proxy_client.http.client.HTTPConnection, "connect", refuse):
            with self.assertRaises(credential_proxy_client.BrokerUnreachable) as caught:
                connection.connect()
        self.assertIsInstance(caught.exception, OSError)
        self.assertIn("Connection refused", str(caught.exception))

    def test_the_opener_wraps_it_in_a_url_error_as_before(self):
        """A caller that reads only the URLError (vcs, workspaces) sees no change."""
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        request = urllib.request.Request(f"http://127.0.0.1:{port}/v1/exec", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.URLError) as caught:
            credential_proxy_client.open_broker_request(request)
        self.assertIsInstance(caught.exception.reason, credential_proxy_client.BrokerUnreachable)


class TestApiSessionExecTier(unittest.TestCase):
    """The Cloud API relay is an exec-tier route; its GETs fall back on the stateful broker."""

    EXEC = "http://exec-tier:8765"
    STATEFUL = "http://stateful:8765"
    URL = "https://container.googleapis.com/v1/projects/p/locations/-/clusters"

    class FlakyHttp:
        def __init__(self, unreachable_hosts=()):
            self.calls = []
            self.unreachable_hosts = set(unreachable_hosts)

        def get(self, url, *, params=None, headers=None, timeout=None):
            self.calls.append(url)
            if urllib.parse.urlsplit(url).netloc in self.unreachable_hosts:
                raise ConnectionError(f"cannot connect to {url}")
            return "upstream-response"

    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        (directory / "token").write_text("caller-token\n", encoding="utf-8")
        patcher = patch.dict(os.environ, {
            "CREDENTIAL_PROXY_URL": self.STATEFUL,
            "CREDENTIAL_PROXY_EXEC_URL": self.EXEC,
            "CREDENTIAL_PROXY_TOKEN_FILE": str(directory / "token"),
        })
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_exec_tier_serves_the_relay_when_its_url_is_set(self):
        http_ = self.FlakyHttp()
        credential_proxy_client.ApiSession(http=http_).get(self.URL)
        self.assertTrue(http_.calls[0].startswith(self.EXEC + credential_proxy_client.API_RELAY_PREFIX))

    def test_a_malformed_exec_url_leaves_the_relay_on_the_stateful_broker(self):
        """`requests` raises MissingSchema for a URL with no scheme, which is not a
        connection error, so without the check the relay never fell back."""
        http_ = self.FlakyHttp()
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_EXEC_URL": "exec-tier"}), \
                patch.object(credential_proxy_client, "_exec_url_notice_written", False), \
                patch("sys.stderr", new=io.StringIO()):
            credential_proxy_client.ApiSession(http=http_).get(self.URL)
        self.assertTrue(http_.calls[0].startswith(self.STATEFUL + credential_proxy_client.API_RELAY_PREFIX), http_.calls)

    def test_a_url_requests_refuses_before_sending_moves_the_relay_to_the_fallback(self):
        """A host `requests` refuses while it prepares the request (a leading
        `*`) is raised before a byte is sent, like a connection error."""
        try:
            import requests
        except ImportError:
            self.skipTest("requests is not installed")
        http_ = requests.Session()
        calls = []

        def get(url, **kwargs):
            calls.append(url)
            if url.startswith(self.STATEFUL):
                return "upstream-response"
            return requests.Session.get(http_, url, **kwargs)

        with patch.dict(os.environ, {"CREDENTIAL_PROXY_EXEC_URL": "http://*.svc:8765"}), \
                patch.object(http_, "get", side_effect=get), patch("sys.stderr", new=io.StringIO()):
            self.assertEqual("upstream-response", credential_proxy_client.ApiSession(http=http_).get(self.URL))
        self.assertEqual(["*.svc:8765", "stateful:8765"], [urllib.parse.urlsplit(url).netloc for url in calls])

    def test_an_unreachable_exec_tier_falls_back_once_and_stays_there(self):
        http_ = self.FlakyHttp(unreachable_hosts={"exec-tier:8765"})
        session = credential_proxy_client.ApiSession(http=http_)
        stderr = io.StringIO()
        with patch("sys.stderr", new=stderr):
            self.assertEqual("upstream-response", session.get(self.URL))
            self.assertEqual("upstream-response", session.get(self.URL))
        self.assertEqual(
            ["exec-tier:8765", "stateful:8765", "stateful:8765"],
            [urllib.parse.urlsplit(url).netloc for url in http_.calls],
        )
        self.assertEqual(1, stderr.getvalue().count(credential_proxy_client.EXEC_FALLBACK_NOTICE))

    def test_the_exec_attempt_gets_the_shims_connect_bound_and_the_callers_read_timeout(self):
        seen = []

        class Recording(self.FlakyHttp):
            def get(inner, url, *, params=None, headers=None, timeout=None):
                seen.append((urllib.parse.urlsplit(url).netloc, timeout))
                return super().get(url, params=params, headers=headers, timeout=timeout)

        bound = credential_proxy_client.BROKER_CONNECT_TIMEOUT_SECONDS
        http_ = Recording(unreachable_hosts={"exec-tier:8765"})
        session = credential_proxy_client.ApiSession(http=http_)
        with patch("sys.stderr", new=io.StringIO()):
            session.get(self.URL, timeout=120)
        self.assertEqual(("exec-tier:8765", (bound, 120)), seen[0])
        self.assertEqual("stateful:8765", seen[1][0])
        self.assertTrue(119 < seen[1][1] <= 120, "the fallback gets what the exec attempt left")
        self.assertEqual((bound, None), credential_proxy_client._exec_connect_bound(None))
        self.assertEqual((3, 50), credential_proxy_client._exec_connect_bound((3, 50)))

    def test_readers_sharing_a_session_all_fall_back_when_the_exec_tier_is_down(self):
        """Fleet collectors share one session across a thread pool: every reader
        must land on the stateful broker, and the session must stay usable."""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        barrier = threading.Barrier(8)

        class Racing(self.FlakyHttp):
            def get(inner, url, *, params=None, headers=None, timeout=None):
                if urllib.parse.urlsplit(url).netloc == "exec-tier:8765":
                    barrier.wait(timeout=10)
                return super().get(url, params=params, headers=headers, timeout=timeout)

        for _ in range(20):
            barrier.reset()
            http_ = Racing(unreachable_hosts={"exec-tier:8765"})
            session = credential_proxy_client.ApiSession(http=http_)
            with patch("sys.stderr", new=io.StringIO()) as stderr, ThreadPoolExecutor(8) as pool:
                results = list(pool.map(lambda _: session.get(self.URL), range(8)))
            self.assertEqual(["upstream-response"] * 8, results)
            self.assertEqual(1, stderr.getvalue().count(credential_proxy_client.EXEC_FALLBACK_NOTICE))
            self.assertEqual("upstream-response", session.get(self.URL))

    def test_a_requests_connection_error_falls_back(self):
        try:
            import requests
        except ImportError:
            self.skipTest("requests is not installed here; the sandbox image has it")

        class RequestsLike(self.FlakyHttp):
            def get(inner, url, *, params=None, headers=None, timeout=None):
                inner.calls.append(url)
                if urllib.parse.urlsplit(url).netloc == "exec-tier:8765":
                    raise requests.exceptions.ConnectionError("Max retries exceeded")
                return "upstream-response"

        session = credential_proxy_client.ApiSession(http=RequestsLike())
        with patch("sys.stderr", new=io.StringIO()):
            self.assertEqual("upstream-response", session.get(self.URL))

    def test_the_fallback_gets_only_the_time_the_exec_attempt_left(self):
        left = credential_proxy_client._time_left
        self.assertIsNone(left(None, 5.0))
        self.assertEqual(15.0, left(25.0, 10.0))
        self.assertEqual((10, 5.0), left((10, 15), 10.0))
        self.assertEqual((10, None), left((10, None), 10.0))
        self.assertIs(False, left(25.0, 24.5))
        self.assertIs(False, left((10, 15), 14.5))

    def test_a_fallback_with_no_time_left_raises_the_error(self):
        http_ = self.FlakyHttp(unreachable_hosts={"exec-tier:8765"})
        session = credential_proxy_client.ApiSession(http=http_)
        clock = iter([100.0, 130.0])
        with patch.object(credential_proxy_client.time, "monotonic", lambda: next(clock)), \
                patch("sys.stderr", new=io.StringIO()):
            with self.assertRaises(ConnectionError):
                session.get(self.URL, timeout=25)
        self.assertEqual(1, len(http_.calls))

    def test_the_built_session_ignores_a_proxy_from_the_environment(self):
        try:
            import requests  # noqa: F401
        except ImportError:
            self.skipTest("requests is not installed here; the sandbox image has it")
        self.assertFalse(credential_proxy_client.ApiSession()._http.trust_env)

    def test_a_named_endpoint_has_no_fallback(self):
        http_ = self.FlakyHttp(unreachable_hosts={"exec-tier:8765"})
        session = credential_proxy_client.ApiSession(self.EXEC, http=http_)
        with self.assertRaises(ConnectionError):
            session.get(self.URL)
        self.assertEqual(1, len(http_.calls))


class TestShimEntryRouting(unittest.TestCase):
    """The shim run as `kubectl`, and with `--endpoint`, against brokers on loopback."""

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading

        cls.answers = []
        cls.requests = []

        class Broker(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                cls.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                status, body = cls.answers.pop(0) if cls.answers else (200, {"exitCode": 0, "stdout": "ok\n"})
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Broker)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        import socket

        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.shim = self.root / "credential-proxy-exec"
        shutil.copy(credential_proxy_client.__file__, self.shim)
        (self.root / "kubectl").symlink_to(self.shim)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.closed = f"http://127.0.0.1:{probe.getsockname()[1]}"
        self.live = f"http://127.0.0.1:{self.server.server_address[1]}"
        type(self).answers.clear()
        type(self).requests.clear()

    def run_shim(self, args, environ):
        env = {"PATH": os.environ["PATH"], "HERMES_HOME": str(self.root / "home"), **environ}
        return subprocess.run(
            [sys.executable, *args], env=env, capture_output=True, text=True, timeout=60
        )

    def test_kubectl_falls_back_from_an_unreachable_exec_tier_and_retries_a_busy_slot(self):
        type(self).answers.append((503, {"status": "busy", "code": "CREDENTIAL_PROXY_BUSY", "bound": "slot", "error": "busy"}))
        completed = self.run_shim(
            [str(self.root / "kubectl"), "get", "pods"],
            {"CREDENTIAL_PROXY_EXEC_URL": self.closed, "CREDENTIAL_PROXY_URL": self.live},
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual("ok\n", completed.stdout)
        self.assertEqual(1, completed.stderr.count(credential_proxy_client.EXEC_FALLBACK_NOTICE), completed.stderr)
        self.assertEqual(2, len(self.requests), "the busy slot was retried once")
        self.assertEqual(1, len({r["requestId"] for r in self.requests}))

    def test_a_proxy_in_the_environment_is_not_used_to_reach_the_broker(self):
        """A proxy in between would accept every connect, so an unreachable broker
        could not be told apart from one that dropped the request."""
        completed = self.run_shim(
            [str(self.root / "kubectl"), "get", "pods"],
            {"CREDENTIAL_PROXY_URL": self.live, "http_proxy": self.closed, "HTTP_PROXY": self.closed},
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(1, len(self.requests))

    def test_a_named_endpoint_wins_over_the_exec_tier(self):
        completed = self.run_shim(
            [str(self.shim), "--endpoint", self.live, "kubectl", "get", "pods"],
            {"CREDENTIAL_PROXY_EXEC_URL": self.closed},
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertNotIn("exec tier", completed.stderr)
        named = self.run_shim(
            [str(self.shim), "--endpoint", self.closed, "kubectl", "get", "pods"],
            {"CREDENTIAL_PROXY_EXEC_URL": self.closed, "CREDENTIAL_PROXY_URL": self.live},
        )
        self.assertEqual(1, named.returncode)
        self.assertIn("credential proxy unavailable", named.stderr, "no fallback for a named endpoint")


@unittest.skipUnless(Path("/proc/self/stat").exists() and shutil.which("bash"), "needs /proc and bash")
class TestRealShellCommandLines(unittest.TestCase):
    """The shell pin through real processes, not a patched ancestry.

    Each case is one `bash -c` command line, as Hermes sends every terminal
    command, running the shim as `gcloud` and `kubectl` through symlinks the
    way the sandbox image installs it, against a broker on loopback. The
    broker answers a get-credentials with a kubeconfig for the cluster named,
    and a kubectl with `<last argument> -> <kubeconfigContext or HOST>`, so
    stdout says which cluster each kubectl would have reached.
    """

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading

        cls.requests = []

        class Broker(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                cls.requests.append(payload)
                argv = payload["argv"]
                if "get-credentials" in argv:
                    cluster = argv[argv.index("get-credentials") + 1]
                    context = f"gke_acme-evals_us-central1-a_{cluster}"
                    body = {"exitCode": 0, "kubeconfig":
                            f"apiVersion: v1\nkind: Config\ncurrent-context: {context}\n"}
                else:
                    reached = payload.get("kubeconfigContext", "HOST")
                    body = {"exitCode": 0, "stdout": f"{argv[-1]} -> {reached}\n"}
                data = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Broker)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "credential-proxy-exec"
        shutil.copy(credential_proxy_client.__file__, shim)
        shim.chmod(0o755)
        for name in ("kubectl", "gcloud"):
            (bin_dir / name).symlink_to(shim)
        self.home = self.root / "home"
        self.env = {
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "HERMES_HOME": str(self.home),
            "CREDENTIAL_PROXY_URL": f"http://127.0.0.1:{self.server.server_address[1]}",
        }

    def line(self, script):
        """Run one command line; return the kubectl answers, in order."""
        script = script.replace("FETCH", "gcloud container clusters get-credentials")
        completed = subprocess.run(
            ["bash", "-c", script], env=self.env, capture_output=True, text=True, timeout=60
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        return completed.stdout.split("\n")[:-1]

    A = "gke_acme-evals_us-central1-a_seeded-a"
    B = "gke_acme-evals_us-central1-a_seeded-b"

    def test_the_shim_will_not_run_as_gh_or_git(self):
        # An image that kept a stray symlink would otherwise forward `git
        # commit` to a broker whose filesystem is not this one.
        shim = self.root / "bin" / "credential-proxy-exec"
        before = len(self.requests)
        for name in ("gh", "git"):
            with self.subTest(name=name):
                (self.root / "bin" / name).symlink_to(shim)
                completed = subprocess.run(
                    ["bash", "-c", f"{name} status"],
                    env=self.env, capture_output=True, text=True, timeout=60,
                )
                self.assertNotEqual(0, completed.returncode)
        self.assertEqual(before, len(self.requests))

    def test_and_chained_kubectl_reaches_the_fetched_cluster(self):
        self.assertEqual([f"m -> {self.A}"], self.line("FETCH seeded-a && kubectl get pods m"))

    def test_semicolon_and_newline_forms_too(self):
        self.assertEqual([f"m -> {self.A}"], self.line("FETCH seeded-a ; kubectl get pods m"))
        self.assertEqual([f"m -> {self.A}"], self.line("FETCH seeded-a\nkubectl get pods m"))

    def test_hermes_eval_wrapper(self):
        # base.py runs the model's text through `eval` inside the same shell.
        self.assertEqual(
            [f"m -> {self.A}"],
            self.line("builtin cd -- /tmp || exit 126\neval 'FETCH seeded-a && kubectl get pods m'"
                      .replace("FETCH", "gcloud container clusters get-credentials")),
        )

    def test_command_substitution_pipeline_and_loop(self):
        out = self.line(
            'FETCH seeded-a && echo "$(kubectl get pods s)" && kubectl get pods p | cat'
            " && for i in 1 2; do kubectl get pods l$i; done"
        )
        self.assertEqual([f"s -> {self.A}", f"p -> {self.A}", f"l1 -> {self.A}", f"l2 -> {self.A}"], out)

    def test_the_next_command_line_reads_the_host(self):
        self.line("FETCH seeded-a && kubectl get pods m")
        self.assertEqual(["n -> HOST"], self.line("kubectl get pods n"))

    def test_before_the_fetch_the_line_reads_the_host(self):
        self.assertEqual(
            ["before -> HOST", f"after -> {self.A}"],
            self.line("kubectl get pods before; FETCH seeded-a && kubectl get pods after"),
        )

    def test_parallel_subshells_do_not_race(self):
        # Interleaved on purpose: b's fetch lands between a's fetch and a's
        # kubectl, which is the order a shared pin gets wrong.
        for _ in range(3):
            out = self.line(
                "(FETCH seeded-a && sleep 0.6 && kubectl get pods a) &"
                " (sleep 0.2 && FETCH seeded-b && kubectl get pods b) & wait"
            )
            self.assertEqual(sorted([f"a -> {self.A}", f"b -> {self.B}"]), sorted(out))

    def test_a_subshells_fetch_does_not_leak_to_its_parent(self):
        self.assertEqual(["m -> HOST"], self.line("(FETCH seeded-a && true) ; kubectl get pods m"))

    def test_a_one_command_subshell_is_the_parent_fetching(self):
        # bash execs a subshell's only command, so gcloud runs as the parent
        # shell's child: the same process tree as `FETCH seeded-a ; kubectl`.
        self.assertEqual([f"m -> {self.A}"], self.line("(FETCH seeded-a) ; kubectl get pods m"))
        # Backgrounded too: a one-command `( ... ) &` pins the line's shell.
        self.assertEqual([f"m -> {self.A}"], self.line("(FETCH seeded-a) & wait; kubectl get pods m"))

    def test_a_kubectl_under_a_process_that_is_not_a_shell(self):
        helper = "python3 -c 'import subprocess; subprocess.run([\"kubectl\", \"get\", \"pods\", \"p\"])'"
        out = self.line(
            "FETCH seeded-a && timeout 30 kubectl get pods t"
            f" && echo x | xargs kubectl get pods && {helper}; true"
        )
        self.assertEqual([f"t -> {self.A}", f"x -> {self.A}", f"p -> {self.A}"], out)

    def test_a_helper_bash_execs_last_reads_the_host(self):
        # The one shape the shell-only keys give up: bash execs a bare -c
        # string's last command, so the helper holds the shell's pid under
        # another name. Hermes' wrapper runs the command inside a non-final
        # eval, where bash does not exec it.
        helper = self.root / "helper.py"
        helper.write_text('import subprocess\nsubprocess.run(["kubectl", "get", "pods", "p"])\n')
        self.assertEqual(["p -> HOST"], self.line(f"FETCH seeded-a && python3 {helper}"))
        self.assertEqual(
            [f"p -> {self.A}"],
            self.line(f"eval 'FETCH seeded-a && python3 {helper}'\n__ec=$?"),
        )

    def test_a_fetch_under_a_process_that_is_not_a_shell_pins_the_line(self):
        self.assertEqual(
            [f"m -> {self.A}"], self.line("timeout 60 FETCH seeded-a && kubectl get pods m")
        )

    def test_a_wrapped_fetch_replaces_the_lines_earlier_pin(self):
        out = self.line(
            "FETCH seeded-a && kubectl get pods x"
            " && timeout 60 FETCH seeded-b && kubectl get pods y"
        )
        self.assertEqual([f"x -> {self.A}", f"y -> {self.B}"], out)

    def test_a_subshell_inherits_its_parents_pin(self):
        self.assertEqual([f"m -> {self.A}"], self.line("FETCH seeded-a && (kubectl get pods m)"))

    def test_an_explicit_context_still_wins(self):
        self.assertEqual(
            [f"m -> {self.B}"],
            self.line(f"FETCH seeded-a && kubectl --context {self.B} get pods m"),
        )

    def test_exited_shells_leave_at_most_the_last_pin(self):
        for _ in range(4):
            self.line("FETCH seeded-a && kubectl get pods m")
        shells = self.home / ".kubeconfigs" / "shells"
        self.assertLessEqual(len(list(shells.iterdir())), 1)


class ScriptedBroker:
    """A local HTTP server that answers `/v1/exec` from a script, in order.

    Each entry is `(status, headers, body)`; a body that is a dict is sent as
    JSON. The last entry repeats once the script runs out, so a test that
    expects five refusals scripts one. Every request's body is kept, so a test
    can check the retried request is the same one. Real sockets rather than a
    patched opener: the retry has to work through `open_broker_request`'s own
    opener and a new connection per attempt, which is what a replica switch
    depends on.
    """

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.disconnect_after_send = False
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                server.requests.append(json.loads(self.rfile.read(length).decode("utf-8")))
                if server.disconnect_after_send:
                    # The request was on the wire and read; the answer never comes.
                    self.close_connection = True
                    self.wfile.close()
                    return
                index = min(len(server.requests) - 1, len(server.script) - 1)
                status, headers, body = server.script[index]
                data = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body.encode("utf-8")
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                if isinstance(body, dict):
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


BUSY_SLOT = {
    "status": "busy",
    "code": "CREDENTIAL_PROXY_BUSY",
    "bound": "slot",
    "error": "the credential proxy is at its limit of 8 concurrent commands",
}
BUSY_DRAINING = {
    "status": "busy",
    "code": "CREDENTIAL_PROXY_BUSY",
    "bound": "draining",
    "error": "the credential proxy is stopping",
}
BUSY_SESSION = {
    "status": "busy",
    "code": "CREDENTIAL_PROXY_BUSY",
    "bound": "session",
    "error": "the session role is limited to 2 concurrent command(s) through the credential proxy",
}
BUSY_UNBOUNDED = {"status": "busy", "code": "CREDENTIAL_PROXY_BUSY", "error": "busy, from an older broker"}
PRESSURE = {"code": "CREDENTIAL_PROXY_RESOURCE_PRESSURE", "error": "the credential proxy is under memory pressure"}
OK = {"exitCode": 0, "stdout": "NAME   STATUS\ndefault   Active\n", "stderr": ""}


class RetriesAdmissionRefusalsTest(unittest.TestCase):
    """The shim waits out the broker's admission refusals instead of failing at once."""

    def setUp(self):
        patcher = patch.dict("os.environ", {"CREDENTIAL_PROXY_TOKEN_FILE": "", "KUBECONFIG": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleeps = []
        self.rng = random.Random(7)

    def broker(self, script):
        broker = ScriptedBroker(script)
        self.addCleanup(broker.close)
        return broker

    def run_shim(self, broker, argv=("kubectl", "get", "ns")):
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", new=out), patch("sys.stderr", new=err):
            code = credential_proxy_client.execute(
                broker.endpoint, list(argv), sleep_fn=self.sleeps.append, rng=self.rng
            )
        return code, out.getvalue(), err.getvalue()

    def test_a_429_then_a_busy_503_then_a_200_runs_the_command(self):
        broker = self.broker([(429, {"Retry-After": "4"}, PRESSURE), (503, {}, BUSY_SLOT), (200, {}, OK)])
        code, out, err = self.run_shim(broker)
        self.assertEqual(0, code, err)
        self.assertEqual(OK["stdout"], out)
        self.assertEqual(3, len(broker.requests))
        # Two sleeps: Retry-After 4 gives [2, 4]; the second retry's base is 4 s, [2, 4] too.
        self.assertEqual(2, len(self.sleeps))
        for delay in self.sleeps:
            self.assertGreaterEqual(delay, 2.0)
            self.assertLessEqual(delay, 4.0)
        # One line per wait, naming the broker's reason and the attempt.
        lines = err.strip().splitlines()
        self.assertEqual(2, len(lines), err)
        self.assertIn("credential proxy busy (HTTP 429): the credential proxy is under memory pressure", lines[0])
        self.assertIn("(attempt 2)", lines[0])
        self.assertIn("credential proxy busy (HTTP 503): the credential proxy is at its limit", lines[1])
        self.assertIn("(attempt 3)", lines[1])

    def test_the_retried_request_is_the_same_request(self):
        broker = self.broker([(503, {}, BUSY_SLOT), (200, {}, OK)])
        self.run_shim(broker)
        self.assertEqual(2, len(broker.requests))
        self.assertEqual(broker.requests[0], broker.requests[1])
        self.assertEqual(broker.requests[0]["requestId"], broker.requests[1]["requestId"])

    def test_five_refusals_give_up_with_a_throttling_line(self):
        broker = self.broker([(429, {}, PRESSURE)])
        code, out, err = self.run_shim(broker)
        self.assertEqual(1, code)
        self.assertEqual("", out)
        self.assertEqual(5, len(broker.requests))
        self.assertEqual(4, len(self.sleeps))
        self.assertIn(
            "credential proxy throttled: gave up after 5 attempts (HTTP 429): "
            "the credential proxy is under memory pressure",
            err.strip().splitlines()[-1],
        )

    def test_a_retry_that_could_outlast_the_deadline_is_not_started(self):
        # Two refusals the broker held for its full admission wait: the clock
        # reads 0 at the first send, 60.5 s at the first refusal and 122.5 s at
        # the second. A third attempt could end past 170 s, inside the terminal
        # tool's 180 s kill, so the shim gives up there with the reason instead.
        readings = iter([0.0, 60.5, 122.5])
        broker = self.broker([(503, {}, BUSY_SLOT)])
        out, err = io.StringIO(), io.StringIO()
        with patch("sys.stdout", new=out), patch("sys.stderr", new=err):
            code = credential_proxy_client.execute(
                broker.endpoint, ["kubectl", "get", "ns"],
                sleep_fn=self.sleeps.append, rng=self.rng, clock=lambda: next(readings),
            )
        self.assertEqual(1, code)
        self.assertEqual(2, len(broker.requests))
        self.assertEqual(1, len(self.sleeps))
        lines = err.getvalue().strip().splitlines()
        self.assertEqual(2, len(lines), err.getvalue())
        self.assertIn("(attempt 2)", lines[0])
        self.assertIn(
            f"credential proxy throttled: gave up after 2 attempts (HTTP 503): {BUSY_SLOT['error']}", lines[1]
        )

    def test_a_caller_s_deadline_with_no_room_for_a_retry_stops_the_shim_at_the_first_refusal(self):
        # The Cluster Agent preflight kills one brokered call at 125 s, which
        # holds one admission wait and one command run, and tells the shim
        # 60 s: not even a refusal answered at once is retried, since the
        # retry's admission wait alone would reach the deadline, so the
        # broker's reason, not the kill, is the output.
        readings = iter([0.0, 0.5])
        broker = self.broker([(503, {}, BUSY_SLOT)])
        out, err = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {credential_proxy_client.RETRY_DEADLINE_ENV: "60"}), \
                patch("sys.stdout", new=out), patch("sys.stderr", new=err):
            code = credential_proxy_client.execute(
                broker.endpoint, ["kubectl", "get", "ns"],
                sleep_fn=self.sleeps.append, rng=self.rng, clock=lambda: next(readings),
            )
        self.assertEqual(1, code)
        self.assertEqual(1, len(broker.requests))
        self.assertEqual([], self.sleeps)
        self.assertEqual(
            [f"credential proxy throttled: gave up after 1 attempt (HTTP 503): {BUSY_SLOT['error']}"],
            err.getvalue().strip().splitlines(),
        )

    def test_the_admission_wait_the_deadline_allows_for_is_the_brokers(self):
        self.assertEqual(
            credential_proxy.COMMAND_SLOT_WAIT_SECONDS, credential_proxy_client.BROKER_ADMISSION_WAIT_SECONDS
        )
        # Two waited attempts fit the deadline; a third never starts.
        two = 2 * credential_proxy_client.BROKER_ADMISSION_WAIT_SECONDS
        self.assertLess(two + credential_proxy_client.RETRY_BACKOFF_BASE_SECONDS, credential_proxy_client.RETRY_DEADLINE_SECONDS)
        self.assertGreater(two + credential_proxy_client.BROKER_ADMISSION_WAIT_SECONDS, credential_proxy_client.RETRY_DEADLINE_SECONDS)

    def test_the_exponential_waits_are_jittered_and_capped_at_16(self):
        broker = self.broker([(503, {}, BUSY_SLOT)])
        self.run_shim(broker)
        bounds = [(1.0, 2.0), (2.0, 4.0), (4.0, 8.0), (8.0, 16.0)]
        self.assertEqual(len(bounds), len(self.sleeps))
        for delay, (low, high) in zip(self.sleeps, bounds):
            self.assertGreaterEqual(delay, low)
            self.assertLessEqual(delay, high)
        self.assertLessEqual(sum(self.sleeps), 30.0)

    def test_retry_after_is_honoured_and_clamped_at_30(self):
        broker = self.broker([(429, {"Retry-After": "120"}, PRESSURE), (200, {}, OK)])
        code, _, _ = self.run_shim(broker)
        self.assertEqual(0, code)
        self.assertEqual(1, len(self.sleeps))
        self.assertGreaterEqual(self.sleeps[0], 15.0)
        self.assertLessEqual(self.sleeps[0], 30.0)

    def test_a_retry_after_of_zero_still_waits_between_the_retries(self):
        # Five 429s each carrying `Retry-After: 0`: four waits, every one inside
        # the floored window [1, 2], none of them zero.
        broker = self.broker([(429, {"Retry-After": "0"}, PRESSURE)])
        code, _, err = self.run_shim(broker)
        self.assertEqual(1, code)
        self.assertEqual(5, len(broker.requests))
        self.assertEqual(4, len(self.sleeps), err)
        for delay in self.sleeps:
            self.assertGreaterEqual(delay, 1.0)
            self.assertLessEqual(delay, 2.0)

    def test_a_retry_after_date_falls_back_to_the_exponential(self):
        broker = self.broker([(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}, PRESSURE), (200, {}, OK)])
        self.run_shim(broker)
        self.assertEqual(1, len(self.sleeps))
        self.assertGreaterEqual(self.sleeps[0], 1.0)
        self.assertLessEqual(self.sleeps[0], 2.0)

    def test_a_session_cap_refusal_is_reported_and_not_retried(self):
        broker = self.broker([(503, {}, BUSY_SESSION), (200, {}, OK)])
        code, _, err = self.run_shim(broker)
        self.assertEqual(1, code)
        self.assertEqual(1, len(broker.requests))
        self.assertEqual([], self.sleeps)
        self.assertIn(BUSY_SESSION["error"], err)
        self.assertNotIn("retrying", err)

    def test_a_busy_reply_whose_bound_is_not_a_string_is_reported_and_not_retried(self):
        # A broker of another version, or something between, writing `bound`
        # as a list: the shim prints the refusal, as it does for a bound it
        # does not know, rather than crash on the lookup.
        broker = self.broker([(503, {}, {**BUSY_SLOT, "bound": ["slot"]}), (200, {}, OK)])
        code, _, err = self.run_shim(broker)
        self.assertEqual(1, code)
        self.assertEqual(1, len(broker.requests))
        self.assertEqual([], self.sleeps)
        self.assertEqual(BUSY_SLOT["error"], err.strip())

    def test_a_busy_reply_with_no_bound_is_retried(self):
        broker = self.broker([(503, {}, BUSY_UNBOUNDED), (200, {}, OK)])
        code, _, _ = self.run_shim(broker)
        self.assertEqual(0, code)
        self.assertEqual(2, len(broker.requests))

    def test_a_bare_non_json_503_is_not_retried(self):
        broker = self.broker([(503, {"Content-Type": "text/html"}, "<html>upstream connect error</html>"), (200, {}, OK)])
        code, _, err = self.run_shim(broker)
        self.assertEqual(1, code)
        self.assertEqual(1, len(broker.requests))
        self.assertEqual([], self.sleeps)
        self.assertIn("credential proxy error (HTTP 503): non-JSON response", err)

    def test_an_envoy_overloaded_503_is_retried(self):
        broker = self.broker([
            (503, {"Content-Type": "text/plain", "x-envoy-overloaded": "true"}, "envoy overloaded"),
            (200, {}, OK),
        ])
        code, _, err = self.run_shim(broker)
        self.assertEqual(0, code, err)
        self.assertEqual(2, len(broker.requests))
        self.assertIn("credential proxy busy (HTTP 503): non-JSON response; retrying", err)

    def test_a_disconnect_after_the_send_is_not_retried(self):
        broker = self.broker([(200, {}, OK)])
        broker.disconnect_after_send = True
        code, _, err = self.run_shim(broker)
        self.assertEqual(1, code)
        self.assertEqual(1, len(broker.requests))
        self.assertEqual([], self.sleeps)
        self.assertIn("the command may have run", err)
        self.assertNotIn("retrying", err)

    def test_a_policy_block_still_exits_77_at_once(self):
        blocked = {"code": "SECURITY_POLICY_BLOCKED", "message": "Command blocked.", "rule": "no-delete"}
        broker = self.broker([(403, {}, blocked), (200, {}, OK)])
        code, _, err = self.run_shim(broker, ("kubectl", "delete", "ns", "x"))
        self.assertEqual(credential_proxy_client.EXIT_SECURITY_POLICY_BLOCKED, code)
        self.assertEqual(1, len(broker.requests))
        self.assertEqual([], self.sleeps)
        self.assertIn("policy rule: no-delete", err)

    def test_a_plain_200_is_unchanged(self):
        broker = self.broker([(200, {}, OK)])
        code, out, err = self.run_shim(broker)
        self.assertEqual((0, OK["stdout"], ""), (code, out, err))
        self.assertEqual(1, len(broker.requests))
        self.assertEqual([], self.sleeps)

    def test_any_other_json_error_is_printed_once(self):
        broker = self.broker([(400, {}, {"error": "argv is required"}), (200, {}, OK)])
        code, _, err = self.run_shim(broker)
        self.assertEqual(1, code)
        self.assertEqual(1, len(broker.requests))
        self.assertEqual("argv is required", err.strip())


class RetryScheduleTest(unittest.TestCase):
    """The delay and the refusal filter, without a server."""

    class Pinned:
        def __init__(self):
            self.draws = []

        def uniform(self, low, high):
            self.draws.append((low, high))
            return high

    def test_the_exponential_bases_are_2_4_8_16(self):
        rng = self.Pinned()
        delays = [credential_proxy_client.retry_delay_seconds(k, None, rng) for k in range(4)]
        self.assertEqual([2.0, 4.0, 8.0, 16.0], delays)
        self.assertEqual([(1.0, 2.0), (2.0, 4.0), (4.0, 8.0), (8.0, 16.0)], rng.draws)
        self.assertEqual(16.0, credential_proxy_client.retry_delay_seconds(9, None, rng))

    def test_retry_after_is_the_base_held_between_2_and_30(self):
        rng = self.Pinned()
        self.assertEqual(4.0, credential_proxy_client.retry_delay_seconds(0, "4", rng))
        self.assertEqual((2.0, 4.0), rng.draws[-1])
        self.assertEqual(30.0, credential_proxy_client.retry_delay_seconds(0, "45", rng))
        self.assertEqual(2.5, credential_proxy_client.retry_delay_seconds(3, " 2.5 ", rng))
        # A header under the backoff floor, 0 or negative included, still waits
        # the floor, jittered: a zero base would make the retries a burst.
        for header in ("1.5", "0", "-3"):
            self.assertEqual(2.0, credential_proxy_client.retry_delay_seconds(0, header, rng), header)
            self.assertEqual((1.0, 2.0), rng.draws[-1], header)

    def test_an_unreadable_retry_after_falls_back(self):
        rng = self.Pinned()
        for header in ("Wed, 21 Oct 2026 07:28:00 GMT", "", "nan", "inf", "soon"):
            self.assertEqual(8.0, credential_proxy_client.retry_delay_seconds(2, header, rng), header)

    def test_retry_wait_is_spent_by_count_or_by_deadline(self):
        rng = self.Pinned()
        wait = credential_proxy_client.retry_wait
        # Immediate refusals: four waits, then None.
        self.assertEqual(2.0, wait(1, 0.0, None, rng, lambda: 0.0))
        self.assertEqual(16.0, wait(4, 0.0, None, rng, lambda: 0.0))
        self.assertIsNone(wait(5, 0.0, None, rng, lambda: 0.0))
        # A waited refusal: 60.5 s elapsed, 2 s of sleep and a 60 s admission wait still fit 170 s.
        self.assertEqual(2.0, wait(1, 0.0, None, rng, lambda: 60.5))
        # 122.5 s elapsed: another admission wait would end past the deadline.
        self.assertIsNone(wait(2, 0.0, None, rng, lambda: 122.5))
        # A long Retry-After counts against the deadline too.
        self.assertIsNone(wait(1, 0.0, "30", rng, lambda: 81.0))
        self.assertEqual(30.0, wait(1, 0.0, "30", rng, lambda: 79.0))
        # A caller's shorter deadline: one waited attempt fits 120 s, a second does not.
        self.assertIsNone(wait(1, 0.0, None, rng, lambda: 60.5, deadline=120.0))
        self.assertEqual(2.0, wait(1, 0.0, None, rng, lambda: 0.5, deadline=120.0))
        # The preflight's 60 s: a retry's admission wait alone reaches it, so
        # not even a refusal answered at once is retried.
        self.assertIsNone(wait(1, 0.0, None, rng, lambda: 0.0, deadline=60.0))

    def test_the_callers_deadline_only_shortens_the_shims(self):
        read = credential_proxy_client.retry_deadline_seconds
        default = credential_proxy_client.RETRY_DEADLINE_SECONDS
        name = credential_proxy_client.RETRY_DEADLINE_ENV
        self.assertEqual(default, read({}))
        self.assertEqual(120.0, read({name: "120"}))
        self.assertEqual(120.0, read({name: " 120 "}))
        self.assertEqual(0.0, read({name: "0"}))
        self.assertEqual(default, read({name: "500"}))
        for bad in ("", "soon", "-1", "inf", "nan"):
            self.assertEqual(default, read({name: bad}), bad)

    def test_the_default_rng_jitters_inside_the_window(self):
        for _ in range(50):
            delay = credential_proxy_client.retry_delay_seconds(1, None)
            self.assertGreaterEqual(delay, 2.0)
            self.assertLessEqual(delay, 4.0)

    def test_which_busy_replies_are_retried(self):
        retryable = credential_proxy_client.busy_reply_retryable
        for bound in sorted(credential_proxy_client.RETRYABLE_BUSY_BOUNDS):
            self.assertTrue(retryable({"code": "CREDENTIAL_PROXY_BUSY", "bound": bound}), bound)
        self.assertTrue(retryable({"code": "CREDENTIAL_PROXY_BUSY"}))
        self.assertTrue(retryable({"code": "CREDENTIAL_PROXY_RESOURCE_PRESSURE"}))
        for bound in ("session", "scope", "refresh-lock", "other"):
            self.assertFalse(retryable({"code": "CREDENTIAL_PROXY_BUSY", "bound": bound}), bound)
        # A bound that is not a string is not one the shim knows, and must not
        # raise out of the lookup.
        for bound in (["slot"], {"slot": True}, 1, 0.5, True):
            self.assertFalse(retryable({"code": "CREDENTIAL_PROXY_BUSY", "bound": bound}), repr(bound))
        self.assertFalse(retryable({"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}}))
        self.assertFalse(retryable({"code": "SECURITY_POLICY_BLOCKED"}))
        self.assertFalse(retryable(None))
        self.assertFalse(retryable(["CREDENTIAL_PROXY_BUSY"]))

    def test_which_exec_answers_are_retried(self):
        retryable = credential_proxy_client.exec_refusal_retryable
        overloaded = {"x-envoy-overloaded": "true"}
        self.assertTrue(retryable(429, {}, None))
        self.assertTrue(retryable(429, {}, {"error": "anything"}))
        self.assertTrue(retryable(503, {}, {"code": "CREDENTIAL_PROXY_BUSY", "bound": "slot"}))
        self.assertTrue(retryable(503, overloaded, None))
        self.assertFalse(retryable(503, {}, None))
        self.assertFalse(retryable(503, overloaded, {"code": "CREDENTIAL_PROXY_BUSY", "bound": "session"}))
        self.assertFalse(retryable(503, {}, {"error": "a plain failure"}))
        self.assertFalse(retryable(500, overloaded, None))
        self.assertFalse(retryable(403, {}, {"code": "SECURITY_POLICY_BLOCKED"}))

    def test_the_envoy_header_is_read_case_insensitively(self):
        headers = http.client.HTTPMessage()
        headers["X-Envoy-Overloaded"] = "true"
        self.assertTrue(credential_proxy_client.exec_refusal_retryable(503, headers, None))


class ApiSessionRetriesTheBrokerOnlyTest(unittest.TestCase):
    """The relay waits out the broker's own refusal and hands an upstream one back."""

    ENDPOINT = "http://agent-credential-proxy.kubeagents-system.svc.cluster.local:8765"
    URL = "https://monitoring.googleapis.com/v3/projects/kagents-dev/timeSeries"

    class Response:
        def __init__(self, status_code, body, headers=None):
            self.status_code = status_code
            self._body = body
            self.headers = headers or {}

        def json(self):
            if isinstance(self._body, (dict, list)):
                return self._body
            raise ValueError("not JSON")

    class ScriptedHttp:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []

        def get(self, url, *, params=None, headers=None, timeout=None):
            self.calls.append({"url": url, "params": params, "headers": headers, "timeout": timeout})
            return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]

    def setUp(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        token_file = directory / "token"
        token_file.write_text("caller-token\n", encoding="utf-8")
        patcher = patch.dict(
            os.environ, {"CREDENTIAL_PROXY_URL": self.ENDPOINT, "CREDENTIAL_PROXY_TOKEN_FILE": str(token_file)}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sleeps = []

    def session(self, responses):
        http_ = self.ScriptedHttp(responses)
        return http_, credential_proxy_client.ApiSession(
            http=http_, sleep_fn=self.sleeps.append, rng=random.Random(3)
        )

    def test_a_broker_busy_then_a_200_is_one_answer(self):
        ok = self.Response(200, {"timeSeries": []})
        http_, session = self.session([
            self.Response(503, BUSY_SLOT, {"Retry-After": "4"}),
            self.Response(429, PRESSURE),
            ok,
        ])
        with patch("sys.stderr", new=io.StringIO()) as err:
            self.assertIs(ok, session.get(self.URL, params={"pageSize": 1}, timeout=30))
        self.assertEqual(3, len(http_.calls))
        self.assertEqual({call["url"] for call in http_.calls}, {http_.calls[0]["url"]})
        self.assertEqual([{"pageSize": 1}] * 3, [call["params"] for call in http_.calls])
        self.assertEqual([30] * 3, [call["timeout"] for call in http_.calls])
        self.assertEqual(2, len(self.sleeps))
        self.assertGreaterEqual(self.sleeps[0], 2.0)
        self.assertLessEqual(self.sleeps[0], 4.0)
        self.assertEqual(2, len(err.getvalue().strip().splitlines()))

    def test_a_draining_refusal_drops_the_pooled_connections_before_the_retry(self):
        # A draining exec pod answers on the caller's keep-alive connection to
        # its Envoy; only a new connection reaches another replica.
        class ClosingHttp(self.ScriptedHttp):
            def __init__(self, responses):
                super().__init__(responses)
                self.events = []

            def get(self, url, **kwargs):
                self.events.append("get")
                return super().get(url, **kwargs)

            def close(self):
                self.events.append("close")

        ok = self.Response(200, {"timeSeries": []})
        draining = ClosingHttp([self.Response(503, BUSY_DRAINING), ok])
        slot = ClosingHttp([self.Response(503, BUSY_SLOT), ok])
        for http_ in (draining, slot):
            session = credential_proxy_client.ApiSession(http=http_, sleep_fn=self.sleeps.append, rng=random.Random(3))
            with patch("sys.stderr", new=io.StringIO()):
                self.assertIs(ok, session.get(self.URL))
        self.assertEqual(["get", "close", "get"], draining.events)
        self.assertEqual(["get", "get"], slot.events)

    def test_a_broker_that_stays_busy_hands_back_its_last_answer(self):
        busy = self.Response(503, BUSY_SLOT)
        http_, session = self.session([busy])
        with patch("sys.stderr", new=io.StringIO()):
            self.assertIs(busy, session.get(self.URL))
        self.assertEqual(5, len(http_.calls))
        self.assertEqual(4, len(self.sleeps))

    def test_the_relay_stops_at_the_deadline_too(self):
        readings = iter([0.0, 60.5, 122.5])
        busy = self.Response(503, BUSY_SLOT)
        http_ = self.ScriptedHttp([busy])
        session = credential_proxy_client.ApiSession(
            http=http_, sleep_fn=self.sleeps.append, rng=random.Random(3), clock=lambda: next(readings)
        )
        with patch("sys.stderr", new=io.StringIO()):
            self.assertIs(busy, session.get(self.URL))
        self.assertEqual(2, len(http_.calls))
        self.assertEqual(1, len(self.sleeps))

    def test_the_relay_keeps_to_a_caller_s_shorter_deadline_too(self):
        readings = iter([0.0, 60.5])
        busy = self.Response(503, BUSY_SLOT)
        http_ = self.ScriptedHttp([busy])
        session = credential_proxy_client.ApiSession(
            http=http_, sleep_fn=self.sleeps.append, rng=random.Random(3), clock=lambda: next(readings)
        )
        with patch.dict("os.environ", {credential_proxy_client.RETRY_DEADLINE_ENV: "120"}), \
                patch("sys.stderr", new=io.StringIO()):
            self.assertIs(busy, session.get(self.URL))
        self.assertEqual(1, len(http_.calls))
        self.assertEqual([], self.sleeps)

    def test_an_upstream_429_passes_through_at_once(self):
        quota = self.Response(429, {"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}})
        http_, session = self.session([quota, self.Response(200, {})])
        self.assertIs(quota, session.get(self.URL))
        self.assertEqual(1, len(http_.calls))
        self.assertEqual([], self.sleeps)

    def test_an_upstream_503_and_a_session_cap_pass_through(self):
        for refused in (
            self.Response(503, {"error": {"code": 503, "status": "UNAVAILABLE"}}),
            self.Response(503, "<html>upstream</html>"),
            self.Response(503, BUSY_SESSION),
        ):
            http_, session = self.session([refused, self.Response(200, {})])
            self.assertIs(refused, session.get(self.URL))
            self.assertEqual(1, len(http_.calls))
        self.assertEqual([], self.sleeps)

    def test_a_transport_that_returns_no_status_is_handed_back(self):
        http_, session = self.session(["upstream-response"])
        self.assertEqual("upstream-response", session.get(self.URL))
        self.assertEqual([], self.sleeps)


class TestSessionIdInTheExecBody(SubmittedPayloadTestCase):
    """The exec body carries the card or session id the shim runs under, so the
    broker can count the run's commands; absent when there is none."""

    ENV = {"KUBECONFIG": "", "HERMES_KANBAN_TASK": "", "HERMES_CRON_RUN": "", "HERMES_SESSION_ID": ""}

    def test_the_card_comes_first(self):
        payload = self.submit(
            ["gcloud", "projects", "list"],
            {
                **self.ENV, "HERMES_KANBAN_TASK": "t_f660e9c5", "HERMES_CRON_RUN": "job_7c1e2a_e6b9f0d1",
                "HERMES_SESSION_ID": "20260925_001944_b09c64",
            },
        )
        self.assertEqual("t_f660e9c5", payload["sessionId"])

    def test_the_scheduled_run_comes_next(self):
        # The ForceCommand in the shell sandbox derives this one from the run's
        # cwd (tests/test_sandbox_session_command.py); the shim sends it.
        payload = self.submit(
            ["gcloud", "projects", "list"],
            {**self.ENV, "HERMES_CRON_RUN": "job_7c1e2a_e6b9f0d1", "HERMES_SESSION_ID": "20260925_001944_b09c64"},
        )
        self.assertEqual("job_7c1e2a_e6b9f0d1", payload["sessionId"])

    def test_the_session_otherwise(self):
        payload = self.submit(["gcloud", "projects", "list"], {**self.ENV, "HERMES_SESSION_ID": "20260925_001944_b09c64"})
        self.assertEqual("20260925_001944_b09c64", payload["sessionId"])

    def test_absent_when_neither_is_set(self):
        payload = self.submit(["gcloud", "projects", "list"], self.ENV)
        self.assertNotIn("sessionId", payload)

    def test_a_value_outside_the_grammar_is_dropped_not_sent(self):
        payload = self.submit(["gcloud", "projects", "list"], {**self.ENV, "HERMES_SESSION_ID": "not a token"})
        self.assertNotIn("sessionId", payload)
        # A malformed card falls through to the session.
        payload = self.submit(
            ["gcloud", "projects", "list"],
            {**self.ENV, "HERMES_KANBAN_TASK": "not a token", "HERMES_SESSION_ID": "20260925_001944_b09c64"},
        )
        self.assertEqual("20260925_001944_b09c64", payload["sessionId"])


class TestSessionRefusalsReadAsAPolicyBlock(unittest.TestCase):
    """The broker's two per-session refusals print like a policy block and exit
    EXIT_SECURITY_POLICY_BLOCKED, so the runtime does not read them as a missing
    executable; the message is what tells the model to stop."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.token_path = self.tmp / "token"
        self.token_path.write_text("valid-token\n")

    def _refused_with(self, payload):
        body = json.dumps(payload).encode("utf-8")
        opened = []

        def fake_open(req, *args, **kwargs):
            opened.append(req)
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", http.client.HTTPMessage(), io.BytesIO(body))

        stderr = io.StringIO()
        environ = {
            "CREDENTIAL_PROXY_TOKEN_FILE": str(self.token_path),
            "KUBECONFIG": "",
            "HERMES_HOME": str(self.tmp),
            "HERMES_SESSION_ID": "robot-4726-a",
        }
        with patch.dict("os.environ", environ, clear=False), patch.object(credential_proxy_client, "open_broker_request", fake_open):
            with patch("sys.stderr", new=stderr):
                code = credential_proxy_client.execute("http://proxy", ["gcloud", "projects", "list"])
        return code, stderr.getvalue(), opened

    def test_the_loop_breaker(self):
        message = (
            "BROKER_LOOP_BREAKER: identical command failed or was blocked 5 consecutive times in this "
            "session; stop retrying this command and report the refusal/error."
        )
        code, err, opened = self._refused_with(
            {"status": "blocked", "code": "BROKER_LOOP_BREAKER", "rule": "session.loop-breaker", "message": message}
        )
        self.assertEqual(credential_proxy_client.EXIT_SECURITY_POLICY_BLOCKED, code)
        self.assertEqual(f"{message}\npolicy rule: session.loop-breaker\n", err)
        # Refused, not retried: one request was sent, and it carried the id.
        self.assertEqual(1, len(opened))
        self.assertEqual("robot-4726-a", json.loads(opened[0].data)["sessionId"])

    def test_the_ceiling(self):
        message = (
            "BROKER_SESSION_BUDGET_EXCEEDED: session exceeded 1500 tool invocations; "
            "summarize current findings and finish the turn."
        )
        code, err, opened = self._refused_with(
            {"status": "blocked", "code": "BROKER_SESSION_BUDGET_EXCEEDED", "rule": "session.tool-budget", "message": message}
        )
        self.assertEqual(77, code)
        self.assertEqual(f"{message}\npolicy rule: session.tool-budget\n", err)
        self.assertEqual(1, len(opened))

    def test_an_unknown_code_is_still_a_plain_error(self):
        code, err, _ = self._refused_with({"status": "blocked", "code": "SOMETHING_ELSE", "error": "no"})
        self.assertEqual(1, code)
        self.assertEqual("no\n", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
