#!/usr/bin/env python3
"""Tests for the sandbox execution helper.

Everything here is about what leaves the agent pod: which login the connection
authenticates as, what the sandbox's shell is asked to parse, and what the ssh
client is allowed to see of the agent pod's environment. Those are the three
ways this helper can be wrong without any test failing elsewhere.

Run:  python3 agents/platform/scripts/test_sandbox_exec.py
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import sandbox_exec

SANDBOX_CONFIG = """
terminal:
  backend: ssh
  ssh_host: platform-agent-shell-0.platform-agent-shell.kubeagents-system.svc.cluster.local
  ssh_key: /etc/sandbox-ssh/id_ed25519
  ssh_port: 2222
  ssh_user: agent
"""

LOCAL_CONFIG = """
terminal:
  backend: local
"""


def write_config(body):
    handle = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False)
    handle.write(body)
    handle.close()
    return handle.name


class ConfigTestCase(unittest.TestCase):
    def test_ssh_backend_enables_the_sandbox(self):
        self.assertTrue(sandbox_exec.sandbox_enabled(write_config(SANDBOX_CONFIG)))

    def test_local_backend_does_not(self):
        self.assertFalse(sandbox_exec.sandbox_enabled(write_config(LOCAL_CONFIG)))

    def test_missing_config_is_not_an_error(self):
        self.assertFalse(sandbox_exec.sandbox_enabled("/nonexistent/config.yaml"))

    def test_a_config_that_is_there_and_unreadable_is_loud(self):
        """The one place a default is worse than a crash.

        "No sandbox" sends the command to `subprocess.run` in the agent pod,
        which is where model-authored code must never run. A managed config
        that exists and cannot be parsed says nothing about which side to use,
        so it is not an answer to guess at.
        """
        broken = write_config("terminal:\n  backend: ssh\n   ssh_host: [\n")
        with self.assertRaises(sandbox_exec.SandboxMisconfigured):
            sandbox_exec.sandbox_enabled(broken)

        unreadable = write_config(SANDBOX_CONFIG)
        os.chmod(unreadable, 0o000)
        self.addCleanup(os.chmod, unreadable, 0o600)
        if os.geteuid() != 0:  # root reads it regardless
            with self.assertRaises(sandbox_exec.SandboxMisconfigured):
                sandbox_exec.sandbox_enabled(unreadable)


class ArgvTestCase(unittest.TestCase):
    def setUp(self):
        self.config = write_config(SANDBOX_CONFIG)

    def argv(self, command, **kwargs):
        return sandbox_exec.ssh_argv(command, path=self.config, **kwargs)

    def test_connects_as_hermes_not_as_the_shell_user(self):
        """The whole point of the second principal.

        terminal.ssh_user is `agent`, whose ~/.bashrc the model owns and which
        bash sources for a non-interactive `ssh host cmd`. Authenticating as it
        would let the model choose what this caller sees.
        """
        target = [a for a in self.argv(["kubectl", "get", "pods"]) if "@" in a]
        self.assertEqual(len(target), 1)
        self.assertTrue(target[0].startswith("hermes@"))
        self.assertNotIn("agent@", " ".join(self.argv(["kubectl"])))

    def test_carries_the_key_and_port_from_the_managed_config(self):
        argv = self.argv(["kubectl"])
        self.assertIn("/etc/sandbox-ssh/id_ed25519", argv)
        self.assertIn("2222", argv)

    def test_batch_mode_and_liveness_options_are_set(self):
        argv = " ".join(self.argv(["kubectl"]))
        for option in ("BatchMode=yes", "ConnectTimeout=10",
                       "ServerAliveInterval=15", "ServerAliveCountMax=3"):
            self.assertIn(option, argv)

    def test_user_ssh_config_is_suppressed(self):
        argv = self.argv(["kubectl"])
        self.assertIn("-F", argv)
        self.assertEqual(argv[argv.index("-F") + 1], "/dev/null")

    def test_arguments_are_quoted_for_the_remote_shell(self):
        remote = self.argv(["kubectl", "get", "pods", "-l", "app=a b; rm -rf /"])[-1]
        # The metacharacters survive as data rather than becoming syntax.
        self.assertIn("'app=a b; rm -rf /'", remote)

    def test_remote_env_is_rendered_into_the_command(self):
        remote = self.argv(["kubectl", "get", "pods"],
                           remote_env={"KUBECONFIG": "/tmp/kube config.yaml"})[-1]
        self.assertIn("env KUBECONFIG='/tmp/kube config.yaml'", remote)

    def test_remote_env_rejects_a_name_that_is_not_a_name(self):
        with self.assertRaises(ValueError):
            self.argv(["kubectl"], remote_env={"A; rm -rf /": "x"})

    def test_cwd_becomes_a_quoted_cd(self):
        remote = self.argv(["kubectl"], cwd="/work space")[-1]
        self.assertTrue(remote.startswith("cd '/work space' &&"))

    def test_the_published_workspace_root_is_the_default_cwd(self):
        """Without this the login lands in /home/hermes and every proxied
        command fails the credential proxy's workspace check."""
        config = write_config(SANDBOX_CONFIG + "  workspace_root: /opt/elsewhere\n")
        remote = sandbox_exec.ssh_argv(["kubectl"], path=config)[-1]
        self.assertTrue(remote.startswith("cd /opt/elsewhere && kubectl"))

    def test_the_default_cwd_falls_back_when_the_operator_published_none(self):
        remote = self.argv(["kubectl"])[-1]
        self.assertTrue(remote.startswith("cd /opt/data && kubectl"))

    def test_the_default_cwd_does_not_come_from_hermes_home(self):
        """HERMES_HOME names a directory in the agent pod, not in the sandbox."""
        with patch.dict(os.environ, {"HERMES_HOME": "/opt/somewhere-else"}):
            remote = self.argv(["kubectl"])[-1]
        self.assertTrue(remote.startswith("cd /opt/data && kubectl"))

    def test_the_known_hosts_directory_is_created_not_assumed(self):
        """Otherwise `accept-new` accepts anew on every connection.

        ssh cannot write the file if its directory is missing. It says so on
        stderr and connects regardless, so the host key is never remembered
        and never compared -- the sandbox could be replaced by anything that
        answers on the same service name and no connection would notice.
        """
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"HERMES_HOME": home}):
                argv = self.argv(["kubectl"])
            expected = Path(home) / ".ssh" / "known_hosts"
            self.assertIn(f"UserKnownHostsFile={expected}", argv)
            self.assertTrue(expected.parent.is_dir())
            self.assertEqual(expected.parent.stat().st_mode & 0o777, 0o700)

    def test_an_unwritable_home_still_remembers_the_key_for_the_pod(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"HERMES_HOME": "/proc/nowhere",
                                         "TMPDIR": tmp}):
                argv = self.argv(["kubectl"])
            option = [a for a in argv if a.startswith("UserKnownHostsFile=")]
            self.assertEqual(len(option), 1)
            self.assertTrue(option[0].endswith("/known_hosts"))
            self.assertTrue(option[0].startswith(f"UserKnownHostsFile={tmp}"))

    def test_no_host_is_reported_as_unavailable(self):
        empty = write_config("terminal:\n  backend: ssh\n")
        with self.assertRaises(sandbox_exec.SandboxUnavailable):
            sandbox_exec.ssh_argv(["kubectl"], path=empty)


class ClientEnvironmentTestCase(unittest.TestCase):
    def test_pod_secrets_are_not_offered_to_the_ssh_client(self):
        """`_run_env()` would have passed these; this helper must not.

        The sandbox declines them today (PermitUserEnvironment no, AcceptEnv
        LANG LC_*), but that is the remote end's choice, not this end's.
        """
        with patch.dict(os.environ, {"API_SERVER_KEY": "sentinel",
                                     "SESSION_KV_API_KEY": "sentinel",
                                     "CREDENTIAL_PROXY_URL": "http://127.0.0.1:8765"}):
            env = sandbox_exec._client_env()
        self.assertNotIn("API_SERVER_KEY", env)
        self.assertNotIn("SESSION_KV_API_KEY", env)
        self.assertNotIn("CREDENTIAL_PROXY_URL", env)
        self.assertIn("PATH", env)


class RunTestCase(unittest.TestCase):
    def setUp(self):
        self.config = write_config(SANDBOX_CONFIG)

    def completed(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(args=["ssh"], returncode=returncode,
                                           stdout=stdout, stderr=stderr)

    def test_ssh_level_failure_raises_rather_than_looking_like_a_cluster_error(self):
        failure = self.completed(255, stderr="ssh: connect to host x port 2222: Connection refused")
        with patch("subprocess.run", return_value=failure):
            with self.assertRaises(sandbox_exec.SandboxUnavailable):
                sandbox_exec.run(["kubectl", "get", "pods"], path=self.config)

    def test_a_connection_that_dies_mid_command_is_a_transport_failure_too(self):
        """Not every ssh failure happens before the command starts.

        An evicted sandbox pod, and a rolling StatefulSet update replacing
        sshd, both kill an open session -- and neither prints "connect to
        host". Read as a remote exit 255, they surface to the caller as the
        cluster refusing the command rather than as something to retry.
        """
        for stderr in (
            "Timeout, server not responding.",
            "ssh_exchange_identification: read: Connection reset by peer",
            "client_loop: send disconnect: Broken pipe",
            "Connection to platform-agent-shell closed by remote host.",
        ):
            with self.subTest(stderr=stderr):
                with patch("subprocess.run", return_value=self.completed(255, stderr=stderr)):
                    with self.assertRaises(sandbox_exec.SandboxUnavailable):
                        sandbox_exec.run(["kubectl", "get", "pods"], path=self.config)

    def test_a_remote_command_exiting_255_is_not_mistaken_for_a_transport_failure(self):
        remote = self.completed(255, stderr="error: the server could not find the requested resource")
        with patch("subprocess.run", return_value=remote):
            result = sandbox_exec.run(["kubectl", "get", "pods"], path=self.config)
        self.assertEqual(result.returncode, 255)

    def test_the_unconfigured_proxy_arrives_as_an_ordinary_failure(self):
        """What an install with no reachable proxy gets.

        Exit 1 with the message on stderr and nothing on stdout, so a caller
        cannot mistake the error text for output.
        """
        proxy = self.completed(1, stderr="CREDENTIAL_PROXY_URL is not configured")
        with patch("subprocess.run", return_value=proxy):
            result = sandbox_exec.run(["kubectl", "get", "pods"], path=self.config)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("not configured", result.stderr)

    def test_args_reports_the_callers_command_not_the_transport(self):
        with patch("subprocess.run", return_value=self.completed()):
            result = sandbox_exec.run(["kubectl", "get", "pods"], path=self.config)
        self.assertEqual(result.args, ["kubectl", "get", "pods"])

    def test_check_raises_with_the_callers_command(self):
        with patch("subprocess.run", return_value=self.completed(1, stderr="boom")):
            with self.assertRaises(subprocess.CalledProcessError) as caught:
                sandbox_exec.run(["kubectl", "get", "pods"], path=self.config, check=True)
        self.assertEqual(caught.exception.cmd, ["kubectl", "get", "pods"])

    def test_without_a_sandbox_the_command_runs_locally(self):
        local = write_config(LOCAL_CONFIG)
        with patch("subprocess.run", return_value=self.completed()) as runner:
            sandbox_exec.run(["kubectl", "get", "pods"], path=local)
        self.assertEqual(runner.call_args[0][0], ["kubectl", "get", "pods"])

    def test_a_command_with_no_document_does_not_inherit_the_callers_stdin(self):
        """The caller's fd 0 is platform_mcp_server.py's JSON-RPC channel.

        `input=None` leaves fd 0 alone rather than closing it, and ssh reads
        ahead on fd 0, so a request the client pipelined behind the tool call
        is consumed by the ssh the tool call started. The server never sees it
        and the client waits for a reply that cannot come. Both branches have
        to redirect: the local fallback runs in the same process.
        """
        for label, config in (("sandbox", self.config), ("local fallback", write_config(LOCAL_CONFIG))):
            with self.subTest(label):
                with patch("subprocess.run", return_value=self.completed()) as runner:
                    sandbox_exec.run(["kubectl", "get", "pods"], path=config)
                self.assertEqual(runner.call_args.kwargs.get("stdin"), subprocess.DEVNULL)
                self.assertNotIn("input", runner.call_args.kwargs)

    def test_a_document_still_reaches_the_command_on_a_pipe(self):
        with patch("subprocess.run", return_value=self.completed()) as runner:
            sandbox_exec.run(["gh", "pr", "create", "--body-file", "-"],
                             path=self.config, stdin="a pull-request body")
        self.assertEqual(runner.call_args.kwargs.get("input"), "a pull-request body")
        self.assertNotIn("stdin", runner.call_args.kwargs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
