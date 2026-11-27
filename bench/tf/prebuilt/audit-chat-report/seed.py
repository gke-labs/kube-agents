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

"""Copy a stream's sandbox-local report store into the broker store before the stack edits it.

Run in the shell sandbox as `python3 - <audit> <local store dir>`, and only with
the broker store (main.tf reads the switch from the pod spec first). With the
broker store on (FLEET_AUDIT_REPORT_STORE=broker, #6110), the scripts' first touch of a
stream copies the sandbox's local copy of it into the broker store, `latest.json`
included, unless the local stream directory carries the seed marker. The stack
hides `latest.json` in the broker's store (store.sh, in the agent container); a
seed after that would bring it back. So this reads each local stream once
through the broker, which seeds it, and then requires the marker in every local
stream directory before the stack goes on. A sandbox with no local store, or a
file store selected (the broker store off), has nothing to seed. Any other
FLEET_AUDIT_REPORT_STORE stops it.

A read the broker does not answer (an OSError) is retried a few times with a
short backoff and then fails the plant. It is never taken as seeded: the store's
own `exists()` answers True for a store it cannot read, so the read here is one
that raises instead (`ring()`).
"""

import os
import sys
import time
from pathlib import Path

SKILL_SCRIPTS = "/opt/data/skills/fleet-audit/scripts"
PLATFORM_SCRIPTS = "/opt/data/scripts"
# The seed read's tries and the pause before the second, doubled before each
# later one: 1s, 2s, 4s, so a broker that is restarting has about 7s to answer.
SEED_READ_ATTEMPTS = 4
SEED_READ_BACKOFF_SECONDS = 1.0
# The report store's switch, which the operator renders on the sandbox's shell
# container (report_status.py: REPORT_STORE_ENV).
REPORT_STORE_ENV = "FLEET_AUDIT_REPORT_STORE"
REPORT_STORE_FILE = "file"
REPORT_STORE_BROKER = "broker"


def report_store():
    """`file` or `broker`, from this container's FLEET_AUDIT_REPORT_STORE; exits on any other value."""
    value = os.environ.get(REPORT_STORE_ENV, "")
    if value in ("", REPORT_STORE_FILE):
        return REPORT_STORE_FILE
    if value == REPORT_STORE_BROKER:
        return REPORT_STORE_BROKER
    sys.exit(
        f"ERROR: {REPORT_STORE_ENV}={value!r} on the shell sandbox is neither "
        f"{REPORT_STORE_FILE!r} nor {REPORT_STORE_BROKER!r}, so which report store to use is unknown."
    )


class SeedReadFailed(Exception):
    """The broker did not answer the seed read on any try."""


def force_seed(audit_report, audit, repo, attempts=SEED_READ_ATTEMPTS, backoff=SEED_READ_BACKOFF_SECONDS, sleep=time.sleep):
    """Read the stream once through the broker, which seeds it; retry an OSError.

    A fresh store object per try, since one caches its reads. SeedReadFailed
    after the last try, naming the last error.
    """
    for attempt in range(1, attempts + 1):
        try:
            audit_report.report_store_dir(audit, repo).ring()
            return
        except OSError as exc:
            print(f"seed: the broker read of {repo} failed (try {attempt} of {attempts}): {exc}", file=sys.stderr)
            if attempt == attempts:
                raise SeedReadFailed(f"{repo}: {exc}") from exc
            sleep(backoff * 2 ** (attempt - 1))


def main() -> int:
    audit, store = sys.argv[1], Path(sys.argv[2])
    if report_store() != REPORT_STORE_BROKER or not store.is_dir():
        print(f"seed: nothing to seed for {audit} (broker store off, or no local store)")
        return 0
    sys.path[:0] = [SKILL_SCRIPTS, PLATFORM_SCRIPTS]
    import audit_report  # noqa: PLC0415 -- the path is the sandbox's, set just above
    import report_status  # noqa: PLC0415
    return seed_streams(audit_report, report_status, audit, store)


def holds_a_run(report_status, stream):
    """True when the local stream directory holds a latest.json or a run."""
    if (stream / report_status.LATEST_NAME).exists():
        return True
    runs = stream / report_status.RUNS_DIR
    return runs.is_dir() and any(runs.glob("*.json"))


def seed_streams(audit_report, report_status, audit, store, sleep=time.sleep):
    """Seed every local stream under `store`, then require each one's marker. An exit status."""
    unread, missing = [], []
    for owner in sorted(p for p in store.iterdir() if p.is_dir()):
        for stream in sorted(p for p in owner.iterdir() if p.is_dir()):
            repo = f"{owner.name}/{stream.name.replace('%2F', '/')}"
            if not holds_a_run(report_status, stream):
                # No latest.json and no run: nothing for a seed to copy, so no
                # marker either, and nothing for the stack to hide.
                continue
            try:
                force_seed(audit_report, audit, repo, sleep=sleep)
            except SeedReadFailed as exc:
                unread.append(str(exc))
                continue
            if not (stream / report_status.SEED_MARKER_NAME).exists():
                missing.append(repo)
    if unread:
        print(f"seed: the broker did not answer the seed read for {'; '.join(unread)}", file=sys.stderr)
    if missing:
        print(f"seed: no seed marker after a broker read for {', '.join(missing)}", file=sys.stderr)
    if unread or missing:
        return 1
    print(f"seed: {audit}'s local streams are in the broker store")
    return 0


if __name__ == "__main__":
    sys.exit(main())
