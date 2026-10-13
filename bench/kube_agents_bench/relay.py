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

"""The ``relay_posted_as_written`` check: did the chat get the scheduled report the job wrote.

``bench/tf/prebuilt/relay-report`` hands one report to the Session KV server's
``/v1/cron-reports`` (``agents/platform/scripts/session_kv_server.py``,
``relay_cron_report``) and records the route's answer. The route stores what it posted
against each thread it posted into, in the ``incidents`` table that ``incident_context``
replays into replies, so that row is what the channel received. This passes when the
route delivered and every row stored for the probe's threads since the post is the
report, exactly.

An install with no chat platform (the CI install) posts nothing and stores nothing,
and that is a fail naming the route's answer: the case grades a channel, and there is
none.

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

# bench/tf/prebuilt/relay-report/relay_probe.py: STATE, JOB_ID and PROFILE.
STATE_FILE = f"{DATA_ROOT}/.bench-relay-probe.json"
PROFILE = "platform"
# session_kv_server.py: SESSION_KV_DB_PATH, as the operator renders it.
SESSION_KV_DB = "/var/lib/kube-agents/session/session_kv.db"
RELAY_READ = "__RELAY_READ__"
DELIVERED = "delivered"
EXCERPT_CHARS = 240

# Prints the stack's state file and the text stored for each thread the probe's report
# was posted to since the post. The routing rows are keyed by
# session_kv_server._cron_report_session_id, one per job per UTC day.
_READ_SCRIPT = """
import json, os, sqlite3, sys
from datetime import datetime, timezone
state, kv_db, profile, sentinel = sys.argv[1:5]
SQLITE_BUSY_TIMEOUT = 10
out = {"state": None, "posted": [], "error": None}
try:
    out["state"] = json.load(open(state))
except FileNotFoundError:
    print(sentinel + json.dumps(out)); sys.exit(0)
except (OSError, ValueError) as exc:
    out["error"] = "%s: %s" % (state, exc); print(sentinel + json.dumps(out)); sys.exit(0)
try:
    since = datetime.fromisoformat(out["state"]["applied_at"])
    stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    prefix = "cron-%s-%s-" % (profile, out["state"]["job_id"])
    if os.path.exists(kv_db):
        kv = sqlite3.connect("file:" + kv_db + "?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT)
        threads = set()
        for (blob,) in kv.execute("SELECT metadata FROM session_metadata WHERE session_id LIKE ?", (prefix + "%",)):
            for entry in (json.loads(blob or "{}").get("platform_threads") or {}).values():
                threads.add((entry.get("chat_id"), entry.get("thread_id")))
        for chat_id, thread_id in sorted(threads):
            for (text,) in kv.execute(
                    "SELECT report FROM incidents WHERE chat_id = ? AND thread_id = ? AND created_at >= ?",
                    (chat_id, thread_id, stamp)):
                out["posted"].append(text)
except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as exc:
    out["error"] = str(exc)
print(sentinel + json.dumps(out))
"""


def read_command() -> str:
    """The ``sh -c`` line that runs the read in the agent container."""
    args = " ".join(shlex.quote(a) for a in [STATE_FILE, SESSION_KV_DB, PROFILE, RELAY_READ])
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_READ_SCRIPT)} {args}'
    )


def read_relay(shell: Callable[[str, float], str], timeout: float) -> dict[str, Any] | None:
    """The probe's state and what its threads stored, or ``None`` if the read failed."""
    reply = shell(read_command(), timeout)
    marker = reply.rfind(RELAY_READ)
    if marker < 0:
        return None
    try:
        parsed = json.loads(reply[marker + len(RELAY_READ) :])
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or not isinstance(parsed.get("posted"), list):
        return None
    return parsed


def verdict(read: dict[str, Any]) -> tuple[VerificationStatus, str]:
    """Grade one read. Pure, so the tests drive it without a pod."""
    state = read["state"]
    report = str(state.get("report") or "").strip()
    answer = state.get("answer") or {}
    if state.get("status") != 200 or answer.get("status") != DELIVERED:
        return "fail", f"the relay did not deliver the report: HTTP {state.get('status')} {json.dumps(answer)[:EXCERPT_CHARS]}"
    posted = read.get("posted") or []
    if not posted:
        return "fail", "the relay answered delivered but stored no post for the report's thread"
    changed = [text for text in posted if text.strip() != report]
    if changed:
        return "fail", (
            f"{len(changed)} of {len(posted)} post(s) are not the report as the job wrote it; "
            f"the channel got {changed[0][:EXCERPT_CHARS]!r}"
        )
    return "pass", f"{len(posted)} post(s), each the report as the job wrote it"


@VERIFIERS.register("relay_posted_as_written")
class RelayPostedAsWrittenVerifier(_OnboardingPollVerifier):
    """Passes when every post the probe's report produced is the report, exactly.

    The agent pod unreadable, or a state file or store the read cannot use, is
    ``status="error"``.
    """

    type: Literal["relay_posted_as_written"]

    def _check(self, read_timeout: float) -> tuple[VerificationStatus, str, dict[str, Any] | None]:
        read = read_relay(agent_shell, read_timeout)
        if read is None:
            return "error", "the agent pod's stores could not be read (kubectl exec failed or the command did not run)", None
        if read.get("error"):
            return "error", f"the probe's state file or the Session KV store could not be read: {read['error']}", read
        if not read.get("state") or "status" not in read["state"]:
            return "error", f"there is no answered post in {STATE_FILE}: the stack did not post the report", read
        status, reason = verdict(read)
        return status, reason, read
