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
import os
import shutil
import subprocess
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

    def test_open_labels_the_workspace_with_the_session_it_runs_under(self):
        """The broker's reap and refusal lines name a caller only if one is sent.

        The label defaults from the kanban card, then the chat session, and is
        left off the wire when there is neither, so a session with no identity
        sends the payload every broker has always accepted.
        """
        answers = {
            "open": {
                "handle": "a" * 32,
                "repo": "acme/infra",
                "base": "main",
                "baseSha": "b" * 40,
            }
        }
        scrubbed = patch.dict(os.environ)
        scrubbed.start()
        self.addCleanup(scrubbed.stop)
        for name in credential_proxy_client.WORKSPACE_CALLER_ENV:
            os.environ.pop(name, None)

        with self._serve(answers):
            with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_abc"}):
                credential_proxy_client.Workspace.open(self.endpoint, "acme/infra")
            with patch.dict(os.environ, {"HERMES_SESSION_ID": "s_def"}):
                credential_proxy_client.Workspace.open(self.endpoint, "acme/infra")
            # The card outranks the session when both are set.
            with patch.dict(
                os.environ, {"HERMES_KANBAN_TASK": "t_abc", "HERMES_SESSION_ID": "s_def"}
            ):
                credential_proxy_client.Workspace.open(self.endpoint, "acme/infra")
                # An explicit label wins over both, and an explicit empty one
                # sends none.
                credential_proxy_client.Workspace.open(
                    self.endpoint, "acme/infra", caller="adhoc-1"
                )
                credential_proxy_client.Workspace.open(
                    self.endpoint, "acme/infra", caller=""
                )
            credential_proxy_client.Workspace.open(self.endpoint, "acme/infra")

        payloads = [body for _, body in self.calls]
        self.assertEqual("t_abc", payloads[0]["caller"])
        self.assertEqual("s_def", payloads[1]["caller"])
        self.assertEqual("t_abc", payloads[2]["caller"])
        self.assertEqual("adhoc-1", payloads[3]["caller"])
        self.assertEqual({"repo": "acme/infra"}, payloads[4])
        self.assertEqual({"repo": "acme/infra"}, payloads[5])

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
        # The open payload is asserted exactly below, and `Workspace.open`
        # labels it from the session's environment when there is one, so the
        # label's sources are cleared here rather than left to whatever shell
        # runs the tests.
        scrubbed = patch.dict(os.environ)
        scrubbed.start()
        self.addCleanup(scrubbed.stop)
        for name in credential_proxy_client.WORKSPACE_CALLER_ENV:
            os.environ.pop(name, None)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
