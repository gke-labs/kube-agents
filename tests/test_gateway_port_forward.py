"""hack/gateway-port-forward.sh against a fake kubectl.

The helper forwards a gateway door to the pod holding the gateway's Lease, if
the door's Service selects it, and restarts the forward when the lease moves.
The fake kubectl answers the three reads from files the test writes, and its
port-forward records the target and sleeps until killed.
"""

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "hack" / "gateway-port-forward.sh"
# The helper reads the Lease every five seconds while a forward runs.
MOVE_WAIT_SECONDS = 15
START_WAIT_SECONDS = 5

FAKE_KUBECTL = r"""#!/usr/bin/env bash
# args: -n <ns> <verb> ...
shift 2
echo "$1 $2" >>"${FAKE_DIR}/calls"
case "$1 $2" in
  "get lease")
    if [ -e "${FAKE_DIR}/lease_fails" ]; then
      echo 'Error from server (InternalError): an error on the server ("") has prevented the request from succeeding' >&2
      exit 1
    fi
    if [ -e "${FAKE_DIR}/lease_missing" ]; then
      echo 'Error from server (NotFound): leases.coordination.k8s.io "platform-agent-a2a-gateway" not found' >&2
      exit 1
    fi
    cat "${FAKE_DIR}/holder" ;;
  "get svc") printf 'app=platform-agent-a2a-gateway,' ;;
  "get pods") cat "${FAKE_DIR}/pods" ;;
  port-forward*)
    echo "$2" >>"${FAKE_DIR}/forwards"
    echo "$$" >>"${FAKE_DIR}/pids"
    exec sleep 300 ;;
  *) echo "fake kubectl: unsupported $*" >&2; exit 1 ;;
esac
"""


class GatewayPortForwardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        kubectl = bin_dir / "kubectl"
        kubectl.write_text(FAKE_KUBECTL)
        kubectl.chmod(0o755)
        (self.dir / "forwards").write_text("")
        (self.dir / "pods").write_text("pod/gw-a\npod/gw-b\n")
        self.env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FAKE_DIR": str(self.dir),
            "KUBE_CONTEXT": "",
        }
        self.proc: subprocess.Popen[bytes] | None = None

    def tearDown(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=10)

    def start(self) -> None:
        self.proc = subprocess.Popen(
            ["bash", str(SCRIPT), "8099", "28099"],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def calls(self, verb: str) -> int:
        path = self.dir / "calls"
        return path.read_text().splitlines().count(verb) if path.exists() else 0

    def forwards(self) -> list[str]:
        return (self.dir / "forwards").read_text().split()

    def wait_for(self, want: list[str], within: float) -> None:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            if self.forwards() == want:
                return
            time.sleep(0.2)
        self.fail(f"forwards {self.forwards()}, want {want}")

    def test_the_forward_follows_the_lease(self) -> None:
        (self.dir / "holder").write_text("gw-a")
        self.start()
        self.wait_for(["pod/gw-a"], START_WAIT_SECONDS)
        # A handover where gw-a's pod survives: the forward stays up, and
        # only the helper's read of the Lease notices.
        (self.dir / "holder").write_text("gw-b")
        self.wait_for(["pod/gw-a", "pod/gw-b"], MOVE_WAIT_SECONDS)

    def test_a_failed_lease_read_keeps_the_forward_that_is_up(self) -> None:
        # An API blip mid-forward moves nothing: the forward to gw-a stays,
        # and the failure is said.
        (self.dir / "holder").write_text("gw-a")
        self.start()
        self.wait_for(["pod/gw-a"], START_WAIT_SECONDS)
        (self.dir / "lease_fails").write_text("")
        polls = self.calls("get lease")
        deadline = time.monotonic() + MOVE_WAIT_SECONDS
        while time.monotonic() < deadline and self.calls("get lease") < polls + 2:
            time.sleep(0.2)
        self.assertGreaterEqual(self.calls("get lease"), polls + 2, "the helper stopped polling")
        self.assertEqual(self.forwards(), ["pod/gw-a"])
        (self.dir / "lease_fails").unlink()
        assert self.proc is not None
        self.proc.send_signal(signal.SIGTERM)
        _, stderr = self.proc.communicate(timeout=10)
        self.assertIn(b"reading lease platform-agent-a2a-gateway failed", stderr)

    def test_steady_polls_read_only_the_lease(self) -> None:
        # The Service and its pods are checked when the holder changes, not
        # on every poll.
        (self.dir / "holder").write_text("gw-a")
        self.start()
        self.wait_for(["pod/gw-a"], START_WAIT_SECONDS)
        polls = self.calls("get lease")
        deadline = time.monotonic() + MOVE_WAIT_SECONDS
        while time.monotonic() < deadline and self.calls("get lease") < polls + 2:
            time.sleep(0.2)
        self.assertGreaterEqual(self.calls("get lease"), polls + 2)
        self.assertEqual((self.calls("get svc"), self.calls("get pods")), (1, 1))

    def test_a_missing_lease_forwards_the_service_quietly(self) -> None:
        (self.dir / "holder").write_text("")
        (self.dir / "lease_missing").write_text("")
        self.start()
        self.wait_for(["svc/platform-agent-a2a-inject"], START_WAIT_SECONDS)
        assert self.proc is not None
        self.proc.send_signal(signal.SIGTERM)
        _, stderr = self.proc.communicate(timeout=10)
        self.assertNotIn(b"failed", stderr)

    def test_a_holder_the_service_does_not_select_gets_the_service(self) -> None:
        (self.dir / "holder").write_text("some-other-pod")
        self.start()
        self.wait_for(["svc/platform-agent-a2a-inject"], START_WAIT_SECONDS)

    def test_no_lease_forwards_the_service(self) -> None:
        (self.dir / "holder").write_text("")
        self.start()
        self.wait_for(["svc/platform-agent-a2a-inject"], START_WAIT_SECONDS)

    def test_sigterm_stops_the_forward(self) -> None:
        (self.dir / "holder").write_text("gw-a")
        self.start()
        self.wait_for(["pod/gw-a"], START_WAIT_SECONDS)
        assert self.proc is not None
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=10), 0)
        # The forward it ran (the fake's sleep, under the pid it recorded) is
        # gone with it.
        for pid in (self.dir / "pids").read_text().split():
            deadline = time.monotonic() + START_WAIT_SECONDS
            while time.monotonic() < deadline and _alive(int(pid)):
                time.sleep(0.1)
            self.assertFalse(_alive(int(pid)), f"forward {pid} outlived the helper")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True

if __name__ == "__main__":
    unittest.main()
