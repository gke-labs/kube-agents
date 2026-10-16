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

"""Fire one scheduled fleet-audit run on the install and wait for its delivery.

Runs in the agent container, streamed in by main.tf. Three commands:

  fire <home> <python> <job>   mark <job> due with Hermes' own ``cron.jobs.trigger_job``
                               and record when, in STATE_FILE
  wait <home> <job> <seconds>  wait until the job's runtime record shows a run that ended
                               after the mark, and record its status and delivery error
  clean <home>                 remove STATE_FILE

The run is a real scheduled run: the profile's cron tick claims the mark and the
scheduler delivers the final message through the job's ``deliver`` leg, the
chat relay.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

STATE_NAME = ".bench-audit-chat-report.json"
PROFILE_HOME = ("profiles", "platform")
ROSTER = ("cron", "jobs.json")
TRIGGER_SCRIPT = "import sys\nfrom cron.jobs import trigger_job\nsys.exit(0 if trigger_job(sys.argv[1]) else 3)\n"
TRIGGER_TIMEOUT_S = 60
POLL_S = 20


def _state_path(home):
    return os.path.join(home, STATE_NAME)


def _job(home, job_id):
    with open(os.path.join(home, *PROFILE_HOME, *ROSTER), encoding="utf-8") as handle:
        store = json.load(handle)
    jobs = store.get("jobs") if isinstance(store, dict) else store
    for job in jobs or []:
        if isinstance(job, dict) and job.get("id") == job_id:
            return job
    raise SystemExit(f"{job_id} is not in the Platform Agent's cron roster")


def _write(home, state):
    with open(_state_path(home), "w", encoding="utf-8") as handle:
        json.dump(state, handle)


def fire(home, python, job_id):
    _job(home, job_id)
    fired_at = datetime.now(timezone.utc).isoformat()
    _write(home, {"job_id": job_id, "fired_at": fired_at})
    env = {**os.environ, "HERMES_HOME": os.path.join(home, *PROFILE_HOME)}
    done = subprocess.run(
        [python, "-c", TRIGGER_SCRIPT, job_id],
        env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TRIGGER_TIMEOUT_S,
    )
    if done.returncode != 0:
        raise SystemExit(f"trigger_job({job_id}) failed: {(done.stderr or done.stdout).strip()}")
    print(f"marked {job_id} due at {fired_at}")


def wait(home, job_id, seconds):
    with open(_state_path(home), encoding="utf-8") as handle:
        state = json.load(handle)
    fired = datetime.fromisoformat(state["fired_at"])
    deadline = time.monotonic() + float(seconds)
    while True:
        job = _job(home, job_id)
        ended = job.get("last_run_at")
        if ended and datetime.fromisoformat(ended) > fired:
            state.update(
                last_run_at=ended,
                last_status=job.get("last_status"),
                last_delivery_error=job.get("last_delivery_error"),
            )
            _write(home, state)
            print(f"{job_id} ended at {ended}: {job.get('last_status')}")
            return
        if time.monotonic() > deadline:
            raise SystemExit(f"{job_id} did not end a run within {seconds}s of {state['fired_at']}")
        time.sleep(POLL_S)


def clean(home):
    try:
        os.unlink(_state_path(home))
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    command, home = sys.argv[1], sys.argv[2]
    if command == "fire":
        fire(home, sys.argv[3], sys.argv[4])
    elif command == "wait":
        wait(home, sys.argv[3], sys.argv[4])
    elif command == "clean":
        clean(home)
    else:
        raise SystemExit(f"unknown command {command!r}")
