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

"""Read the findings queue of the install under test.

The queue is served by the Session KV server inside the agent pod, on loopback
only, and answers a caller that presents the pod's ``SESSION_KV_API_KEY``, which
the agent container carries in its environment. So the read is one
``kubectl exec`` into that container that GETs ``/v1/findings`` there, the route
the agent's own ``get_findings`` tool calls. It goes through the route rather
than the SQLite file because the route's shape is what every build of the server
shares; the table gains columns between builds.

A reply without the sentinel is a failed read, never an empty one: the shell
returns ``""`` on any kubectl failure.
"""

from __future__ import annotations

import json
import shlex
from collections.abc import Callable
from typing import Any

from kube_agents_bench.worker_trajectory import FALLBACK_PYTHON, HERMES_PYTHON

__all__ = ["FINDINGS_READ", "SESSION_KV_URL", "read_project"]

# platform_mcp_server.py: _findings_request's base URL.
SESSION_KV_URL = "http://127.0.0.1:8699"
FINDINGS_READ = "__FINDINGS_QUEUE_READ__"
# findings_queue.py: list_findings caps `limit` here.
ROW_LIMIT = 1000
# platform_mcp_server.py: FINDINGS_TIMEOUT_SECONDS.
REQUEST_TIMEOUT_SEC = 20.0
# The fields of a row a verdict reads; the rest stays in the pod.
ROW_FIELDS = ("id", "state", "check_slug", "project", "cluster", "namespace", "object")

# Runs in the agent container. Prints the sentinel and one JSON line: the rows
# under the project, or the error that stopped the read.
_READ_SCRIPT = r"""
import json, os, sys, urllib.error, urllib.parse, urllib.request
base, project, limit, timeout, fields, sentinel = sys.argv[1:7]
out = {"findings": None, "error": None}
token = (os.environ.get("SESSION_KV_API_KEY") or "").strip()
if not token:
    out["error"] = "SESSION_KV_API_KEY is not set in the agent container"
else:
    query = urllib.parse.urlencode({"project": project, "limit": limit})
    request = urllib.request.Request(base + "/v1/findings?" + query, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(request, timeout=float(timeout)) as response:
            rows = json.loads(response.read().decode("utf-8")).get("findings")
        if not isinstance(rows, list):
            out["error"] = "GET /v1/findings returned no list of findings"
        else:
            out["findings"] = [{k: row.get(k) for k in fields.split(",")} for row in rows if isinstance(row, dict)]
    except (urllib.error.URLError, OSError, ValueError) as exc:
        out["error"] = "GET /v1/findings: %s" % exc
print(sentinel)
print(json.dumps(out))
"""


def read_command(project: str) -> str:
    """The ``sh -c`` line that reads the queue's rows under ``project`` in the agent container."""
    args = " ".join(
        shlex.quote(a)
        for a in [SESSION_KV_URL, project, str(ROW_LIMIT), str(REQUEST_TIMEOUT_SEC), ",".join(ROW_FIELDS), FINDINGS_READ]
    )
    return (
        f'PY={shlex.quote(HERMES_PYTHON)}; [ -x "$PY" ] || PY={shlex.quote(FALLBACK_PYTHON)}; '
        f'"$PY" -c {shlex.quote(_READ_SCRIPT)} {args}'
    )


def read_project(shell: Callable[[str, float], str], project: str, timeout: float) -> dict[str, Any] | None:
    """The queue's rows under ``project``, or ``None`` if the pod could not be read.

    Returns ``{"findings": [...], "error": None}``, or ``{"findings": None,
    "error": "..."}`` when the pod answered but the queue did not. ``shell`` is
    :func:`kube_agents_bench.onboarding.agent_shell`, a parameter so the tests
    can run the command locally.
    """
    lines = shell(read_command(project), timeout).splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == FINDINGS_READ), None)
    if start is None:
        return None
    try:
        parsed = json.loads("\n".join(lines[start + 1 :]))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("error"):
        return {"findings": None, "error": str(parsed["error"])}
    if not isinstance(parsed.get("findings"), list):
        return None
    return {"findings": parsed["findings"], "error": None}
