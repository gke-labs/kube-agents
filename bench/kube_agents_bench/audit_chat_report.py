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

"""The ``audit_chat_report_posted`` check: did the channel get the audit's report, and only that.

``bench/tf/prebuilt/audit-chat-report`` fires one scheduled fleet-audit run and
waits for it to end. Its report reaches the Session KV server's
``/v1/cron-reports`` through the job's ``deliver`` leg, and the route stores what
it posted against each thread it posted into, in the ``incidents`` table that
``incident_context`` replays into replies. That row is what the channel received.

The audit's ``finish`` leaves the report it rendered in
``chat_report_<audit>.json`` in the run's scratch directory on the shell sandbox
(``audit_report.py``, ``chat_report_path_for``). This passes when the row
stored for the job's threads since the run was fired is that report. The
``incidents`` table keeps one row per thread, the latest post, so what is graded
is the scheduled delivery, the last post of the run: a mid-run
``report_to_chat`` post before it is replaced in the table and is not graded. A
row holding anything else (the model's final message with its working notes, or
the notice that the run did not finish) fails, and so does a run that left no
report from ``finish``.

A silent run, a run that never ended, and an install with no chat platform are
errors: there is nothing in a channel to grade.
"""

import json
import math
import re
import shlex
from datetime import datetime
from typing import Any, Callable, Literal

from devops_bench.verification.base import VERIFIERS, VerificationStatus

from kube_agents_bench import onboarding
from kube_agents_bench.oobe import agent_shell
from kube_agents_bench.verifiers import _OnboardingPollVerifier
from kube_agents_bench.worker_trajectory import DATA_ROOT, FALLBACK_PYTHON, HERMES_PYTHON

# bench/tf/prebuilt/audit-chat-report/arm.py: STATE_NAME.
STATE_FILE = f"{DATA_ROOT}/.bench-audit-chat-report.json"
PROFILE = "platform"
# session_kv_server.py: SESSION_KV_DB_PATH, as the operator renders it.
SESSION_KV_DB = "/var/lib/kube-agents/session/session_kv.db"
CRON_OUTPUT = f"{DATA_ROOT}/profiles/{PROFILE}/cron/output"
# audit_report.py: SCRATCH_DIR and chat_report_path_for.
CHAT_REPORT = "/opt/data/scratch/chat_report_{audit}.json"
# session_kv_server.py: CHAT_REPORT_PARTS_SEPARATOR.
PARTS_SEPARATOR = "\n\n"
# session_kv_server.py: AUDIT_REPOS_SUMMARY_LINE.
REPOS_SUMMARY = re.compile(r": (?P<count>\d+) repositories reported in this run\.\Z")
SILENT_MARKER = "[SILENT]"
# The scheduler's `delivery_outcome` for a run whose report reached its leg.
DELIVERED = "delivered"
GATEWAY_READ = "__AUDIT_CHAT_GATEWAY__"
SANDBOX_READ = "__AUDIT_CHAT_SANDBOX__"
SANDBOX_ABSENT = "__AUDIT_CHAT_ABSENT__"
MAX_REPORT_BYTES = 1 << 20
# What read_chat_report returns for a record past MAX_REPORT_BYTES.
OVERSIZE_KEY = "__oversize__"
EXCERPT_CHARS = 240

# Prints the state file, the rows the job's threads stored since the fire, and
# the response of the newest run output written since it.
_GATEWAY_SCRIPT = """
import glob, json, os, sqlite3, sys
from datetime import datetime, timezone
state, kv_db, profile, output_root, sentinel = sys.argv[1:6]
kv_db = os.environ.get("SESSION_KV_DB_PATH") or kv_db
out = {"state": None, "posted": [], "threads": 0, "store": False, "response": None, "error": None}
try:
    out["state"] = json.load(open(state))
except FileNotFoundError:
    print(sentinel + json.dumps(out)); sys.exit(0)
except (OSError, ValueError) as exc:
    out["error"] = "%s: %s" % (state, exc); print(sentinel + json.dumps(out)); sys.exit(0)
try:
    job = out["state"]["job_id"]
    since = datetime.fromisoformat(out["state"]["fired_at"])
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    out["store"] = os.path.exists(kv_db)
    if out["store"]:
        kv = sqlite3.connect("file:" + kv_db + "?mode=ro", uri=True, timeout=10)
        threads = set()
        for (blob,) in kv.execute("SELECT metadata FROM session_metadata WHERE session_id LIKE ?",
                                  ("cron-%s-%s-%%" % (profile, job),)):
            for entry in (json.loads(blob or "{}").get("platform_threads") or {}).values():
                threads.add((entry.get("chat_id"), entry.get("thread_id")))
        out["threads"] = len(threads)
        for chat_id, thread_id in sorted(threads):
            for (text,) in kv.execute(
                    "SELECT report FROM incidents WHERE chat_id = ? AND thread_id = ? AND created_at >= ?",
                    (chat_id, thread_id, stamp)):
                out["posted"].append(text)
    runs = [p for p in glob.glob(os.path.join(output_root, job, "*.md")) if os.path.getmtime(p) >= since.timestamp()]
    if runs:
        text = open(max(runs, key=os.path.getmtime), errors="replace").read()
        at = text.rfind("\\n## Response\\n")
        out["response"] = text[at + len("\\n## Response\\n"):] if at >= 0 else None
except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as exc:
    out["error"] = str(exc)
print(sentinel + json.dumps(out))
"""


def gateway_command() -> str:
    args = " ".join(shlex.quote(a) for a in [STATE_FILE, SESSION_KV_DB, PROFILE, CRON_OUTPUT, GATEWAY_READ])
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_GATEWAY_SCRIPT)} {args}'
    )


def sandbox_command(audit: str) -> str:
    path = shlex.quote(CHAT_REPORT.format(audit=audit))
    # One byte past the cap, so a record over it is told from one at it.
    return (
        f"if [ -f {path} ]; then printf '%s' {SANDBOX_READ}; head -c {MAX_REPORT_BYTES + 1} {path}; "
        f"else printf '%s' {SANDBOX_ABSENT}; fi"
    )


def read_gateway(shell: Callable[[str, float], str], timeout: float) -> dict[str, Any] | None:
    reply = shell(gateway_command(), timeout)
    marker = reply.rfind(GATEWAY_READ)
    if marker < 0:
        return None
    try:
        parsed = json.loads(reply[marker + len(GATEWAY_READ) :])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) and isinstance(parsed.get("posted"), list) else None


def read_chat_report(shell: Callable[[str, float], str], audit: str, timeout: float) -> tuple[bool, Any]:
    """``(read, record)``: ``read`` False when the sandbox did not answer; ``record`` None when absent or unparseable."""
    reply = shell(sandbox_command(audit), timeout)
    if reply.strip() == SANDBOX_ABSENT:
        return True, None
    marker = reply.find(SANDBOX_READ)
    if marker < 0:
        return False, None
    body = reply[marker + len(SANDBOX_READ) :]
    if len(body.encode("utf-8")) > MAX_REPORT_BYTES:
        return True, {OVERSIZE_KEY: True}
    try:
        record = json.loads(body)
    except json.JSONDecodeError:
        return True, None
    return True, record if isinstance(record, dict) else None


def _without_summary(text: str, parts: int) -> str:
    """`text` without the closing line the relay adds after several reports
    (session_kv_server.py: AUDIT_REPOS_SUMMARY_LINE), when it counts `parts`."""
    head, _, last = text.rpartition(PARTS_SEPARATOR)
    match = REPOS_SUMMARY.search(last)
    if parts > 1 and head and match and int(match.group("count")) == parts:
        return head
    return text


def _is_silent(response: str | None) -> bool:
    return response is not None and response.strip().strip("*`_ ").upper() == SILENT_MARKER


def verdict(gateway: dict[str, Any], record: dict[str, Any] | None) -> tuple[VerificationStatus, str]:
    """Grade one read. Pure, so the tests drive it without a pod."""
    state = gateway["state"]
    if not state.get("last_run_at"):
        return "error", f"the fired {state.get('job_id')} run has not ended, so nothing was delivered to grade"
    if _is_silent(gateway.get("response")):
        return "error", "the run was silent, so nothing reached a channel to grade"
    posted = gateway.get("posted") or []
    if not posted:
        outcome = state.get("delivery_outcome")
        if outcome and outcome != DELIVERED:
            return "fail", f"the run's report was not delivered: {str(outcome)[:EXCERPT_CHARS]}"
        if not gateway.get("threads"):
            return "error", "the relay registered no thread for the job, so what the channel got cannot be read back"
        return "fail", "the relay stored no post for the run in the job's thread"
    if (record or {}).get(OVERSIZE_KEY):
        return "fail", f"the run's chat report record is over {MAX_REPORT_BYTES} bytes"
    fired = datetime.fromisoformat(state["fired_at"]).timestamp()
    parts = (record or {}).get("parts")
    try:
        started = float((record or {}).get("started_at"))
    except (TypeError, ValueError):
        started = None
    if started is not None and not math.isfinite(started):
        started = None
    texts = [str(p.get("text") or "").strip() for p in parts or [] if isinstance(p, dict)]
    texts = [t for t in texts if t]
    if record is None or started is None or started < fired or not texts:
        return "fail", (
            f"the run left no report from finish, so the channel got something else: "
            f"{posted[0][:EXCERPT_CHARS]!r}"
        )
    unfinished = [str(p.get("repo") or "") for p in parts or [] if isinstance(p, dict) and not str(p.get("text") or "").strip()]
    if unfinished:
        return "fail", f"the run did not finish for {', '.join(unfinished)}"
    report = PARTS_SEPARATOR.join(texts)
    changed = [text for text in posted if _without_summary(text.strip(), len(texts)) != report]
    if changed:
        return "fail", (
            f"{len(changed)} of {len(posted)} post(s) are not the report finish rendered; "
            f"the channel got {changed[0][:EXCERPT_CHARS]!r}"
        )
    return "pass", f"{len(posted)} post(s), each the report finish rendered and nothing else"


@VERIFIERS.register("audit_chat_report_posted")
class AuditChatReportPostedVerifier(_OnboardingPollVerifier):
    """Passes when every post the fired audit run produced is the report its ``finish`` rendered.

    A pod that cannot be read, or a state file the stack did not write, is
    ``status="error"``.
    """

    type: Literal["audit_chat_report_posted"]

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        gateway = read_gateway(agent_shell, read_timeout)
        if gateway is None:
            return "error", "the agent pod's stores could not be read (kubectl exec failed or the command did not run)", None
        if gateway.get("error"):
            return "error", f"the state file or the Session KV store could not be read: {gateway['error']}", gateway
        state = gateway.get("state")
        if not state or "fired_at" not in state:
            return "error", f"there is no fired run in {STATE_FILE}: the stack did not fire the audit", gateway
        read, record = read_chat_report(onboarding.sandbox_shell, str(state.get("job_id")), read_timeout)
        if not read:
            return "error", f"{onboarding.sandbox_pod()} could not be read (kubectl exec failed or the command did not run)", gateway
        status, reason = verdict(gateway, record)
        return status, reason, {**gateway, "chat_report": record}
