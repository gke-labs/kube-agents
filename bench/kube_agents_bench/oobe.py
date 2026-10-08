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
state file, and the stage records when it marked each audit; this passes when every audit
has a run of its own claimed at or after its mark that got going: running, completed, or
ended after it started.

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

# agents/chat/scripts/oobe.py: FIRST_RUN_AUDITS, in the order the stage chains them.
FIRST_RUN_AUDITS = ("fleet-wide-cost-analysis", "compliance-audit", "obtainability-audit", "stockout-prevention")
# bench/tf/prebuilt/oobe-first-run-audits/arm.py: STATE.
STATE_FILE = f"{DATA_ROOT}/.bench-oobe.json"
# agents/chat/scripts/oobe.py: AUDITS_MARKER, with its STATE_FIRED list (what the stage marked
# due) and STATE_MARKS (when). Still in place when the verifier runs; the teardown restores it.
AUDITS_MARKER = f"{DATA_ROOT}/.oobe_audits_fired"
PLATFORM_EXECUTIONS_DB = f"{DATA_ROOT}/profiles/platform/cron/executions.db"
STARTS_READ = "__OOBE_STARTS_READ__"
# A run that got going by its status; a run that ended otherwise counts too when it has a
# started_at (it began, then failed or was cut off). A row with no start time ran nothing.
STARTED_STATUSES = ("running", "completed")
# The gateway Deployment, as the stack names it (variables.tf: agent_deployment). Exec goes
# there rather than through AGENT_SERVICE_NAME, which can name a tunnel in front of the
# gateway that has none of its containers.
DEFAULT_AGENT_DEPLOYMENT = "platform-agent-gateway"

# Prints the arm time, the audits the stage recorded marking due, and each audit's first run
# claimed at or after its mark (the arm, for an audit with no mark). A skipped row is passed
# over: it is a mark that found the audit already running, not a run. A run the stage did not
# mark is a scheduled one that fell in the window, not the stage's. A missing
# state file, a sqlite failure or a timestamp that does not parse is printed as an
# error rather than raised, so the verdict names it instead of reading as an
# unreachable pod.
_STARTS_SCRIPT = """
import json, os, sqlite3, sys
from datetime import datetime
state, marker, db, sentinel, audits = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:]
SQLITE_BUSY_TIMEOUT = 10
SKIPPED = "skipped"
out = {"applied_at": None, "marked": [], "adopted": [], "runs": {}, "error": None, "held": {}, "gave_up": [], "skipped": None}
marks = {}
try:
    out["applied_at"] = json.load(open(state))["applied_at"]
except FileNotFoundError:
    pass
except (OSError, ValueError, KeyError, TypeError) as exc:
    out["error"] = "%s: %s" % (state, exc)
try:
    recorded = json.load(open(marker))
    out["adopted"] = [a for a in recorded.get("adopted", []) if isinstance(a, str)]
    out["marked"] = [a for a in recorded.get("fired", []) if isinstance(a, str) and a not in out["adopted"]]
    out["held"] = recorded.get("held") or {}
    out["gave_up"] = recorded.get("gave_up") or []
    out["skipped"] = recorded.get("reason") if recorded.get("skipped") else None
    marks = {a: t for a, t in (recorded.get("marks") or {}).items() if isinstance(t, (int, float))}
except (OSError, ValueError, AttributeError, TypeError):
    pass
# A store not yet created has no runs, as the stage and the runner read it.
if out["applied_at"] and not out["error"] and os.path.exists(db):
    try:
        armed = datetime.fromisoformat(out["applied_at"]).timestamp()
        con = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT)
        for audit in audits:
            since = max(armed, marks.get(audit, armed))
            for status, claimed, finished, began in con.execute(
                "SELECT status, claimed_at, finished_at, started_at FROM executions WHERE job_id = ? AND claimed_at IS NOT NULL"
                " ORDER BY claimed_at", (audit,)):
                if status != SKIPPED and datetime.fromisoformat(claimed).timestamp() >= since:
                    out["runs"][audit] = {"status": status, "claimed_at": claimed, "finished_at": finished, "started_at": began}
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
    args = " ".join(
        shlex.quote(a) for a in [STATE_FILE, AUDITS_MARKER, PLATFORM_EXECUTIONS_DB, STARTS_READ, *FIRST_RUN_AUDITS]
    )
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


def _overlaps(runs: dict[str, dict[str, Any]], chain: list[str]) -> list[str]:
    """Each audit in ``chain`` that started before the one before it had ended."""
    found = []
    for earlier, later in zip(chain, chain[1:]):
        ended = runs[earlier].get("finished_at")
        began = runs[later]["claimed_at"]
        if not ended or datetime.fromisoformat(began) < datetime.fromisoformat(ended):
            found.append(f"{later} started at {began} while {earlier} {'ran until ' + ended if ended else 'had not ended'}")
    return found


@VERIFIERS.register("oobe_audits_started")
class OobeAuditsStartedVerifier(_OnboardingPollVerifier):
    """Passes once the stage marked every first-run audit due, each has a run since its mark that
    got going (running, completed, or ended after its start), and each started only after the one
    before it in the chain ended. An audit the stage adopted (a run completed since the sweep, not
    marked) counts as had and is left out of that order; at least one must be the stage's own.

    A row with no start time does not count: a run cut off at its start leaves exactly that. Nor does a run the stage did not mark: a scheduled run that falls in the window is
    not the stage's. Past running, the outcome is the audit's own, graded by the audit
    cases.
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
        runs, marked = read["runs"], set(read.get("marked") or [])
        # An audit the stage passed over because it had completed a run since the sweep (a
        # scheduled one): already had, so not required to be the stage's, and outside the chain.
        adopted = [a for a in FIRST_RUN_AUDITS if a in set(read.get("adopted") or [])]
        # A run that got going and then failed, or was cut off by a restart, still started: how it
        # ended is the audit cases' to grade. A row with no start time ran nothing.
        running = [
            a for a in FIRST_RUN_AUDITS
            if runs.get(a, {}).get("status") in STARTED_STATUSES or runs.get(a, {}).get("started_at")
        ]
        started = [a for a in running if a in marked]
        if started and len(started) + len(adopted) == len(FIRST_RUN_AUDITS):
            overlaps = _overlaps(runs, started)
            if overlaps:
                return "fail", f"the first-run audits overlapped instead of running one after another: {'; '.join(overlaps)}", read
            also = f"; {', '.join(adopted)} had already completed a run since the sweep and was not run again" if adopted else ""
            return "pass", f"{len(started)} first-run audits were marked due by the stage and ran one after another since {armed}{also}", read
        stalled = [f"{a} ({runs[a].get('status')})" for a in FIRST_RUN_AUDITS if a in runs and a not in running and a not in adopted]
        missing = [a for a in FIRST_RUN_AUDITS if a not in runs and a not in adopted]
        unmarked = [a for a in running if a not in marked and a not in adopted]
        parts = []
        if missing:
            parts.append(f"no run claimed since {armed} and its mark for {', '.join(missing)}")
        if stalled:
            parts.append(f"a run that did not get going for {', '.join(stalled)}")
        if unmarked:
            parts.append(f"a run the stage did not mark due (a scheduled one) for {', '.join(unmarked)}")
        if not started and not stalled and not unmarked:
            parts.append("nothing started the first-run audits")
        # The stage's own account of an audit it did not mark, so a red names the install's state.
        if read.get("skipped"):
            parts.append(f"the stage skipped the first-run audits: {read['skipped']}")
        held = read.get("held") or {}
        if held:
            parts.append(f"the stage held {', '.join(f'{a} ({why})' for a, why in sorted(held.items()))}")
        if read.get("gave_up"):
            parts.append(f"the stage gave up on {', '.join(read['gave_up'])} after its marks were never claimed")
        return "fail", "; ".join(parts), read
