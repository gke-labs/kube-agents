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

"""The ``oobe_audits_started`` check: did the first-run audits stage start every audit.

``agents/chat/scripts/oobe.py`` starts four Platform Agent audits once the onboarding
scan settles, by marking each due on that profile's roster. A started audit leaves a
row in the profile's ``cron/executions.db``. The case's stack
(``bench/tf/prebuilt/oobe-first-run-audits``) records when it armed the stage in its
state file; this passes when every audit has a run claimed at or after that time that is
running or has completed.

Its own module rather than a section of ``verifiers.py``, registered through the same
``devops_bench.verifiers`` entry-point group.
"""

import json
import os
import shlex
from datetime import datetime
from typing import Any, Callable, Literal

from devops_bench.verification.base import VERIFIERS, VerificationStatus

from kube_agents_bench import onboarding
from kube_agents_bench.verifiers import _OnboardingPollVerifier
from kube_agents_bench.worker_trajectory import DATA_ROOT, FALLBACK_PYTHON, HERMES_PYTHON

# agents/chat/scripts/oobe.py: FIRST_RUN_AUDITS.
FIRST_RUN_AUDITS = ("compliance-audit", "obtainability-audit", "fleet-wide-cost-analysis", "stockout-prevention")
# bench/tf/prebuilt/oobe-first-run-audits/arm.py: STATE.
STATE_FILE = f"{DATA_ROOT}/.bench-oobe.json"
PLATFORM_EXECUTIONS_DB = f"{DATA_ROOT}/profiles/platform/cron/executions.db"
STARTS_READ = "__OOBE_STARTS_READ__"
# A run that got going: the scheduler has it, or it ended without being skipped or lost.
# A claimed row the run never left, a skipped one and a failed one say the audit did not run.
STARTED_STATUSES = ("running", "completed")
# The gateway Deployment, as the stack names it (variables.tf: agent_deployment). Exec goes
# there rather than through AGENT_SERVICE_NAME, which can name a tunnel in front of the
# gateway that has none of its containers.
DEFAULT_AGENT_DEPLOYMENT = "platform-agent-gateway"

# Prints the arm time and each audit's newest run claimed at or after it. A missing
# state file, a sqlite failure or a timestamp that does not parse is printed as an
# error rather than raised, so the verdict names it instead of reading as an
# unreachable pod.
_STARTS_SCRIPT = """
import json, os, sqlite3, sys
from datetime import datetime
state, db, sentinel, audits = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
SQLITE_BUSY_TIMEOUT = 10
out = {"applied_at": None, "runs": {}, "error": None}
try:
    out["applied_at"] = json.load(open(state))["applied_at"]
except FileNotFoundError:
    pass
except (OSError, ValueError, KeyError, TypeError) as exc:
    out["error"] = "%s: %s" % (state, exc)
if out["applied_at"] and not out["error"]:
    try:
        armed = datetime.fromisoformat(out["applied_at"]).timestamp()
        con = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT)
        for audit in audits:
            for status, claimed in con.execute(
                "SELECT status, claimed_at FROM executions WHERE job_id = ? AND claimed_at IS NOT NULL"
                " ORDER BY claimed_at DESC", (audit,)):
                if datetime.fromisoformat(claimed).timestamp() >= armed:
                    out["runs"][audit] = {"status": status, "claimed_at": claimed}
                break
    except (sqlite3.Error, ValueError, TypeError) as exc:
        out["error"] = "%s: %s" % (db, exc)
print(sentinel + json.dumps(out))
"""


def agent_shell(script: str, timeout: float) -> str:
    """Run ``script`` in the gateway's agent container. Best effort: any failure returns ``""``."""
    deployment = os.environ.get("AGENT_DEPLOYMENT", DEFAULT_AGENT_DEPLOYMENT)
    container = os.environ.get("AGENT_CONTAINER", onboarding.DEFAULT_AGENT_CONTAINER)
    return onboarding._kubectl_exec(f"deployment/{deployment}", container, script, timeout)


def starts_command() -> str:
    """The ``sh -c`` line that runs the read in the agent container."""
    args = " ".join(shlex.quote(a) for a in [STATE_FILE, PLATFORM_EXECUTIONS_DB, STARTS_READ, *FIRST_RUN_AUDITS])
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_STARTS_SCRIPT)} {args}'
    )


def read_starts(shell: Callable[[str, float], str], timeout: float) -> dict[str, Any] | None:
    """The arm time and the audits started since, or ``None`` if the read failed."""
    reply = shell(starts_command(), timeout)
    marker = reply.rfind(STARTS_READ)
    if marker < 0:
        return None
    try:
        parsed = json.loads(reply[marker + len(STARTS_READ) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("runs"), dict):
        return None
    return parsed


@VERIFIERS.register("oobe_audits_started")
class OobeAuditsStartedVerifier(_OnboardingPollVerifier):
    """Passes once every first-run audit has a run since the arm that is running or completed.

    A row only claimed, skipped or failed does not count: a run cut off at its start leaves
    exactly that. Past running, the outcome is the audit's own, graded by the audit cases.
    The agent pod unreadable, or a state file or cron store the read cannot use, is
    ``status="error"``.
    """

    type: Literal["oobe_audits_started"]

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        read = read_starts(agent_shell, read_timeout)
        if read is None:
            return "error", "the agent pod's cron store could not be read (kubectl exec failed or the command did not run)", None
        if read.get("error"):
            return "error", f"the stack's state file or the Platform Agent's cron store could not be read: {read['error']}", read
        if not read.get("applied_at"):
            return "error", f"there is no {STATE_FILE}: the stack did not arm the stage", read
        armed = datetime.fromisoformat(read["applied_at"]).isoformat()
        runs = read["runs"]
        started = [a for a in FIRST_RUN_AUDITS if runs.get(a, {}).get("status") in STARTED_STATUSES]
        if len(started) == len(FIRST_RUN_AUDITS):
            return "pass", f"all {len(FIRST_RUN_AUDITS)} first-run audits are running or done since {armed}", read
        stalled = [f"{a} ({runs[a].get('status')})" for a in FIRST_RUN_AUDITS if a in runs and a not in started]
        missing = [a for a in FIRST_RUN_AUDITS if a not in runs]
        parts = []
        if missing:
            parts.append(f"no run claimed since {armed} for {', '.join(missing)}")
        if stalled:
            parts.append(f"a run that did not get going for {', '.join(stalled)}")
        if not started and not stalled:
            parts.append("nothing started the first-run audits")
        return "fail", "; ".join(parts), read
