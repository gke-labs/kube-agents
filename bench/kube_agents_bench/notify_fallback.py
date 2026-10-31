# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The ``notification_fell_back_to_home`` check: did a report survive a thread that refused it.

``bench/tf/prebuilt/notify-dead-thread`` asks
``platform_mcp_server.send_notification`` to report into a Google Chat thread in a
space the install does not own. Under ``next`` the A2A gateway refuses a threaded
notification outside the home channel, so the threaded send fails on every run.

This passes when the tool reports both halves: the threaded send failed, **and** the
report was then delivered to the home channel anyway. Before
gke-labs/kube-agents#743's fix the home-channel broadcast is gated on whether a
target was *composed* rather than on whether one was *delivered*, so the refusal ends
the call and the only line is the error — the report reaches nobody, which is the red.

Both halves are required on purpose. A run where the threaded send unexpectedly
*succeeded* proves nothing about the fallback, so it is an error rather than a pass:
the fixture did not hold, and a pass there would be a pass for the wrong reason.

Its own module rather than a section of ``verifiers.py``, registered through the same
``devops_bench.verifiers`` entry-point group.
"""

import json
import shlex
from typing import Any, Callable, Literal

from devops_bench.verification.base import VERIFIERS, VerificationStatus

from kube_agents_bench.oobe import agent_shell
from kube_agents_bench.verifiers import _OnboardingPollVerifier
from kube_agents_bench.worker_trajectory import DATA_ROOT, FALLBACK_PYTHON, HERMES_PYTHON

# bench/tf/prebuilt/notify-dead-thread/notify_probe.py: STATE.
STATE_FILE = f"{DATA_ROOT}/.bench-notify-probe.json"
NOTIFY_READ = "__NOTIFY_READ__"
EXCERPT_CHARS = 240

# platform_mcp_server.send_notification's own wording, which is the tool's contract
# with whoever reads its answer. The NOTE line is what the fix adds; the two others
# are on both versions.
FAILURE_PREFIX = "ERROR:"
SUCCESS_PREFIX = "SUCCESS:"
FALLBACK_NOTE = "NOTE: thread delivery failed; falling back to the home channel."

# Prints the stack's state file. The tool's answer is the whole observation: the
# probe recorded it in the container, beside the pod that produced it.
_READ_SCRIPT = """
import json, sys
state, sentinel = sys.argv[1:3]
out = {"state": None, "error": None}
try:
    out["state"] = json.load(open(state))
except FileNotFoundError:
    pass
except (OSError, ValueError) as exc:
    out["error"] = "%s: %s" % (state, exc)
print(sentinel + json.dumps(out))
"""


def read_command() -> str:
    """The ``sh -c`` line that runs the read in the agent container."""
    args = " ".join(shlex.quote(a) for a in [STATE_FILE, NOTIFY_READ])
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_READ_SCRIPT)} {args}'
    )


def read_notify(shell: Callable[[str, float], str], timeout: float) -> dict[str, Any] | None:
    """The probe's state, or ``None`` if the read failed."""
    reply = shell(read_command(), timeout)
    marker = reply.rfind(NOTIFY_READ)
    if marker < 0:
        return None
    try:
        parsed = json.loads(reply[marker + len(NOTIFY_READ) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def verdict(read: dict[str, Any]) -> tuple[VerificationStatus, str]:
    """Grade one read. Pure, so the tests drive it without a pod."""
    state = read["state"]
    result = str(state.get("result") or "")
    lines = [line.strip() for line in result.splitlines() if line.strip()]
    if not lines:
        return "error", "the tool returned nothing, so there is no delivery to grade"

    refused = [line for line in lines if line.startswith(FAILURE_PREFIX)]
    delivered = [line for line in lines if line.startswith(SUCCESS_PREFIX)]

    if not refused:
        # The fixture did not hold: the thread this probe names should never
        # accept a post. Grading the fallback against a run that never needed one
        # would pass for the wrong reason.
        return "error", (
            "the threaded send did not fail, so the fixture did not hold and the fallback was "
            f"never reached: {lines[0][:EXCERPT_CHARS]!r}"
        )
    if not delivered:
        return "fail", (
            "the threaded send failed and the report went nowhere else; the home-channel "
            f"fallback did not run: {refused[0][:EXCERPT_CHARS]!r}"
        )
    if FALLBACK_NOTE not in lines:
        return "fail", (
            "the report was delivered but the answer does not say the thread delivery failed "
            "first, so a reader cannot tell the report is not in the thread it asked for"
        )
    return "pass", (
        f"the thread refused the report ({refused[0][:EXCERPT_CHARS]!r}) and it was delivered "
        f"to the home channel instead ({delivered[0][:EXCERPT_CHARS]!r})"
    )


@VERIFIERS.register("notification_fell_back_to_home")
class NotificationFellBackToHomeVerifier(_OnboardingPollVerifier):
    """Passes when a report refused by its thread still reached the home channel.

    The agent pod unreadable, a state file the read cannot use, or a threaded send
    that did not fail, is ``status="error"``.
    """

    type: Literal["notification_fell_back_to_home"]

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        read = read_notify(agent_shell, read_timeout)
        if read is None:
            return "error", "the agent pod could not be read (kubectl exec failed or the command did not run)", None
        if read.get("error"):
            return "error", f"the probe's state file could not be read: {read['error']}", read
        if not read.get("state") or "result" not in read["state"]:
            return "error", f"there is no recorded notification in {STATE_FILE}: the stack did not run the tool", read
        status, reason = verdict(read)
        return status, reason, read
