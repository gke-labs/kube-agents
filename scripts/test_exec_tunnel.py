"""The exec relay, against a fake kubectl and a real loopback server.

`tests/e2e` is a live-cluster suite that CI never imports
(scripts/test_test_discovery.py records the exclusion), so the relay it depends
on would otherwise be checked only by an RC run. It is separable from the
cluster, though: everything specific to Kubernetes is the `kubectl` argv, so a
`kubectl` on PATH that runs the relay's own remote half locally exercises the
real code — the READY handshake, the short-write loop, pod re-resolution and
the reap — over a real socket.

The two cases that earn their keep are the ones the module docstring says broke
it: a payload large enough to force short writes on the pipe, and a stale pod
name that has to be re-resolved rather than retried against.
"""

import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import exec_tunnel  # noqa: E402

# Big enough that the relay's pipe writes come back short, which is finding 2
# in exec_tunnel's docstring. 1.9MB is the size that actually truncated the
# dashboard's entrypoint.
BIG_BODY = b"x" * (1900 * 1024)


class _Upstream(BaseHTTPRequestHandler):
    """Stands in for a loopback listener inside the pod."""

    protocol_version = "HTTP/1.1"

    def do_GET(self):  # noqa: N802
        body = BIG_BODY if self.path == "/big" else b'{"sessions": []}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


FAKE_KUBECTL = r'''"""Answers the two kubectl calls exec_tunnel makes, without a cluster."""
import json, os, sys

args = sys.argv[1:]


def pod_item(name, ready=True):
    return {
        "metadata": {"name": name},
        "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]},
    }


if args[0] == "get" and args[1] == "pods":
    if os.environ.get("FAKE_BROKEN"):
        sys.stderr.write("Unable to connect to the server: dial tcp: i/o timeout\n")
        sys.exit(1)
    if os.environ.get("FAKE_EMPTY"):
        items = []
    elif os.environ.get("FAKE_NOTREADY_FIRST"):
        items = [pod_item("running-pod", ready=False), pod_item("ready-pod")]
    elif os.environ.get("FAKE_STALE"):
        # The first resolve hands back a pod the exec refuses, so the caller
        # has to re-resolve; the counter file makes the second call differ.
        with open(os.environ["FAKE_STATE"], "a+") as fh:
            fh.seek(0)
            n = len(fh.read().split())
            fh.write("x ")
        items = [pod_item("stale-pod" if n == 0 else "good-pod")]
    else:
        items = [pod_item("good-pod")]
    sys.stdout.write(json.dumps({"items": items}))
    sys.exit(0)

if args[0] == "exec":
    # kubectl exec -i -n <ns> <pod> -c <container> -- ...
    pod = args[args.index("-n") + 2]
    if pod == "stale-pod":
        sys.stderr.write("Error from server (NotFound): pods \"stale-pod\" not found\n")
        sys.exit(1)
    # Everything after "--" is the interpreter and its -u -c <program>.
    tail = args[args.index("--") + 1:]
    os.execvp(sys.executable, [sys.executable, "-u", "-c", tail[-1]])

sys.stderr.write("fake kubectl: unhandled %r\n" % (args,))
sys.exit(1)
'''


class ExecTunnelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        bin_dir = pathlib.Path(cls.tmp.name) / "bin"
        bin_dir.mkdir()
        kubectl = bin_dir / "kubectl"
        # sys.executable, not `#!/usr/bin/env python3`: env resolves off the
        # ambient PATH, so the fake would run whichever interpreter the shell
        # happened to offer -- a different one from the module under test, and
        # none at all under a stripped PATH.
        kubectl.write_text("#!" + sys.executable + "\n" + FAKE_KUBECTL)
        kubectl.chmod(0o755)
        cls.bin_dir = str(bin_dir)

        # For the "kubectl is not installed" case. An empty directory rather
        # than the developer's real PATH: see test_a_missing_kubectl_*.
        empty_bin = pathlib.Path(cls.tmp.name) / "empty-bin"
        empty_bin.mkdir()
        cls.empty_bin = str(empty_bin)

        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
        cls.upstream_port = cls.upstream.server_address[1]
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.upstream.shutdown()
        cls.upstream.server_close()
        cls.tmp.cleanup()

    # Every knob the fake reads, cleared before and after each test so one
    # case cannot leak a mode into the next.
    FAKE_FLAGS = ("FAKE_STALE", "FAKE_EMPTY", "FAKE_BROKEN", "FAKE_NOTREADY_FIRST")

    def setUp(self):
        self._path = os.environ.get("PATH", "")
        # The fake shadows any real kubectl, and no test ever falls back to the
        # developer's PATH for a kubectl call.
        os.environ["PATH"] = self.bin_dir + os.pathsep + self._path
        state = tempfile.NamedTemporaryFile(delete=False)
        state.close()
        self._state = state.name
        os.environ["FAKE_STATE"] = self._state
        for flag in self.FAKE_FLAGS:
            os.environ.pop(flag, None)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ["PATH"] = self._path
        os.environ.pop("FAKE_STATE", None)
        for flag in self.FAKE_FLAGS:
            os.environ.pop(flag, None)
        os.unlink(self._state)

    def _temp_sink(self):
        """A closed-and-removed-on-cleanup file for kubectl's stderr."""
        sink = tempfile.NamedTemporaryFile(mode="w", delete=False)
        self.addCleanup(os.unlink, sink.name)
        self.addCleanup(sink.close)
        return sink

    def _tunnel(self, **overrides):
        kwargs = dict(
            namespace="ns",
            selector="app=platform-agent-gateway",
            container="envoy-credential-proxy",
            remote_port=self.upstream_port,
            python=sys.executable,
            ready_timeout=30.0,
            log=lambda message: None,
        )
        kwargs.update(overrides)
        server = exec_tunnel.serve_background(exec_tunnel.TunnelConfig(**kwargs), local_port=0)
        # Reverse order of registration, so: shutdown, close_relays, then close.
        self.addCleanup(server.server_close)
        self.addCleanup(server.close_relays)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def _get(self, port, path="/"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}{path}", timeout=30) as response:
            return response.status, response.read()

    def test_relays_a_response(self):
        status, body = self._get(self._tunnel())
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"sessions": []}')

    def test_relays_a_payload_larger_than_one_pipe_write(self):
        status, body = self._get(self._tunnel(), "/big")
        self.assertEqual(status, 200)
        self.assertEqual(len(body), len(BIG_BODY), "short write truncated the stream")

    def test_re_resolves_the_pod_when_the_first_exec_fails(self):
        os.environ["FAKE_STALE"] = "1"
        log = self._temp_sink()
        status, _ = self._get(self._tunnel(stderr=log))
        self.assertEqual(status, 200)
        self.assertIn("stale-pod", pathlib.Path(log.name).read_text())

    def _resolver(self):
        return exec_tunnel.PodResolver(exec_tunnel.TunnelConfig(
            namespace="ns", selector="app=platform-agent-gateway",
            container="c", remote_port=self.upstream_port, python=sys.executable,
        ))

    def test_no_matching_pod_is_reported_not_raised(self):
        os.environ["FAKE_EMPTY"] = "1"
        with self.assertRaises(RuntimeError) as caught:
            self._resolver().get()
        self.assertIn("app=platform-agent-gateway", str(caught.exception))

    def test_a_missing_kubectl_is_a_runtime_error(self):
        # PATH is pointed at an EMPTY directory, never at the developer's real
        # PATH. An earlier version of this test stripped the fake kubectl and
        # left the rest in place, so on a machine with kubectl installed it
        # resolved pods against whatever cluster the current context named --
        # a PR-gating unit test making a live API call.
        os.environ["PATH"] = self.empty_bin
        with self.assertRaises(RuntimeError) as caught:
            self._resolver().get()
        self.assertIn("could not run kubectl", str(caught.exception))

    def test_a_failing_kubectl_is_a_runtime_error(self):
        os.environ["FAKE_BROKEN"] = "1"
        with self.assertRaises(RuntimeError) as caught:
            self._resolver().get()
        self.assertIn("Unable to connect to the server", str(caught.exception))

    def test_ready_pods_are_preferred_over_merely_running_ones(self):
        os.environ["FAKE_NOTREADY_FIRST"] = "1"
        self.assertEqual(self._resolver().get(), "ready-pod")

    def test_track_refuses_after_close_and_close_is_idempotent(self):
        server = exec_tunnel.build_server(exec_tunnel.TunnelConfig(), local_port=0)
        self.addCleanup(server.server_close)
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.assertTrue(server.track(proc))
        server.close_relays()
        self.assertIsNotNone(proc.poll(), "close_relays did not reap the tracked child")

        late = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(exec_tunnel.reap, late)
        # A handler that got past Popen before the teardown and reaches track()
        # after it must be told so, or its exec session is never reaped.
        self.assertFalse(server.track(late))
        server.close_relays()  # idempotent

    def test_reap_waits_for_the_child(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        exec_tunnel.reap(proc, timeout=5.0)
        self.assertIsNotNone(proc.poll(), "reap left the child unwaited")

    def test_ready_timeout_does_not_hang_on_a_listener_that_never_answers(self):
        # A remote port nothing is bound to: the remote half raises before it
        # writes READY, so _spawn must reject rather than block the handler.
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        dead_port = dead.getsockname()[1]
        dead.close()

        port = self._tunnel(remote_port=dead_port, ready_timeout=5.0, stderr=self._temp_sink())
        started = time.time()
        with socket.create_connection(("127.0.0.1", port), timeout=10) as client:
            client.sendall(b"GET / HTTP/1.0\r\n\r\n")
            try:
                # Clean close or reset both mean "no bytes and not hung", which
                # is the property under test; which one depends on the platform.
                self.assertEqual(client.recv(1), b"", "expected the relay to close, not answer")
            except ConnectionResetError:
                pass
        self.assertLess(time.time() - started, 30)


if __name__ == "__main__":
    unittest.main()
