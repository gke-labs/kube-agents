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

"""Hand one scheduled report to the chat relay, and clean up after it. Runs in the agent container.

Usage: python3 - <mode> <home> <hermes> <base url> [<report, base64>] < relay_probe.py

post: POSTs the report to the Session KV server's /v1/cron-reports on loopback, the
call the scheduler's relay plugin makes for every `deliver: "chat"` job
(deploy/docker/plugins/chat/adapter.py), and records the time, the report and the
route's answer in the state file the verifier reads. The container's environment
carries the SESSION_KV_API_KEY the route requires. Exits 0 whatever the route
answered: the answer is the verifier's to grade.

clean: archives every card filed from the probe's relay sessions (an image that runs
an agent turn over the report can file one) and removes the state file. Safe to run
with nothing posted. The post itself stays in the channel.
"""

import base64
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

mode, home, hermes, base = sys.argv[1:5]
STATE = os.path.join(home, ".bench-relay-probe.json")
JOB_ID = "bench-relay-probe"
PROFILE = "platform"
TITLE = "Bench relay probe"
KANBAN_DB = os.path.join(home, "kanban.db")
# session_kv_server._cron_report_session_id, for this profile and job.
SESSION_PREFIX = f"cron-{PROFILE}-{JOB_ID}-"
# The relay plugin's RELAY_TIMEOUT_SECONDS, plus room for the connect.
TIMEOUT_SECONDS = 400.0
SQLITE_BUSY_TIMEOUT = 10
TMP_SUFFIX = ".tmp"


def write_state(state):
    tmp = STATE + TMP_SUFFIX
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE)


def post(report):
    token = (os.environ.get("SESSION_KV_API_KEY") or "").strip()
    if not token:
        sys.exit("SESSION_KV_API_KEY is not set in this container, so the route would refuse the report")
    state = {"applied_at": datetime.now(timezone.utc).isoformat(), "job_id": JOB_ID, "report": report}
    write_state(state)
    body = json.dumps({"job_id": JOB_ID, "profile": PROFILE, "title": TITLE, "report": report}).encode()
    req = urllib.request.Request(
        f"{base}/v1/cron-reports",
        data=body,
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            state["status"], state["answer"] = resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        state["status"], state["answer"] = exc.code, {"detail": exc.read().decode(errors="replace")[:2000]}
    write_state(state)
    print(f"posted {JOB_ID}: HTTP {state['status']} {json.dumps(state['answer'])[:300]}")


def clean():
    cards = []
    if os.path.exists(KANBAN_DB):
        con = sqlite3.connect(f"file:{KANBAN_DB}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT)
        cards = [
            row[0]
            for row in con.execute(
                "SELECT id FROM tasks WHERE session_id LIKE ? AND status != 'archived'", (SESSION_PREFIX + "%",)
            )
        ]
        con.close()
    for card in cards:
        subprocess.run([hermes, "kanban", "archive", card], capture_output=True, text=True, check=False)
    try:
        os.remove(STATE)
    except FileNotFoundError:
        pass
    print(f"cleaned: {len(cards)} card(s) archived")


if mode == "post":
    post(base64.b64decode(sys.argv[5]).decode("utf-8"))
elif mode == "clean":
    clean()
else:
    sys.exit(f"unknown mode {mode!r}")
