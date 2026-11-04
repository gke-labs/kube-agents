"""The credential broker's SIGTERM drain, held inside the waits around it.

    python3 -m unittest tests.test_credential_proxy_drain_budget

Three files each declare one link of the chain, and nothing but this test stops
one moving without the others:

- the broker (`agents/platform/scripts/credential_proxy.py`) drains for up to
  SHUTDOWN_DRAIN_SECONDS, waits SHUTDOWN_END_WAIT_SECONDS for the requests whose
  commands it ended, then hands each Chat relay's receipts back in one call of
  at most SHUTDOWN_NACK_TIMEOUT_SECONDS;
- the entrypoint (`deploy/shared/start-services.sh`) waits
  BROKER_RUNTIME_DRAIN_SECONDS for the broker to exit, polling every
  SHUTDOWN_DRAIN_POLL_SECONDS, before it stops Envoy;
- the operator (`k8s-operator/internal/controller/credential_proxy_manifests.go`)
  gives the pod credentialProxyTerminationGraceSeconds before the kubelet's SIGKILL.

A drain longer than the entrypoint's wait is cut by its SIGKILL with the answers
unwritten; an entrypoint wait as long as the grace period leaves Envoy no time to
stop. The files are read as text: importing the broker pulls in its runtime
dependencies.
"""

from __future__ import annotations

import pathlib
import re
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
BROKER = REPO_ROOT / "agents/platform/scripts/credential_proxy.py"
ENTRYPOINT = REPO_ROOT / "deploy/shared/start-services.sh"
OPERATOR = REPO_ROOT / "k8s-operator/internal/controller/credential_proxy_manifests.go"
# The broker's two Chat relays, the operator's and the A2A gateway's, each
# hand their receipts back in a call of their own, one after the other.
CHAT_RELAYS = 2


def _number(pattern: str, path: pathlib.Path) -> float:
    found = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    if found is None:
        raise AssertionError(f"{path.relative_to(REPO_ROOT)} declares nothing matching {pattern!r}")
    return float(found.group(1))


def broker(name: str) -> float:
    return _number(rf"^{name} = (\d+(?:\.\d+)?)$", BROKER)


def entrypoint(name: str) -> float:
    return _number(rf"^readonly {name}=(\d+)$", ENTRYPOINT)


def operator(name: str) -> float:
    return _number(rf"^const {name}\s+int64\s*=\s*(\d+)$", OPERATOR)


class DrainBudgetTest(unittest.TestCase):
    def test_the_broker_stop_fits_the_entrypoint_wait(self):
        stop = (broker("SHUTDOWN_DRAIN_SECONDS") + broker("SHUTDOWN_END_WAIT_SECONDS")
                + CHAT_RELAYS * broker("SHUTDOWN_NACK_TIMEOUT_SECONDS"))
        wait = entrypoint("BROKER_RUNTIME_DRAIN_SECONDS")
        self.assertLessEqual(
            stop + entrypoint("SHUTDOWN_DRAIN_POLL_SECONDS"), wait,
            "the broker's drain, end wait and hand-back outlast the entrypoint's wait for it",
        )

    def test_the_entrypoint_wait_leaves_envoy_time_inside_the_grace_period(self):
        self.assertLess(
            entrypoint("BROKER_RUNTIME_DRAIN_SECONDS"),
            operator("credentialProxyTerminationGraceSeconds"),
            "the kubelet's SIGKILL would land before the entrypoint stops Envoy",
        )

    def test_the_end_wait_holds_a_process_group_kill(self):
        # The drain ends a command through `_capture_output`'s stop check, then
        # `_kill_process_group`: a poll, a SIGTERM grace, a SIGKILL settle.
        self.assertLessEqual(
            broker("STOP_POLL_SECONDS") + broker("KILL_GRACE_SECONDS") + broker("KILL_SETTLE_SECONDS"),
            broker("SHUTDOWN_END_WAIT_SECONDS"),
        )


if __name__ == "__main__":
    unittest.main()
