import io
import json
import logging
import os
import queue
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import types
import unittest
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import credential_proxy
import gke_endpoint
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
                timeout_seconds=5, max_output_bytes=1024, state_dir=self.temp_dir.name
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
            ["git", "fetch", "--prune", "origin"],
            ["git", "config", "user.name", "platform-agent"],
            ["git", "ls-files"],
        ):
            with self.subTest(argv=argv):
                self.assertIsNone(executor.git_lease_violation(argv, unleased))

    def test_clone_is_allowed_at_the_lease_root(self):
        # `ensure_workspace` runs it one directory above a tree that does not
        # exist yet, so there is nothing there to damage — and the `.lease` is
        # written first, so the directory is leased even then.
        executor = self.executor()
        holder = executor.workspace_dir / "gitops" / "t_card"
        holder.mkdir(parents=True)
        self.assertIsNone(
            executor.git_lease_violation(
                ["git", "clone", "--quiet", "https://github.com/acme/fleet", "x"],
                str(holder),
            )
        )

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
            _git_plan(["git", "--attr-source", "HEAD", "help", "-m", "git"])[0], "HEAD"
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


class GitLeaseGateWiringTest(unittest.TestCase):
    """The gate as the agent meets it — over HTTP, through /v1/exec."""

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

    def executor(self, timeout_seconds=5, max_output_bytes=1024):
        return CommandExecutor(
            timeout_seconds=timeout_seconds,
            max_output_bytes=max_output_bytes,
            state_dir=self.temp_dir.name,
        )

    def caller_kubeconfig(self, executor, name="kubeconfig.yaml", body=None):
        """A kubeconfig where the agent can reach it — i.e. one to distrust."""
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
        pinned = self.caller_kubeconfig(executor, name="profiles/cluster-a/kubeconfig.yaml")

        resolved = executor._resolve_kubeconfig(str(pinned))

        self.assertEqual(managed, resolved)
        # The whole point: what kubectl opens is somewhere the agent cannot write.
        self.assertFalse(executor._within_workspace(resolved))

    def test_hostile_kubeconfig_content_never_reaches_the_command(self):
        # The escape this mechanism exists to close. Every field here is one the
        # sidecar would otherwise act on: `exec.command` runs next to the
        # credentials, `server` picks where the minted token is sent, and
        # `insecure-skip-tls-verify` removes the obstacle to sending it there.
        # None of it can be seen by the policy engine, whose rules match argv.
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

        resolved = executor._resolve_kubeconfig(str(hostile))
        contents = resolved.read_text(encoding="utf-8")

        for trace in ("attacker.example.invalid", "/bin/sh", "insecure-skip-tls-verify"):
            self.assertNotIn(trace, contents)

    def test_kubeconfig_flag_is_rerouted_as_well_as_the_environment(self):
        # `--kubeconfig` takes precedence over KUBECONFIG in kubectl and reaches
        # the sidecar untouched — no policy rule mentions it. Rewriting only the
        # environment would leave the flag as a way straight back to the
        # caller's own file.
        executor = self.executor()
        managed = self.seed_managed(executor)
        pinned = self.caller_kubeconfig(executor)

        joined = executor._reroute_kubeconfig_flags(["kubectl", f"--kubeconfig={pinned}", "get", "pods"])
        separate = executor._reroute_kubeconfig_flags(["kubectl", "--kubeconfig", str(pinned), "get", "pods"])

        self.assertEqual(["kubectl", f"--kubeconfig={managed}", "get", "pods"], joined)
        self.assertEqual(["kubectl", "--kubeconfig", str(managed), "get", "pods"], separate)

    def test_kubeconfig_flag_outside_the_workspace_is_still_refused(self):
        executor = self.executor()
        with self.assertRaisesRegex(ValueError, "outside the shared workspace"):
            executor._reroute_kubeconfig_flags(["kubectl", "--kubeconfig=/etc/kubeconfig.yaml", "get", "pods"])

    def test_kubeconfig_surrounding_whitespace_is_ignored(self):
        # Profile .env files routinely carry a trailing newline; a path that
        # only differs by whitespace must still resolve, not silently fail.
        executor = self.executor()
        managed = self.seed_managed(executor)
        pinned = self.caller_kubeconfig(executor)
        self.assertEqual(managed, executor._resolve_kubeconfig(f"  {pinned}\n"))

    # ---- Failing closed ------------------------------------------------------

    def test_rejects_kubeconfig_naming_no_current_context(self):
        executor = self.executor()
        pinned = self.caller_kubeconfig(executor, body="apiVersion: v1\nkind: Config\n")
        with self.assertRaisesRegex(ValueError, "names no current-context"):
            executor._resolve_kubeconfig(str(pinned))

    def test_rejects_kubeconfig_whose_context_is_not_a_gke_name(self):
        # Without a parseable triple there is no cluster to re-fetch, so there is
        # no way to serve the request without trusting the caller's document.
        executor = self.executor()
        pinned = self.caller_kubeconfig(executor, body="current-context: minikube\n")
        with self.assertRaisesRegex(ValueError, "not a GKE context name"):
            executor._resolve_kubeconfig(str(pinned))

    def test_rejects_kubeconfig_outside_shared_workspace(self):
        with self.assertRaisesRegex(ValueError, "outside the shared workspace"):
            self.executor()._resolve_kubeconfig("/etc/kubeconfig.yaml")

    def test_rejects_kubeconfig_escaping_the_workspace_by_traversal(self):
        executor = self.executor()
        escape = str(executor.workspace_dir / ".." / "home" / ".kube" / "config")
        with self.assertRaisesRegex(ValueError, "outside the shared workspace"):
            executor._resolve_kubeconfig(escape)

    def test_rejects_merged_kubeconfig_lists(self):
        # kubectl would flatten these into one view; there is no meaningful way
        # to regenerate a merge of documents that are never trusted.
        executor = self.executor()
        allowed = self.caller_kubeconfig(executor)
        with self.assertRaisesRegex(ValueError, "single file"):
            executor._resolve_kubeconfig(f"{allowed}:/etc/kubeconfig.yaml")

    def test_rejects_an_implausibly_large_kubeconfig(self):
        executor = self.executor()
        pinned = self.caller_kubeconfig(executor, body="#" * (1 << 20) + "\n")
        with self.assertRaisesRegex(ValueError, "implausibly large"):
            executor._resolve_kubeconfig(str(pinned))

    # ---- Fetching, and the visible pin --------------------------------------

    def test_cache_miss_refetches_credentials_from_gcloud(self):
        executor = self.fake_gcloud(self.executor())
        pinned = self.caller_kubeconfig(executor)

        resolved = executor._resolve_kubeconfig(str(pinned))

        self.assertEqual(executor.kubeconfig_dir / f"{self.CONTEXT}.yaml", resolved)
        self.assertIn(self.CONTEXT, resolved.read_text(encoding="utf-8"))
        # Nothing is left behind from the fetch.
        self.assertEqual([resolved.name], sorted(p.name for p in executor.kubeconfig_dir.iterdir()))

    def test_get_credentials_writes_both_the_managed_copy_and_the_visible_pin(self):
        # cluster_agent_profile.py and switch_kube_context both reach a cluster
        # by running this first, so it is what warms the cache. The workspace
        # copy has to appear too: the profile records that path and the Cluster
        # Agent preflight stats it.
        executor = self.fake_gcloud(self.executor())
        destination = executor.workspace_dir / "profiles" / "cluster-a" / "kubeconfig.yaml"

        result = executor.execute(
            ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
             "--location=us-central1", "--project=demo-project"],
            kubeconfig=str(destination),
        )

        self.assertEqual(0, result.exit_code)
        self.assertIn(self.CONTEXT, destination.read_text(encoding="utf-8"))
        managed = executor.kubeconfig_dir / f"{self.CONTEXT}.yaml"
        self.assertIn(self.CONTEXT, managed.read_text(encoding="utf-8"))

    def test_get_credentials_never_writes_through_the_callers_path(self):
        # gcloud must not be handed the agent-writable path directly; if it were,
        # the agent could swap the file between the write and the read that files
        # it in the cache.
        executor = self.fake_gcloud(self.executor())
        destination = executor.workspace_dir / "kubeconfig.yaml"
        seen = []
        original = executor._execute

        def record(argv, **kwargs):
            seen.append(kwargs.get("kubeconfig_path"))
            return original(argv, **kwargs)

        with mock.patch.object(executor, "_execute", record):
            executor.execute(
                ["gcloud", "container", "clusters", "get-credentials", "cluster-a",
                 "--location=us-central1", "--project=demo-project"],
                kubeconfig=str(destination),
            )

        self.assertEqual(1, len(seen))
        self.assertFalse(executor._within_workspace(seen[0]))

    # ---- Choosing the control-plane endpoint --------------------------------

    def test_cache_miss_passes_dns_endpoint_when_the_cluster_needs_it(self):
        # The cold path: a restart empties the state dir, so the proxy refetches
        # on its own rather than reusing what the agent's get-credentials filed.
        # A DNS-only cluster has to survive that refetch.
        executor = self.fake_gcloud(self.executor())
        pinned = self.caller_kubeconfig(executor)
        seen = []
        original = executor._execute

        def record(argv, **kwargs):
            seen.append(argv)
            return original(argv, **kwargs)

        with (
            mock.patch("gke_endpoint.dns_endpoint_args", return_value=["--dns-endpoint"]),
            mock.patch.object(executor, "_execute", record),
        ):
            executor._resolve_kubeconfig(str(pinned))

        fetches = [argv for argv in seen if "get-credentials" in argv]
        self.assertEqual(1, len(fetches))
        self.assertEqual("--dns-endpoint", fetches[0][-1])

    def test_dns_endpoint_probe_runs_the_resolved_gcloud_not_whatever_is_on_path(self):
        # gke_endpoint builds argv starting with the literal "gcloud". In the
        # sidecar the only gcloud that may run is the resolved executable, so the
        # adapter has to substitute it.
        executor = self.fake_gcloud(self.executor())
        resolved = executor.executables["gcloud"]
        target = credential_proxy.parse_gke_context(self.CONTEXT)
        seen = []

        def fake_args(project, cluster, location, *, run=None, env=None):
            seen.append(run(["gcloud", "container", "clusters", "describe", cluster]))
            return []

        with mock.patch("gke_endpoint.dns_endpoint_args", fake_args):
            executor._dns_endpoint_args(resolved, target)

        self.assertEqual(1, len(seen))
        # The stub exits non-zero without KUBECONFIG set, which is all this needs
        # to prove: the adapter ran *something*, and it ran it through _execute.
        self.assertIsInstance(seen[0], tuple)

    def test_missing_gke_endpoint_falls_back_instead_of_failing_the_fetch(self):
        # credential_proxy is otherwise stdlib-only. Losing a sibling module must
        # cost the flag, not the whole credential proxy.
        executor = self.fake_gcloud(self.executor())
        target = credential_proxy.parse_gke_context(self.CONTEXT)

        with mock.patch.dict(sys.modules, {"gke_endpoint": None}):
            self.assertEqual([], executor._dns_endpoint_args("gcloud", target))

    def test_timeout_kills_command(self):
        result = self.executor(timeout_seconds=1).execute_internal(["/bin/sleep", "10"])
        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.exit_code)

    def test_timeout_handles_process_group_exit_race(self):
        process = mock.Mock(pid=123, returncode=0)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["command"], 1),
            (b"", b""),
        ]
        with (
            mock.patch("credential_proxy.subprocess.Popen", return_value=process),
            mock.patch("credential_proxy.os.killpg", side_effect=ProcessLookupError),
        ):
            result = self.executor(timeout_seconds=1).execute_internal(["command"])
        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.exit_code)

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


class GitHubRefreshHandlerTest(unittest.TestCase):
    """A failed refresh splits its diagnosis: detail to the log, none to the reply.

    The reply crosses back into the agent sandbox and the caller renders the
    resulting reason code into a chat room, so it stays output-free. The
    helper's stderr carries the broker's actual refusal and is the only thing
    an operator has to read, so it has to reach the sidecar's own log.
    """

    def _refresh(self, result):
        handler = CredentialProxyHandler.__new__(CredentialProxyHandler)
        handler.max_request_bytes = 10 * 1024 * 1024
        body = json.dumps({"repository": "gke-agentic/adamparco-infra"}).encode()
        handler.headers = {"Content-Length": str(len(body))}
        handler.rfile = io.BytesIO(body)
        handler.executor = types.SimpleNamespace(execute_internal=lambda argv: result)
        replies = []
        handler._json = lambda status, payload: replies.append((status, payload))
        with self.assertLogs(credential_proxy.LOGGER, level="WARNING") as logs:
            handler._handle_github_refresh()
        return replies, logs.output

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

    def test_logs_broker_refusal_but_keeps_it_out_of_the_reply(self):
        refusal = "Minty returned error (HTTP 403): installation not found"
        replies, logs = self._refresh(self._failure(refusal + "\n"))

        self.assertIn(refusal, "\n".join(logs))
        self.assertEqual(
            replies,
            [(HTTPStatus.BAD_GATEWAY, {"error": "GitHub credential refresh failed"})],
        )
        self.assertNotIn(refusal, json.dumps(replies[0][1]))

    def test_truncates_oversized_stderr(self):
        # `_execute` bounds output at CREDENTIAL_PROXY_MAX_OUTPUT_BYTES, 4 MiB by
        # default, which is not a log line -- and this path runs on every failed
        # cron tick.
        _, logs = self._refresh(self._failure("x" * 5000))

        detail = logs[0].split("GitHub credential refresh exited 1: ", 1)[1]
        self.assertEqual(detail, "x" * 1000)

    def test_omits_the_detail_when_stderr_is_empty(self):
        _, logs = self._refresh(self._failure("   \n"))

        self.assertTrue(logs[0].endswith("GitHub credential refresh exited 1"))

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

    def _serve_with(self, enforce_value):
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
        )
        environment = {
            "API_SERVER_EXTERNAL_KEY": "external",
            "CREDENTIAL_PROXY_BOOTSTRAP_COMMAND": "",
        }
        if enforce_value is not None:
            environment["CREDENTIAL_PROXY_ENFORCE_READ_ONLY"] = enforce_value
        try:
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(credential_proxy, "ThreadingHTTPServer", mock.MagicMock()), \
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

    def test_serve_disarms_the_gate_when_the_env_var_says_false(self):
        CredentialProxyHandler.enforce_read_only = True
        self.assertFalse(self._serve_with("false"))

    def test_serve_leaves_the_gate_armed_on_a_typo(self):
        CredentialProxyHandler.enforce_read_only = False
        self.assertTrue(self._serve_with("banana"))


class ReadOnlyOverTheSocketTest(unittest.TestCase):
    """A mutation must stop at the proxy socket, not merely at a decision function."""

    def setUp(self):
        self.executed = []
        owner = self

        class RecordingExecutor:
            ALLOWED_EXECUTABLES = CommandExecutor.ALLOWED_EXECUTABLES

            def git_lease_violation(self, argv, cwd):
                return None

            def execute(self, argv, stdin=None, cwd=None, kubeconfig=None):
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
                with mock.patch.dict(os.environ, {"API_SERVER_EXTERNAL_KEY": "external"}, clear=True), \
                        mock.patch.object(credential_proxy, "ThreadingHTTPServer", mock.MagicMock()), \
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
    identity, and the root formatter is line-oriented plain text. A newline in
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

        def execute(self, argv, stdin=None, cwd=None, kubeconfig=None):
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
        self._post({"requestId": "ok", "argv": ["git", "status"], "cwd": "/tmp/a\nb"})
        self._assert_single_line_records("git lease refused")


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

        def execute(self, argv, stdin=None, cwd=None, kubeconfig=None):
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
            audience=self.AUDIENCE,
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


class BuildAuthenticatorTest(unittest.TestCase):
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
        self.assertEqual("kubeagents-credential-proxy", authenticator.audience)


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
                mock.patch.object(credential_proxy, "ThreadingHTTPServer", refuse_to_bind), \
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
                mock.patch.object(credential_proxy, "ThreadingHTTPServer", refuse_to_bind), \
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
        }
        original = CredentialProxyHandler.__dict__.get("authenticator")
        try:
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(credential_proxy, "ThreadingHTTPServer", FakeServer), \
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

        def execute(self, argv, stdin=None, cwd=None, kubeconfig=None):
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
            audience="kubeagents-credential-proxy",
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


if __name__ == "__main__":
    unittest.main()
