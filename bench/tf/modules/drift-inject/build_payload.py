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

"""Compose the record the drift detector would have built, from argv.

The shape is `payloadForEvent` in `k8s-operator/cmd/drift-detector/inject.go`.
Every value arrives as an argument and is placed by `json.dumps`, so no caller
value is ever interpolated into JSON text -- which is what the three drift
stacks did, through an unquoted heredoc that had to expand `$project` and
`$insert_id` and therefore could not be quoted.

A tracked file rather than an inline `python3 -c`, so that
`bench/tests/test_drift_inject_payload.py` can pin what each stack builds. All
three stacks are nightly-only, so a silently wrong payload would otherwise
surface a night later as a judge disagreeing with a correct agent, rather than
on the pull request that wrote it.

usage: build_payload.py <out> <summary> <cluster> <project> <location>
                        <principal> <user_agent> <verb> <method_name>
                        <timestamp> <insert_id>
                        <res_group> <res_version> <res_namespace>
                        <res_resource> <res_name>
                        <join> <reconciled>
                        ["<manager>|<operation>|<path,path>"]...
"""

import json
import sys

KIND = "gitops-drift"
SCALARS = (
    "summary",
    "cluster",
    "project",
    "location",
    "principal",
    "user_agent",
    "verb",
    "method_name",
    "timestamp",
    "insert_id",
)
RESOURCE = ("group", "version", "namespace", "resource", "name")
TRAILING = ("join", "reconciled")
OWNER_PARTS = 3
BOOLEANS = {"true": True, "false": False}


def build(argv):
    """Return the payload dict for argv[1:], or raise SystemExit with the reason."""
    fixed = len(SCALARS) + len(RESOURCE) + len(TRAILING)
    if len(argv) < fixed + 1:
        raise SystemExit(
            f"build_payload: expected at least {fixed} arguments after the output "
            f"path, got {len(argv) - 1}. A miscount here would otherwise produce a "
            f"payload the daemon accepts with fields shifted by one, and the run "
            f"would fail minutes later as a card that never appeared."
        )
    values = argv[1:]
    doc = {"kind": KIND}
    doc.update(dict(zip(SCALARS, values)))
    values = values[len(SCALARS) :]
    doc["resource"] = dict(zip(RESOURCE, values))
    values = values[len(RESOURCE) :]
    doc["join"] = values[0]

    reconciled = values[1].strip().lower()
    if reconciled not in BOOLEANS:
        raise SystemExit(
            f"build_payload: reconciled must be 'true' or 'false', got {values[1]!r}. "
            f"Anything else would have silently become false on a field the card renders."
        )
    doc["reconciled"] = BOOLEANS[reconciled]

    # Absent, not empty, when no owner is passed. `payloadForEvent` builds
    # `owners` by appending over event.Owners, which a delete leaves nil, and
    # the field is `json:"owners,omitempty"` -- so the detector emits no key at
    # all. `gitops-drift-deleted-policy` depends on that: sending `[]` would be
    # the shape that does not occur.
    owners = []
    for spec in values[2:]:
        # Split from the RIGHT. Splitting from the left absorbs a `|` in the
        # manager into the operation: the field managers are unvalidated `var`
        # strings, so `argo|cd` would parse as three parts and go out as
        # manager `argo`, operation `cd`, and the real operation glued to the
        # front of the first path. Paths may not contain `|`; managers may.
        parts = spec.rsplit("|", OWNER_PARTS - 1)
        if len(parts) != OWNER_PARTS:
            raise SystemExit(
                f"build_payload: owner spec {spec!r} is not "
                f"'<manager>|<operation>|<path,path>'."
            )

        # Build the owner FIRST and validate that, rather than validating the
        # raw parts and emitting something derived from them afterwards. The
        # two are not the same object: a paths part of "," is a non-empty
        # string that yields no paths, and " Apply " is a non-empty string
        # that is not the operation anyone meant. Both used to pass a check on
        # the parts and ship from a different value -- a record asserting that
        # a manager the plant named owns no recorded paths, which
        # `_drift_ownership_block` renders verbatim and which reaches a judge
        # rather than an apply error.
        owner = {
            "manager": parts[0].strip(),
            "operation": parts[1].strip(),
            "paths": [path.strip() for path in parts[2].split(",") if path.strip()],
        }
        missing = [key for key, value in owner.items() if not value]
        if missing:
            raise SystemExit(
                f"build_payload: owner spec {spec!r} yields no "
                f"{', '.join(missing)}; every owner needs a manager, an "
                f"operation and at least one path."
            )
        owners.append(owner)
    if owners:
        doc["owners"] = owners
    return doc


def main(argv):
    out = argv[0]
    doc = build(argv)
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


if __name__ == "__main__":
    main(sys.argv[1:])
