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

"""Ask `send_notification` to report into a thread that cannot receive it. Runs in the agent container.

Usage: python3 - <mode> <home> <scripts> <base url> < notify_probe.py

plant: writes one session row naming a Google Chat thread in a space that is not the
install's home space, then calls `platform_mcp_server.send_notification` with that
session id and records what the tool returned in the state file the verifier reads.

The thread is the point. Under `next` the A2A gateway posts a threaded notification
only into a thread of the home channel and refuses anything else outright
(`a2a/gateway/notify.go`, "is not a thread of the home channel"), so a space this
install does not own makes the threaded send fail every time, with no deleted thread
or archived space to arrange. What the two versions then do differs: the fixed tool
falls back to the home channel and says so, and the tool before the fix returns the
refusal alone and the report reaches nobody. The verifier grades that difference.

Exits 0 whatever the tool returned: the answer is the verifier's to grade. On an
install whose fix is in place this posts one real message to the home channel, which
is why the message says it is bench traffic.

clean: deletes the probe's session row and any incident row stored against its
coordinates, and removes the state file. Safe to run with nothing planted. A message
already posted to the home channel stays there.
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

mode, home, scripts, base = sys.argv[1:5]

STATE = os.path.join(home, ".bench-notify-probe.json")
SESSION_ID = "bench-notify-probe"
# A space this install does not own, so the gateway refuses the thread rather than
# posting into it. Deliberately not a real space: the refusal is the fixture.
PROBE_CHAT_ID = "spaces/BENCHNOTIFYPROBE"
PROBE_THREAD_ID = "spaces/BENCHNOTIFYPROBE/threads/BENCHNOTIFYPROBE"
# The row names its platform, so the platform guess is not what is under test here:
# this case is about a composed thread target that fails to deliver.
PROBE_PLATFORM = "google_chat"
MESSAGE = (
    "kube-agents bench probe (notification-dead-thread-falls-back). "
    "Test traffic, safe to ignore."
)
# session_kv_server.py: SESSION_KV_DB_PATH, which the container's environment carries.
SESSION_KV_DB = os.environ.get("SESSION_KV_DB_PATH", "/var/lib/kube-agents/session/session_kv.db")
SQLITE_BUSY_TIMEOUT = 10
TMP_SUFFIX = ".tmp"
# How much of the tool's answer the state file keeps, and the apply log shows.
RESULT_MAX_CHARS = 4000
LOG_RESULT_MAX_CHARS = 400


def write_state(state):
    tmp = STATE + TMP_SUFFIX
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE)


def row():
    return {
        "platform": PROBE_PLATFORM,
        "chat_id": PROBE_CHAT_ID,
        "thread_id": PROBE_THREAD_ID,
    }


def plant():
    if not os.path.exists(SESSION_KV_DB):
        sys.exit(f"no session database at {SESSION_KV_DB}; this stack needs the agent pod's own")
    with sqlite3.connect(SESSION_KV_DB, timeout=SQLITE_BUSY_TIMEOUT) as con:
        con.execute(
            "INSERT OR REPLACE INTO session_metadata (session_id, metadata) VALUES (?, ?)",
            (SESSION_ID, json.dumps(row())),
        )

    state = {
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "session_id": SESSION_ID,
        "chat_id": PROBE_CHAT_ID,
        "thread_id": PROBE_THREAD_ID,
    }
    write_state(state)

    # The tool, imported from the PVC copy that actually runs, not the image copy.
    sys.path.insert(0, scripts)
    import platform_mcp_server  # noqa: E402

    state["result"] = platform_mcp_server.send_notification(MESSAGE, session_id=SESSION_ID)[:RESULT_MAX_CHARS]
    write_state(state)
    print(f"planted {SESSION_ID}: {state['result'][:LOG_RESULT_MAX_CHARS]}")


def clean():
    rows = 0
    if os.path.exists(SESSION_KV_DB):
        with sqlite3.connect(SESSION_KV_DB, timeout=SQLITE_BUSY_TIMEOUT) as con:
            rows += con.execute(
                "DELETE FROM incidents WHERE chat_id = ? AND thread_id = ?",
                (PROBE_CHAT_ID, PROBE_THREAD_ID),
            ).rowcount
            con.execute("DELETE FROM session_metadata WHERE session_id = ?", (SESSION_ID,))
    try:
        os.remove(STATE)
    except FileNotFoundError:
        pass
    print(f"cleaned: session row removed, {rows} stored post(s) removed")


if mode == "plant":
    plant()
elif mode == "clean":
    clean()
else:
    sys.exit(f"unknown mode {mode!r}")
