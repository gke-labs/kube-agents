#!/usr/bin/env python3
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
"""Render the recorded-weeks report templates from the retrospective collector.

The stack beside this file seeds the upgrade retrospective's report store on the
shell sandbox with a full report whose first-upgraded cluster carries a gate:
`risk` (one container's log volume rose tenfold past the floor) or `clear`. The
templates under `report/` are what the collector's own renderer produces for a
synthetic run over the seeded fleet, with placeholders for the leased project,
the four slot clusters and the apply time; `bench/tests/test_recorded_weeks_stack.py`
pins the checked-in templates to this script's output, so a renderer change
regenerates them here rather than drifting under the case.

Run from the repository root: `python3 bench/tf/prebuilt/upgrade-retro-recorded-weeks/render.py`.
"""

import copy
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]
SCRIPTS = REPO / "agents" / "platform" / "skills" / "fleet-audit" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import upgrade_retrospective as ur  # noqa: E402

TESTDATA = SCRIPTS / "testdata" / "upgrade_retrospective"
MODES = ("risk", "clear")
# The placeholders the stack substitutes at apply time (main.tf).
PROJECT = "{{PROJECT}}"
CLUSTERS = {slot: f"{{{{CLUSTER_{slot.upper()}}}}}" for slot in "abcd"}
LOCATIONS = {slot: f"{{{{LOCATION_{slot.upper()}}}}}" for slot in "abcd"}
# A parseable sentinel the stack replaces with the apply time (the JSON's
# `generated_at` and the title's date), so the route reads a fresh report.
GENERATED_AT = "2026-01-02T03:04:05Z"
TITLE_DATE = "2026-01-02"
SINCE = "2026-09-28T18:00:00Z"
NOW = datetime(2026, 10, 19, 18, 0, tzinfo=timezone.utc)
LAST_RUN = "2026-10-12T18:00:00Z"
FAMILY_REGULAR, FAMILY_HELD = "seeded-regular", "seeded-held"
CONTROL_PLANE_BEFORE, CONTROL_PLANE_AFTER = "1.33.4-gke.1134000", "1.33.5-gke.1080000"
HELD_CONTROL_PLANE = "1.32.9-gke.1185000"
POOL = "default-pool"
CHANNEL = "REGULAR"
WINDOW_TEXT = "daily 03:00Z for 4h"
NEXT_OPENS = "2026-10-20T03:00:00Z"
OPERATIONS = [
    {"name": "operation-1760066820000-aaaaaaaa", "type": "UPGRADE_MASTER", "target": ur.CONTROL_PLANE_TARGET, "start": "2026-10-10T03:27:00Z", "end": "2026-10-10T03:35:00Z", "duration_s": 480, "status": "DONE", "error": None},
    {"name": "operation-1760153160000-bbbbbbbb", "type": "UPGRADE_NODES", "target": POOL, "start": "2026-10-11T03:26:00Z", "end": "2026-10-11T03:31:00Z", "duration_s": 300, "status": "DONE", "error": None},
]
RISK_CONTAINER = ("kube-system", "konnectivity-agent")
RISK_SCALE = 10
PODS, BUDGETS = 14, 2


class RecordedSession:
    """The two recorded weeks, the after-week scaled for the risk mode."""

    def __init__(self, mode):
        self.mode, self.calls = mode, 0
        self.before = json.loads((TESTDATA / "log_volume_seeded_a_before.json").read_text())["body"]
        self.after = json.loads((TESTDATA / "log_volume_seeded_a_after.json").read_text())["body"]

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        body = copy.deepcopy(self.before if self.calls % 2 == 1 else self.after)
        if self.mode == "risk" and self.calls % 2 == 0:
            for series in body["timeSeries"]:
                labels = series["resource"]["labels"]
                if (labels.get("namespace_name"), labels.get("container_name")) == RISK_CONTAINER:
                    for point in series["points"]:
                        point["value"]["int64Value"] = str(int(point["value"]["int64Value"]) * RISK_SCALE)
        return type("Response", (), {"status_code": 200, "text": json.dumps(body)})()


def cluster_doc(slot, family, control_plane):
    return {"project": PROJECT, "location": LOCATIONS[slot], "name": CLUSTERS[slot], "resourceLabels": {ur.FAMILY_LABEL_KEY: family}, "currentMasterVersion": control_plane, "releaseChannel": {"channel": CHANNEL}, "nodePools": [{"name": POOL, "version": control_plane}]}


def next_upgrade(current):
    return {"channel": CHANNEL, "target": CONTROL_PLANE_AFTER, "current": current, "below_target": current != CONTROL_PLANE_AFTER, "window": WINDOW_TEXT, "next_opens": NEXT_OPENS, "exclusions": []}


def build(mode):
    key = {slot: ur.cluster_key(PROJECT, LOCATIONS[slot], CLUSTERS[slot]) for slot in "abcd"}
    clusters = [cluster_doc("a", FAMILY_REGULAR, CONTROL_PLANE_AFTER), cluster_doc("b", FAMILY_HELD, HELD_CONTROL_PLANE), cluster_doc("c", FAMILY_REGULAR, CONTROL_PLANE_BEFORE), cluster_doc("d", FAMILY_REGULAR, CONTROL_PLANE_BEFORE)]
    versions_after = {"control_plane": CONTROL_PLANE_AFTER, "node_pools": {POOL: CONTROL_PLANE_AFTER}}
    review = {
        "cluster": key["a"], "project": PROJECT, "location": LOCATIONS["a"], "name": CLUSTERS["a"],
        "what_happened": {"status": ur.STATUS_UPGRADED, "reasons": ["control plane moved", f"pool {POOL} moved"], "window_start": SINCE, "channel": CHANNEL, "versions_before": {"control_plane": CONTROL_PLANE_BEFORE, "node_pools": {POOL: CONTROL_PLANE_BEFORE}}, "versions_after": versions_after, "cluster_status": "RUNNING", "operations": copy.deepcopy(OPERATIONS), "symptom_window_start": OPERATIONS[0]["start"]},
        "what_failed": [], "mitigations": [], "shapes": [], "managed_agents": 0, "next_upgrade": next_upgrade(CONTROL_PLANE_AFTER),
        "baseline": {"versions": versions_after, "pods": PODS, "budgets": BUDGETS, "shapes": 0, "symptom_count": 0, "first_seen": 0, "first_run": False, "guards": [], "shape_reads_failed": []},
        "symptom_baseline": [], "guards": [], "read_errors": [], "reviewed": True, "partial": [], "answered": [name for name, _ in ur.KUBECTL_READS], "commands": [], "starting": [], "gated": [], "terminating": [], "skew_notes": [], "unavailable_workloads": [], "budgets_at_zero": [], "family": FAMILY_REGULAR,
    }
    review["gate"] = ur.cluster_gate(review, ur.operation_window(OPERATIONS), RecordedSession(mode), NOW)
    assert review["gate"]["verdict"] == mode, review["gate"]
    unchanged = []
    for slot, control_plane in (("b", HELD_CONTROL_PLANE), ("c", CONTROL_PLANE_BEFORE), ("d", CONTROL_PLANE_BEFORE)):
        unchanged.append({"cluster": key[slot], "control_plane": control_plane, "last_operation": None, "last_run": LAST_RUN, "next_upgrade": next_upgrade(control_plane), "commands": [], "checks_unevaluated": []})
    families = ur.family_verdicts(clusters, {key["a"]: review["gate"]})
    sections = ur.triage([review], unchanged, [], [], GENERATED_AT, families=families, gates_reread=[])
    result = {
        "generated_at": GENERATED_AT, "since": SINCE, "projects": [PROJECT], "reviews": [review], "unchanged": unchanged, "failed_reads": [], "operations_unread": [], "removed_clusters": [], "rechecks": [], "upgrading": [], "scoped": False, "after_upgrade": False, ur.AFTER_RESET_KEY: False, "reset_archive": None, "reset_at": None, "scope_reason": "", "fleet": [PROJECT], "outside_fleet": [], "sections": sections, "families": families, "gates_reread": [], "operation_guards": {"refreshed": [], "cleared": [], "held": [], "unlisted": []}, "unread_clusters": {}, "held_clusters": {}, "not_running": {}, "deferred": [], "deferred_rechecks": 0, "budget_spent": False, "scope_unread": [], "scope_unresolved": None, "scope_note": None, "guards": [], "dry_run": False,
    }
    report = ur.render_report(result)
    assert report.startswith(ur.REPORT_TITLE.format(date=TITLE_DATE)), report[:80]
    return report, json.dumps(result, indent=2, sort_keys=True) + "\n"


def render_all():
    out = {}
    for mode in MODES:
        report, twin = build(mode)
        out[f"report-{mode}.md.tmpl"] = report
        out[f"report-{mode}.json.tmpl"] = twin
    return out


def main():
    for name, text in render_all().items():
        (HERE / "report" / name).write_text(text)
        print(f"wrote report/{name} ({len(text)} chars)")


if __name__ == "__main__":
    main()
