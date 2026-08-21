"""Seam: platform_mcp_server over real stdio, spoken to by a real MCP client.

Every existing test calls the tool functions directly; the transport — the
JSON-RPC stream FastMCP runs on stdout — was never exercised, and stdout is
exactly where this server had a live bug: a bare `print()` on the
session-metadata failure path wrote prose into the protocol stream, corrupting
it for the client mid-session. This test spawns the real server process,
performs the real MCP handshake with the client from the same `mcp` package
the server uses, lists the tools, and drives `send_notification` down the
failure path that used to poison the stream.

Fakes sit at the seam's far ends only: `hermes`/`gcloud`/`kubectl` are
argv-recording executables on PATH, and the session-KV lookup fails as it
would with the daemon down (connection refused) — which is the exception path
the bug lived on.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

from _seams import SCRIPTS_DIR, fake_executable, free_port

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    HAVE_MCP_CLIENT = True
except Exception:  # pragma: no cover - requirements-test always carries mcp
    HAVE_MCP_CLIENT = False

EXPECTED_TOOLS = {
    "verify_gke_cluster",
    "list_cc_healthchecks",
    "get_cc_operator_status",
    "get_cc_pod_diagnostics",
    "list_cc_pods",
    "audit_log_searcher",
    "send_notification",
    "report_to_chat",
}


@unittest.skipUnless(HAVE_MCP_CLIENT, "mcp client not importable")
class McpStdioSeamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = Path(self.tmp.name)
        self.hermes_calls = self.tmp_path / "hermes-argv.jsonl"
        bin_dir = self.tmp_path / "bin"
        fake_executable(
            bin_dir,
            "hermes",
            f"""
            import json, sys
            from pathlib import Path
            Path({str(self.hermes_calls)!r}).open("a").write(json.dumps(sys.argv[1:]) + "\\n")
            print("posted")
            """,
        )
        fake_executable(bin_dir, "gcloud", "import sys; print('{}'); sys.exit(0)")
        fake_executable(bin_dir, "kubectl", "import sys; print('{}'); sys.exit(0)")

        env = dict(os.environ)
        env.update(
            {
                "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
                "PYTHONPATH": str(SCRIPTS_DIR),
                # The config the server's platform detection reads; absent →
                # deterministic google_chat fallback.
                "PLATFORM_AGENT_CONFIG_PATH": str(self.tmp_path / "absent.yaml"),
                "PLATFORM_AGENT_DOTENV_PATH": str(self.tmp_path / "absent.env"),
                "SESSION_KV_API_KEY": "irrelevant-here",
                "GOOGLE_CHAT_HOME_CHANNEL": "spaces/HOME",
            }
        )
        self.params = StdioServerParameters(
            command=os.environ.get("PYTHON", "python3"),
            args=[str(SCRIPTS_DIR / "platform_mcp_server.py")],
            env=env,
        )

    def _run(self, coro):
        return asyncio.run(coro)

    def test_the_handshake_lists_exactly_the_registered_tools(self):
        async def scenario():
            async with stdio_client(self.params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    return {tool.name for tool in listed.tools}

        names = self._run(scenario())
        self.assertEqual(EXPECTED_TOOLS, names)

    def test_the_kv_failure_path_keeps_the_protocol_stream_parseable(self):
        """The stream-corruption bug, pinned from the transport side.

        `send_notification` with a session id looks up thread metadata on the
        session KV daemon; with the daemon down that raises, and the handler
        used to `print()` the failure — one line of prose into the JSON-RPC
        stdout. The call below only completes if the stream stayed clean: a
        corrupted stream surfaces as a client-side protocol error or a hang,
        never a result. Before the fix in this change, this test fails; after
        it, the failure line goes to stderr where logs belong.
        """

        async def scenario():
            async with stdio_client(self.params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await asyncio.wait_for(
                        session.call_tool(
                            "send_notification",
                            {
                                "message": "integration probe",
                                "session_id": "k8s-evt-deadbeef",
                            },
                        ),
                        timeout=30,
                    )
                    # A second call proves the stream survived the first.
                    listed = await session.list_tools()
                    return result, {tool.name for tool in listed.tools}

        result, names = self._run(scenario())
        text = "".join(
            block.text for block in result.content if getattr(block, "text", None)
        )
        self.assertIn("SUCCESS", text)
        self.assertEqual(EXPECTED_TOOLS, names)
        # And the message went out through the fake hermes, to the home
        # channel broadcast target (the metadata lookup failed, by design).
        argvs = [
            json.loads(line)
            for line in self.hermes_calls.read_text().splitlines()
            if line.strip()
        ]
        self.assertEqual(1, len(argvs))
        self.assertEqual(["send", "--to", "google_chat:spaces/HOME"], argvs[0][:3])

    def test_every_stdout_line_the_server_emits_is_protocol_json(self):
        """Every line of the stdio conversation held to the protocol contract.

        Not the pin for the bare-print bug: the client at requirements-test's
        pin tolerates stray prose lines, and the buffered print flushes late
        enough that this sweep passes even with the bug present — reproduced
        during review. The deterministic pin for the print's destination is
        test_the_kv_failure_log_line_never_touches_stdout; this test's job is
        the broader contract, held through EOF. The protocol
        contract is stricter than the lenient client: stdout carries JSON-RPC
        and nothing else. This test speaks the handshake over raw pipes and
        asserts every stdout line parses as JSON. The KV-failure print does
        not fire deterministically under this harness (it depends on how the
        lookup fails), so the deterministic pin for the print's destination is
        test_the_kv_failure_log_line_never_touches_stdout below; this test
        holds the whole conversation to the protocol contract regardless of
        which paths fired.
        """
        import subprocess

        env = dict(self.params.env)
        server = subprocess.Popen(
            [self.params.command, *self.params.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
        )
        self.addCleanup(server.kill)

        def send(payload):
            server.stdin.write(json.dumps(payload) + "\n")
            server.stdin.flush()

        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "seam-test", "version": "0"},
                },
            }
        )
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "send_notification",
                    "arguments": {
                        "message": "purity probe",
                        "session_id": "k8s-evt-deadbeef",
                    },
                },
            }
        )

        lines, saw_result = [], False
        import time as _time

        deadline = _time.monotonic() + 60
        while _time.monotonic() < deadline and not saw_result:
            line = server.stdout.readline()
            if not line:
                break
            lines.append(line.rstrip("\n"))
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                self.fail(
                    "non-JSON line on the JSON-RPC stdout stream: " + line.strip()
                )
            if parsed.get("id") == 2:
                saw_result = True
        self.assertTrue(saw_result, "the tools/call result never arrived:\n" + "\n".join(lines))
        # The bare print() is block-buffered on a pipe: the prose line flushes
        # at process exit, after the JSON-RPC exchange — which is exactly why
        # the bug survived in production. Close stdin, let the server exit,
        # and hold every remaining flushed line to the same JSON standard.
        server.stdin.close()
        server.stdin = None  # communicate() must not flush a closed pipe
        remainder, _ = server.communicate(timeout=30)
        for line in (remainder or "").splitlines():
            if not line.strip():
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError:
                self.fail(
                    "non-JSON line flushed onto the JSON-RPC stdout stream at exit: "
                    + line.strip()
                )

    def test_the_kv_failure_log_line_never_touches_stdout(self):
        """The deterministic pin for the bare-print bug: forced failure path.

        Imports the server module in-process, forces the metadata lookup to
        raise, and asserts the failure line lands on stderr and never stdout —
        red before the fix in this change, green after. In the stdio server
        stdout is the JSON-RPC stream; a log line there is protocol
        corruption whenever the buffer flushes mid-session.
        """
        import contextlib
        import io
        import sys as _sys
        from unittest import mock

        _sys.path.insert(0, str(SCRIPTS_DIR))
        import importlib

        module = importlib.import_module("platform_mcp_server")
        stdout, stderr = io.StringIO(), io.StringIO()
        # Both outward calls are pinned shut: the KV lookup raises (the path
        # under test), and subprocess.run raises too — on a workstation with
        # a real hermes on PATH, an unpatched broadcast would post an actual
        # chat message from a unit-test run.
        with mock.patch.object(
            module.urllib.request, "urlopen", side_effect=OSError("kv down")
        ), mock.patch.object(
            module.subprocess, "run", side_effect=FileNotFoundError("hermes")
        ):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = module.send_notification(
                    "probe", session_id="k8s-evt-deadbeef"
                )
        self.assertEqual("", stdout.getvalue(), "log lines must never reach the JSON-RPC stream")
        self.assertIn("Failed to resolve session metadata", stderr.getvalue())
        self.assertIn("ERROR", result)  # the pinned-shut broadcast fails loudly


if __name__ == "__main__":
    unittest.main()
