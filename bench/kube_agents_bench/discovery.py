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

"""Read the onboarding discovery sweep's fan-out off the agent's disk.

The sweep card is filed by the ``bootstrap-inventory-scan`` cron job, not by
the conversation a case drives, so nothing in the transcript names it. What
it did is on the agent's data volume: the card id in
``.bootstrap_scan_filed``, the cards its worker filed in the board's
``kanban_worker_children``, and the Cluster Agent roster under ``profiles/``.
This module reads all three in one ``kubectl exec``, the way
:mod:`kube_agents_bench.board` reads card statuses.

The roster is read from the profiles directly rather than through
``bootstrap_scan_gate``, so the read answers the same question on a build
whose gate does not list the roster at all.

A reply without the sentinel is a failed read, never an empty one: the
harness's ``_agent_shell`` returns ``""`` on any kubectl failure.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from typing import Any

from kube_agents_bench.board import BOARD_FILE
from kube_agents_bench.worker_trajectory import DATA_ROOT, FALLBACK_PYTHON, HERMES_PYTHON

__all__ = ["CLUSTER_KEY_PREFIX", "FANOUT_PRESENT", "command", "read_fanout"]

FANOUT_PRESENT = "__BOOTSTRAP_FANOUT__"

# Spelled as agents/chat/scripts/bootstrap_scan_gate.py spells them; a rename
# there without one here reads as a sweep that filed nothing.
SCAN_MARKER = ".bootstrap_scan_filed"
CLUSTER_KEY_PREFIX = "bootstrap-inventory-cluster-"

# agents/platform/scripts/cluster_agent_profile.py: RESERVED_PROFILES.
RESERVED_PROFILES = ("default", "platform")

# profile_scaffold.PROFILE_MARKER and cluster_agent_reconcile.SCAFFOLD_ARTIFACTS:
# a profile missing either is one the gate leaves out of Step 2, as
# platform_control's list_cluster_profiles does.
READY_FILES = ("profile.yaml", "USER.md")

# Runs inside the agent container. Positional arguments: data root, board
# file, sentinel, scan marker, key prefix, the comma-joined ready files, then
# the reserved profile names.
_IN_POD_SCRIPT = r"""
import json, os, sqlite3, sys

ROOT, BOARD, SENTINEL, MARKER, PREFIX, READY = sys.argv[1:7]
RESERVED = set(sys.argv[7:])
SQLITE_BUSY_TIMEOUT = 10
out = {"sweep": None, "roster": [], "unidentified": [], "not_ready": [], "children": [], "error": None}


def fail(message):
    out["error"] = message
    print(SENTINEL)
    print(json.dumps(out))
    sys.exit(0)


try:
    with open(os.path.join(ROOT, MARKER)) as fh:
        marker = dict(line.strip().split("=", 1) for line in fh if "=" in line)
except OSError as exc:
    fail("no discovery sweep has been filed: %s" % exc)
sweep_id = marker.get("task_id", "")
if not sweep_id:
    fail("%s names no task_id" % MARKER)

try:
    import yaml
except ImportError:
    fail("pyyaml is not importable, so the roster cannot be read")

profiles = os.path.join(ROOT, "profiles")
names = sorted(
    n for n in (os.listdir(profiles) if os.path.isdir(profiles) else [])
    if n not in RESERVED and os.path.isdir(os.path.join(profiles, n))
)
for name in names:
    if not all(os.path.isfile(os.path.join(profiles, name, f)) for f in READY.split(",")):
        out["not_ready"].append(name)
        continue
    try:
        with open(os.path.join(profiles, name, "config.yaml")) as fh:
            ident = (yaml.safe_load(fh) or {}).get("cluster_identity") or {}
    except (OSError, ValueError, yaml.YAMLError, AttributeError):
        ident = {}
    if not isinstance(ident, dict) or not all(ident.get(k) for k in ("project", "cluster", "location")):
        out["unidentified"].append(name)
        continue
    key = "%s%s-%s-%s" % (PREFIX, ident["project"], ident["cluster"], ident["location"])
    out["roster"].append({"profile": name, "key": key})

try:
    conn = sqlite3.connect("file:%s/%s?mode=ro" % (ROOT, BOARD), uri=True, timeout=SQLITE_BUSY_TIMEOUT)
    row = conn.execute("SELECT id, status FROM tasks WHERE id = ?", (sweep_id,)).fetchone()
    if row is None:
        fail("the sweep card %s named by %s is not on the board" % (sweep_id, MARKER))
    out["sweep"] = {"id": row[0], "status": row[1]}
    rows = conn.execute(
        "SELECT t.id, t.assignee, t.idempotency_key, t.status FROM kanban_worker_children w "
        "JOIN tasks t ON t.id = w.child_id WHERE w.creator_id = ? ORDER BY w.created_at, t.id",
        (sweep_id,),
    ).fetchall()
    for tid, assignee, key, status in rows:
        parents = [p for (p,) in conn.execute("SELECT parent_id FROM task_links WHERE child_id = ?", (tid,))]
        out["children"].append(
            {"id": tid, "assignee": assignee, "key": key, "status": status, "parents": parents}
        )
    conn.close()
except sqlite3.Error as exc:
    fail("kanban board: %s" % exc)

print(SENTINEL)
print(json.dumps(out))
"""


def command() -> str:
    """The ``sh -c`` line that reads the sweep, its children and the roster."""
    args = " ".join(
        shlex.quote(a)
        for a in [
            DATA_ROOT,
            BOARD_FILE,
            FANOUT_PRESENT,
            SCAN_MARKER,
            CLUSTER_KEY_PREFIX,
            ",".join(READY_FILES),
            *RESERVED_PROFILES,
        ]
    )
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_IN_POD_SCRIPT)} {args}'
    )


def read_fanout(shell: Callable[[str, float], str], timeout: float) -> tuple[dict[str, Any] | None, str]:
    """The sweep, its worker's children and the roster, or ``None`` and why not.

    ``shell`` is ``harness._agent_shell``, a parameter so the tests can run the
    script locally.
    """
    reply = shell(command(), timeout)
    marker = reply.find(FANOUT_PRESENT)
    if marker < 0:
        return None, "the agent pod could not be read (kubectl exec failed or the script did not run)"
    try:
        payload = json.loads(reply[marker + len(FANOUT_PRESENT) :].strip())
    except json.JSONDecodeError as exc:
        return None, f"the fan-out read did not return JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "the fan-out read returned something other than an object"
    if payload.get("error"):
        return None, str(payload["error"])
    return payload, ""
