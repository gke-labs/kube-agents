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

"""The late-first-message half of the case. Runs in the agent container.

Usage: python3 - <home> <command> < late.py

- ``clear``: record the home channel and the delivery markers, then take them away, so the
  install is one nobody has spoken to: no ``platforms.<p>.home_channel`` in ``config.yaml``, no
  ``<P>_HOME_CHANNEL`` in ``.env``, no ``.user_aligned`` and no ``.bootstrap_completed``. Refuses
  an install whose container environment names a home channel (the CR's ``homeChannel``), which
  this cannot take away, and one whose ``oobe`` job is bound to a chat, which would post the
  report there.
- ``finished``: print ``finished`` once each first-run audit has a run claimed at or after the
  stage's arm (``applied_at`` in oobe-first-run-audits/arm.py's state file) that has ended, or the
  stage recorded it as adopted, held or given up on, and none has a run still going, else
  ``pending``.
- ``open``: touch ``.user_aligned``, as the onboarding plugin does on a person's first message,
  and record when.
- ``restore``: put back what ``clear`` recorded, byte for byte.

The state file is written before anything changes, so ``restore`` undoes a ``clear`` that
stopped part-way.
"""

import ast
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone

home, command = sys.argv[1:3]
STATE = os.path.join(home, ".bench-oobe-late.json")
# oobe-first-run-audits/arm.py: STATE.
ARM_STATE = os.path.join(home, ".bench-oobe.json")
# agents/chat/scripts/oobe.py: AUDITS_MARKER, and the audits it records as having had their turn
# without a run of the stage's own: adopted (a run completed since the sweep), held, given up on.
AUDITS_MARKER = os.path.join(home, ".oobe_audits_fired")
HAD_TURN_KEYS = ("adopted", "gave_up", "held")
CONFIG = os.path.join(home, "config.yaml")
ENV_FILE = os.path.join(home, ".env")
ALIGNED = os.path.join(home, ".user_aligned")
COMPLETED = os.path.join(home, ".bootstrap_completed")
EXECUTIONS_DB = os.path.join(home, "profiles", "platform", "cron", "executions.db")
TMP_SUFFIX = ".tmp"
# The stage's audit list is read from the image's copy of oobe.py, not run, as
# hack/ci_platform_runs.py reads it, so the chat opens only once every audit on the deployed
# stage's list has had its turn.
STAGE_SOURCE = os.environ.get("OOBE_STAGE_SOURCE", "/opt/defaults/scripts/oobe.py")
STAGE_AUDITS = "FIRST_RUN_AUDITS"
IN_FLIGHT = ("claimed", "running")
# A skipped row ran nothing: the stage marks the audit again, or the skip found a run still going.
SKIPPED = "skipped"
# Hermes' home-channel variables, as persist_home_channel's .env mirror writes them
# (bootstrap_onboarding plugin: HOME_CHANNEL_ENV_FORMAT and HOME_THREAD_ENV_SUFFIX).
HOME_CHANNEL_VARS = ("GOOGLE_CHAT_HOME_CHANNEL", "SLACK_HOME_CHANNEL")
THREAD_SUFFIX = "_THREAD_ID"
HOME_CHANNEL_KEY = "home_channel"
OOBE_JOB = "oobe"
LOCAL_DELIVERY = "local"
SQLITE_BUSY_TIMEOUT = 10


def read(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def write(path, text):
    tmp = path + TMP_SUFFIX
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def put_back(path, text):
    if text is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    write(path, text)


def load_state():
    with open(STATE, encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state):
    write(STATE, json.dumps(state))


def without_home_channel(text):
    import yaml

    cfg = yaml.safe_load(text) or {}
    platforms = cfg.get("platforms") if isinstance(cfg, dict) else None
    if not isinstance(platforms, dict):
        return text
    changed = False
    for block in platforms.values():
        if isinstance(block, dict) and HOME_CHANNEL_KEY in block:
            del block[HOME_CHANNEL_KEY]
            changed = True
    return yaml.safe_dump(cfg, sort_keys=False) if changed else text


def without_home_vars(text):
    names = {v for base in HOME_CHANNEL_VARS for v in (base, base + THREAD_SUFFIX)}
    kept = [line for line in text.splitlines(keepends=True) if line.split("=", 1)[0].strip() not in names]
    return "".join(kept)


def clear():
    if os.path.exists(STATE):
        sys.exit(f"{STATE} exists: the case is already armed")
    named = [v for v in HOME_CHANNEL_VARS if os.environ.get(v, "").strip()]
    if named:
        sys.exit(f"the container environment sets {', '.join(named)} (the CR's homeChannel); this case needs an install without one")
    from cron.jobs import load_jobs

    job = next((j for j in load_jobs() if j.get("id") == OOBE_JOB), None)
    if job is not None and job.get("deliver", LOCAL_DELIVERY) != LOCAL_DELIVERY:
        sys.exit(f"the {OOBE_JOB} job delivers to {job.get('deliver')!r}; the report this case plants would be posted there")
    state = {
        "cleared_at": datetime.now(timezone.utc).isoformat(),
        "config": read(CONFIG),
        "env": read(ENV_FILE),
        "aligned": read(ALIGNED),
        "completed": read(COMPLETED),
        "opened_at": None,
    }
    save_state(state)
    if state["config"] is not None:
        write(CONFIG, without_home_channel(state["config"]))
    if state["env"] is not None:
        write(ENV_FILE, without_home_vars(state["env"]))
    put_back(ALIGNED, None)
    put_back(COMPLETED, None)
    print(f"cleared the home channel and the delivery markers at {state['cleared_at']}")


def stage_audits():
    with open(STAGE_SOURCE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == STAGE_AUDITS for t in node.targets):
            return tuple(ast.literal_eval(node.value))
    sys.exit(f"no {STAGE_AUDITS} in {STAGE_SOURCE}")


def finished():
    with open(ARM_STATE, encoding="utf-8") as fh:
        since = datetime.fromisoformat(json.load(fh)["applied_at"]).timestamp()
    audits = stage_audits()
    try:
        with open(AUDITS_MARKER, encoding="utf-8") as fh:
            recorded = json.load(fh)
    except (OSError, ValueError):
        recorded = {}
    had_turn = set()
    for key in HAD_TURN_KEYS:
        value = recorded.get(key) if isinstance(recorded, dict) else None
        had_turn.update(value if isinstance(value, (list, dict)) else ())
    if not os.path.isfile(EXECUTIONS_DB):
        print("pending")
        return
    conn = sqlite3.connect(f"file:{EXECUTIONS_DB}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT)
    try:
        rows = conn.execute(
            f"SELECT job_id, status, claimed_at FROM executions WHERE job_id IN ({','.join('?' * len(audits))})",
            audits,
        ).fetchall()
    finally:
        conn.close()
    ended, going = set(), set()
    for job_id, status, claimed in rows:
        try:
            if datetime.fromisoformat(claimed).timestamp() < since or status == SKIPPED:
                continue
        except (TypeError, ValueError):
            continue
        (going if status in IN_FLIGHT else ended).add(job_id)
    # A run still going would end after the first message, with a summary the report cannot name,
    # even for an audit the stage recorded as having had its turn.
    print("finished" if not going and ended | had_turn >= set(audits) else "pending")


def open_chat():
    state = load_state()
    with open(ALIGNED, "x"):
        pass
    state["opened_at"] = datetime.now(timezone.utc).isoformat()
    save_state(state)
    print(f"opened the chat at {state['opened_at']}")


def restore():
    if not os.path.exists(STATE):
        print("the home channel was not cleared")
        return
    state = load_state()
    put_back(CONFIG, state.get("config"))
    put_back(ENV_FILE, state.get("env"))
    put_back(ALIGNED, state.get("aligned"))
    put_back(COMPLETED, state.get("completed"))
    os.remove(STATE)
    print("restored the home channel and the delivery markers")


if command == "clear":
    clear()
elif command == "finished":
    finished()
elif command == "open":
    open_chat()
elif command == "restore":
    restore()
else:
    sys.exit(f"unknown command {command!r}")
