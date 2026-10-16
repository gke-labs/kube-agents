"""The ``oobe_late_report`` check: does the report sent on a late first message carry the audits' results.

A first-run audit that ends before anyone has opened the chat finds no home channel: the relay
refuses its summary, the run's ``delivery_outcome`` is ``failed``, and nothing posts it later. The
onboarding report the delivery stage (``agents/chat/scripts/bootstrap_delivery.py``) sends on the
first message is then the first thing that user reads.

The case's stack (``bench/tf/prebuilt/oobe-late-first-message``) clears the home channel, lets the
four audits run, then opens the chat. This passes when the delivered report links the ledger issue
of every audit the stage started whose summary failed and named one in its final message, and names
every other such audit as not posted. It needs at least one failed summary: without one the stack did not take the
home channel away, and the case tested nothing.

Its own module, registered through the ``devops_bench.verifiers`` entry-point group.
"""

import json
import shlex
from typing import Any, Callable, Literal

from devops_bench.verification.base import VERIFIERS, VerificationStatus

from kube_agents_bench.oobe import AUDITS_MARKER, FIRST_RUN_AUDITS, STATE_FILE, agent_shell
from kube_agents_bench.verifiers import _OnboardingPollVerifier
from kube_agents_bench.worker_trajectory import DATA_ROOT, FALLBACK_PYTHON, HERMES_PYTHON

# The report names only audits the stage started (`fired` in AUDITS_MARKER), so only those are
# graded; held, given-up and skipped audits are #3283.
# bench/tf/prebuilt/oobe-late-first-message/late.py: STATE, with the time the chat was opened.
LATE_STATE_FILE = f"{DATA_ROOT}/.bench-oobe-late.json"
PLATFORM_EXECUTIONS_DB = f"{DATA_ROOT}/profiles/platform/cron/executions.db"
PLATFORM_OUTPUT_DIR = f"{DATA_ROOT}/profiles/platform/cron/output"
# The default profile's saved runs of the `oobe` job, whose delivery stage prints the report.
OOBE_OUTPUT_DIR = f"{DATA_ROOT}/cron/output/oobe"
# The first line of the stand-in report the stack plants (report.txt).
REPORT_TITLE = "Onboarding report (oobe-late-first-message eval)"
# bootstrap_delivery.py: AUDITS_UNDELIVERED_NOTE, the words that say a summary was never posted.
NOT_POSTED = "summaries were not posted"
DELIVERY_FAILED = "failed"
LATE_READ = "__OOBE_LATE_READ__"

# Prints the open time, each audit's last completed run since the arm with its delivery outcome
# and the ledger link in its saved final message, and the saved `oobe` run that delivered the
# stand-in report after the open. Read failures are printed as an error, not raised.
_LATE_SCRIPT = r"""
import json, os, re, sqlite3, sys
from datetime import datetime
arm_state, late_state, marker, db, out_dir, oobe_dir, title, sentinel, audits = (
    sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], sys.argv[6], sys.argv[7], sys.argv[8], sys.argv[9:])
SQLITE_BUSY_TIMEOUT = 10
SLACK_SECONDS = 300
HEADING = "## Response"
LEDGER = re.compile(r"https://[^\s()<>\[\]]+/issues/\d+")
LEDGER_WORD = "ledger"
out = {"applied_at": None, "opened_at": None, "fired": [], "runs": {}, "report": None, "error": None}

def ts(value):
    return datetime.fromisoformat(value).timestamp()

def newest(directory, since):
    try:
        docs = [os.path.join(directory, n) for n in os.listdir(directory) if n.endswith(".md")]
    except OSError:
        return []
    return sorted((p for p in docs if os.path.getmtime(p) >= since), key=os.path.getmtime, reverse=True)

def response(path):
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read().rpartition(HEADING)[2]

try:
    out["applied_at"] = json.load(open(arm_state))["applied_at"]
    out["opened_at"] = json.load(open(late_state)).get("opened_at")
except FileNotFoundError:
    pass
except (OSError, ValueError, KeyError, TypeError) as exc:
    out["error"] = "state: %s" % exc
try:
    out["fired"] = [a for a in json.load(open(marker)).get("fired", []) if isinstance(a, str)]
except (OSError, ValueError, AttributeError, TypeError):
    pass
if out["applied_at"] and not out["error"] and os.path.exists(db):
    try:
        armed = ts(out["applied_at"])
        con = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT)
        for audit in audits:
            last = None
            for claimed, finished, outcome in con.execute(
                "SELECT claimed_at, finished_at, delivery_outcome FROM executions"
                " WHERE job_id = ? AND status = 'completed' ORDER BY claimed_at", (audit,)):
                if ts(claimed) >= armed:
                    last = {"claimed_at": claimed, "finished_at": finished, "delivery_outcome": outcome, "ledger": None}
            if last is not None:
                since = ts(last["finished_at"]) - SLACK_SECONDS if last["finished_at"] else armed
                docs = newest(os.path.join(out_dir, audit), since)
                if docs:
                    text = response(docs[0])
                    named = [l for l in text.splitlines() if LEDGER_WORD in l.lower() and LEDGER.search(l)]
                    match = LEDGER.search(named[0] if named else text)
                    last["ledger"] = match.group(0) if match else None
                out["runs"][audit] = last
    except (sqlite3.Error, ValueError, TypeError, OSError) as exc:
        out["error"] = "%s: %s" % (db, exc)
if out["opened_at"] and not out["error"]:
    for doc in newest(oobe_dir, ts(out["opened_at"])):
        text = response(doc)
        if title in text:
            out["report"] = text
            break
print(sentinel + json.dumps(out))
"""


def late_command() -> str:
    """The ``sh -c`` line that runs the read in the agent container."""
    args = " ".join(
        shlex.quote(a)
        for a in [
            STATE_FILE, LATE_STATE_FILE, AUDITS_MARKER, PLATFORM_EXECUTIONS_DB, PLATFORM_OUTPUT_DIR, OOBE_OUTPUT_DIR,
            REPORT_TITLE, LATE_READ, *FIRST_RUN_AUDITS,
        ]
    )
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_LATE_SCRIPT)} {args}'
    )


def read_late(shell: Callable[[str, float], str], timeout: float) -> dict[str, Any] | None:
    """What the read printed, or ``None`` if it did not run."""
    reply = shell(late_command(), timeout)
    marker = reply.rfind(LATE_READ)
    if marker < 0:
        return None
    try:
        parsed = json.loads(reply[marker + len(LATE_READ) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("runs"), dict):
        return None
    return parsed


def grade(read: dict[str, Any]) -> tuple[VerificationStatus, str]:
    """The verdict on one read."""
    if read.get("error"):
        return "error", f"the stack's state files or the Platform Agent's cron store could not be read: {read['error']}"
    if not read.get("applied_at") or not read.get("opened_at"):
        return "error", "the stack did not arm the stage and open the chat (no applied_at or opened_at in its state files)"
    runs = read["runs"]
    fired = set(read.get("fired") or [])
    if not fired:
        return "error", "the oobe stage's marker records no audit it started (missing, unreadable, or a skip), so nothing could be graded"
    failed = [a for a in FIRST_RUN_AUDITS if a in fired and runs.get(a, {}).get("delivery_outcome") == DELIVERY_FAILED]
    if not failed:
        outcomes = ", ".join(f"{a} ({runs[a].get('delivery_outcome')})" for a in FIRST_RUN_AUDITS if a in runs) or "none"
        return "error", f"no first-run audit's summary failed to deliver, so the home channel was not taken away; runs: {outcomes}"
    report = read.get("report")
    if not report:
        return "fail", f"no saved run of the oobe job since {read['opened_at']} carries the planted report: nothing was delivered on the first message"
    missing_links = [f"{a} ({runs[a]['ledger']})" for a in failed if runs[a].get("ledger") and runs[a]["ledger"] not in report]
    unlinked = [a for a in failed if not runs[a].get("ledger")]
    after = report.partition(NOT_POSTED)[2] if NOT_POSTED in report else ""
    unnamed = [a for a in unlinked if f"`{a}`" not in after]
    if missing_links or unnamed:
        parts = []
        if missing_links:
            parts.append(f"it does not link the ledger issue of {', '.join(missing_links)}")
        if unnamed:
            parts.append(f"it does not name {', '.join(unnamed)} as not posted")
        return "fail", f"the report sent on the first message leaves out audits whose summaries were never posted: {'; '.join(parts)}"
    linked = [a for a in failed if runs[a].get("ledger")]
    return "pass", (
        f"the report sent on the first message carries the {len(failed)} summaries that never reached the chat"
        f" ({len(linked)} with their ledger link)"
    )


@VERIFIERS.register("oobe_late_report")
class OobeLateReportVerifier(_OnboardingPollVerifier):
    """Passes once the report delivered on the first message links the ledger issue of every
    first-run audit the stage started whose summary failed to deliver, and names as not posted any
    such audit whose final message had no link. At least one summary must have failed; none is ``status="error"``,
    as is an unreadable agent pod or state file.
    """

    type: Literal["oobe_late_report"]

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        read = read_late(agent_shell, read_timeout)
        if read is None:
            return "error", "the agent pod could not be read (kubectl exec failed or the command did not run)", None
        status, reason = grade(read)
        return status, reason, read
