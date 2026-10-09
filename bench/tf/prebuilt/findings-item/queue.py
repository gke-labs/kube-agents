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

"""Plant and remove the findings-queue rows of bench/tasks/findings-decision-covers-item.

main.tf runs this in the agent container as `python3 - <mode> <base url> <rows, base64>`:
the Session KV server answers there on loopback, and the container's environment
carries the SESSION_KV_API_KEY it requires. It calls only routes every build of the
server has (POST /v1/findings, PATCH /v1/findings/{id}, POST /v1/findings/{id}/verified
and GET /v1/findings), so it plants the same rows on a build without the item-wide
decision as on one with it.

plant: registers the rows (findings.json), then sets each to `surfaced`. The route
sets `surfaced` from any state and it is not a decision, so this also returns a row
an earlier run left dismissed, accepted or snoozed to undecided without touching its
siblings. Exits 1 unless the read-back finds every row surfaced.

teardown: records every open row of the rows' projects as resolved, which takes it
off the open list. A dismissed row stays dismissed, which is closed too. The projects
are invented names only this case uses, so every row under them is this case's.
Exits 1 if the read-back still finds an open row.
"""

import base64
import json
import os
import sys
import urllib.parse
import urllib.request

# platform_mcp_server.py's FINDINGS_TIMEOUT_SECONDS.
TIMEOUT_SECONDS = 20.0
# The cap list_findings applies to `limit` (findings_queue.py).
ROW_LIMIT = 1000
# findings_queue.py's STATES that are still on the open list.
OPEN_STATES = ("queued", "surfaced", "accepted", "snoozed")
PLANTED_STATE = "surfaced"
TEARDOWN_OUTCOME = "resolved"
TEARDOWN_NOTE = "bench teardown: a row bench/tf/prebuilt/findings-item planted"


def call(base, method, path, body=None):
    token = (os.environ.get("SESSION_KV_API_KEY") or "").strip()
    if not token:
        sys.exit("SESSION_KV_API_KEY is not set in this container, so the findings queue would refuse every call")
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": "Bearer " + token}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def rows_in(base, project):
    query = urllib.parse.urlencode({"project": project, "limit": ROW_LIMIT})
    return call(base, "GET", "/v1/findings?" + query)["findings"]


def plant(base, rows):
    registered = call(base, "POST", "/v1/findings", {"findings": rows})["results"]
    ids = [result["id"] for result in registered]
    for finding_id in ids:
        call(base, "PATCH", "/v1/findings/" + urllib.parse.quote(finding_id, safe=""), {"state": PLANTED_STATE})
    states = {}
    for project in sorted({row["project"] for row in rows}):
        states.update({row["id"]: row["state"] for row in rows_in(base, project)})
    wrong = {finding_id: states.get(finding_id) for finding_id in ids if states.get(finding_id) != PLANTED_STATE}
    if wrong:
        sys.exit("planted rows are not %s after the plant: %s" % (PLANTED_STATE, json.dumps(wrong, sort_keys=True)))
    for finding_id in ids:
        print("planted %s (%s)" % (finding_id, PLANTED_STATE))


def teardown(base, rows):
    projects = sorted({row["project"] for row in rows})
    for project in projects:
        for row in rows_in(base, project):
            if row["state"] in OPEN_STATES:
                call(
                    base,
                    "POST",
                    "/v1/findings/" + urllib.parse.quote(row["id"], safe="") + "/verified",
                    {"outcome": TEARDOWN_OUTCOME, "observed": TEARDOWN_NOTE},
                )
                print("resolved %s (was %s)" % (row["id"], row["state"]))
    still_open = [row["id"] for project in projects for row in rows_in(base, project) if row["state"] in OPEN_STATES]
    if still_open:
        sys.exit("rows still open after the teardown: %s" % ", ".join(still_open))


def main(argv):
    mode, base, rows_b64 = argv
    rows = json.loads(base64.b64decode(rows_b64).decode("utf-8"))
    if mode == "plant":
        plant(base, rows)
    elif mode == "teardown":
        teardown(base, rows)
    else:
        sys.exit("mode is %r; must be plant or teardown" % mode)


if __name__ == "__main__":
    main(sys.argv[1:])
