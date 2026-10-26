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

"""Which cases in the matrix need a seeded-fleet fixture the lease-time scan saw drifted.

``hack/ci-eval-pr.sh`` runs ``hack/fleet-fixture-state.py --wait 0 --report``
against the leased project right after it publishes the fleet kubeconfigs.
This module reads that report beside every task in the matrix, once, before
the fan-out, and writes one file per case whose fixture is out of its
designed state: ``<out-dir>/<case>``, holding the reason the runner records
for each of the case's repetitions instead of running them
(``record_unit_not_run``). The reason leads with ``fixture-drift:<role>``
and carries the scan's own words for what it observed, so the gate, the
dashboard and the nightly issue filer all see the same label.

A case needs the roles its checks READ: every singular ``fixture_role:``,
which a ``fleet_resource_property`` check resolves to the role's cluster and
reads the fixture's own object through. A case with no such check needs its
``fixtures:`` list when that names exactly one role: the domain probes grade
the agent's report of that one planted defect, and
``upgrades-lagging-master-probe`` names ``version-laggard`` nowhere else.
Plural ``fixture_roles:`` entries only ask whether a role's slot was reached,
and a multi-role ``fixtures:`` list with no singular check is the readiness
shape (the planted role, then one role per slot so the report can be held to
every cluster): neither says the grade depends on that role's state, so
neither excuses the case, and it is graded as usual.

The change under test cannot widen any of this. On a pull request or a Tide batch
``hack/ci-eval-pr.sh`` passes the files it changes (``--changed-files``): a
change to what decides drift (the catalog, the scan, the runner that
addresses the clusters, this module) excuses nothing, and a case whose own
directory the change edits is never excused.

Only positive, sustained evidence counts. A role is excused only when the
lease-time scan READ it out of shape after waiting for it to converge AND the
published hourly scan (``fixture-state.json``, ``--published``) holds it
drifted on the same project in its last two scans, the rule ``health.py``'s
``fixture_drift`` condition uses: a pod rescheduling, or the crashloop's
restart lag after a node repair, reads drifted once and is not excused. A
published document that cannot be read excuses nothing. ``drifted`` is the
only lease-time verdict that can excuse; ``unchecked`` (the read failed), ``unpublished`` (the
runner wrote no kubeconfig for it) and ``no_state`` say nothing about the
fixture, and the case is graded as usual. A report that is missing, will
not parse, or is of another schema version is the same: every case is graded
as usual and the run says why. A task file that will not read leaves that
case graded as usual too. The only exit other than 0 is argparse's.

Run:  python -m kube_agents_bench.fixture_drift --report R --published P --out-dir D [--changed-files F] TASK...
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

#: The label every reason this module writes leads with, followed by the
#: role. ``scoring.py`` and the dashboard (``scripts/eval_dashboard/classify.py``,
#: ``health.py``) match the same literal; their tests read it from here.
FIXTURE_DRIFT_LABEL = "fixture-drift"

#: ``hack/fleet-fixture-state.py``'s ``REPORT_SCHEMA_VERSION`` and its one
#: verdict that is evidence of drift.
REPORT_SCHEMA_VERSION = 1
VERDICT_DRIFTED = "drifted"

#: The task.yaml keys a case names its fixtures under.
FIXTURES_KEY = "fixtures"
SPEC_KEY = "verification_spec"
CHECK_KEY = "check"
CHILD_CHECKS_KEY = "checks"
ROLE_KEY = "fixture_role"
ROLES_KEY = "fixture_roles"

#: Said in place of the scan's detail when a drifted role carries none.
NO_DETAIL = "no detail recorded"
#: How much of a role's first drift line the reason carries. The reason is
#: printed on every repetition's grading line, inside a block
#: ``hack/ci-eval-pr.sh`` keeps under PIPE_BUF; the whole detail is in the
#: report, which the job keeps as an artifact.
DETAIL_MAX_CHARS = 200
# How old the published hourly scan may be and still count: the same three
# missed scans health.py's FIXTURE_STATE_MAX_AGE refuses past (a test pins the
# two together). An older document means the scan job is down, and its last
# verdicts say nothing about the fixtures now.
PUBLISHED_MAX_AGE = timedelta(hours=3)
ELLIPSIS = "..."

#: Repository paths that decide what the scan calls drift. A pull request
#: that changes one is graded as usual throughout: it could otherwise make a
#: role read as drifted and excuse the cases on it.
DRIFT_DECIDING_PATHS = frozenset(
    {
        "bench/tf/fleet/fixtures.json",
        "hack/fleet-fixture-state.py",
        "hack/fleet-kubeconfigs.sh",
        "bench/kube_agents_bench/fixture_drift.py",
    }
)
#: Where a case's own files live, relative to the repository root.
CASES_PREFIX = "bench/tasks/"


class ScanUnusable(Exception):
    """The report cannot be read as a lease-time scan: grade every case as usual."""


@dataclass(frozen=True)
class LeaseScan:
    """The roles the lease-time scan READ and found out of shape."""

    project: str
    drifted: dict[str, list[str]]


def label(role: str) -> str:
    return f"{FIXTURE_DRIFT_LABEL}:{role}"


def load_scan(path: Path) -> LeaseScan:
    """The drifted roles in one ``--report`` document; ``ScanUnusable`` when
    the document is not one this module can trust."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ScanUnusable(f"no lease-time scan report at {path}") from exc
    except (OSError, ValueError) as exc:
        raise ScanUnusable(f"the lease-time scan report {path} does not read as JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ScanUnusable(f"the lease-time scan report {path} is not a JSON object")
    if doc.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ScanUnusable(
            f"the lease-time scan report {path} has schema_version {doc.get('schema_version')!r}, "
            f"not {REPORT_SCHEMA_VERSION}"
        )
    roles = doc.get("roles")
    if not isinstance(roles, dict):
        raise ScanUnusable(f"the lease-time scan report {path} carries no roles map")
    drifted: dict[str, list[str]] = {}
    for role, entry in roles.items():
        if isinstance(entry, dict) and entry.get("state") == VERDICT_DRIFTED:
            detail = entry.get("detail")
            drifted[str(role)] = [str(line) for line in detail] if isinstance(detail, list) else []
    return LeaseScan(project=str(doc.get("project") or "the leased project"), drifted=drifted)


def sustained_roles(path: Path, project: str, now: datetime | None = None) -> set[str]:
    """The roles the published hourly scan holds drifted on ``project`` in
    both its latest scan and the one before (``previous.drifted``). A document
    older than PUBLISHED_MAX_AGE is unusable, as health.py treats it."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        scanned_at = datetime.fromisoformat(doc["scanned_at"].replace("Z", "+00:00"))
        roles = doc["projects"][project]["roles"]
        before = set(doc["previous"]["drifted"].get(project) or [])
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ScanUnusable(f"the published hourly scan {path} does not say whether a drift on {project} is sustained ({exc!r})") from exc
    if scanned_at.tzinfo is None or (now or datetime.now(timezone.utc)) - scanned_at > PUBLISHED_MAX_AGE:
        raise ScanUnusable(f"the published hourly scan {path} is from {doc['scanned_at']}, older than {PUBLISHED_MAX_AGE}: the scan job is not publishing")
    return {r for r, v in roles.items() if isinstance(v, dict) and v.get("state") == VERDICT_DRIFTED and r in before}


def _check_roles(node: Any, found: set[str]) -> None:
    """Every singular ``fixture_role:`` in one check subtree."""
    if not isinstance(node, dict):
        return
    if isinstance(node.get(ROLE_KEY), str):
        found.add(node[ROLE_KEY])
    for child in node.get(CHILD_CHECKS_KEY) or []:
        _check_roles(child, found)


def _first_line(detail: list[str]) -> str:
    if not detail:
        return NO_DETAIL
    line = detail[0] if len(detail[0]) <= DETAIL_MAX_CHARS else detail[0][: DETAIL_MAX_CHARS - len(ELLIPSIS)] + ELLIPSIS
    return line + (f" (+{len(detail) - 1} more in the scan report)" if len(detail) > 1 else "")


def case_roles(task_yaml: Path) -> set[str]:
    """The fixture roles one case's grade depends on (module docstring).
    Raises ``OSError`` or ``yaml.YAMLError`` on a file that will not read."""
    import yaml  # here, so sustained_roles imports with the stdlib alone (hack/ci-eval-pr.sh)

    doc = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        return set()
    read: set[str] = set()
    for entry in doc.get(SPEC_KEY) or []:
        if isinstance(entry, dict):
            _check_roles(entry.get(CHECK_KEY), read)
    if read:
        return read
    fixtures = {r for r in doc.get(FIXTURES_KEY) or [] if isinstance(r, str)}
    return fixtures if len(fixtures) == 1 else set()


def drift_reason(roles: set[str], scan: LeaseScan) -> str | None:
    """The reason a case's repetitions are recorded under, or None when no
    role it needs drifted. One clause per drifted role, label first."""
    hits = sorted(roles & set(scan.drifted))
    if not hits:
        return None
    clauses = [f"{label(role)}: {_first_line(scan.drifted[role])}" for role in hits]
    return (
        " | ".join(clauses)
        + f" (seeded-fleet scan of {scan.project} at lease time; the case was not run against a fixture"
        " out of its designed state)"
    )


def mark(tasks: list[Path], scan: LeaseScan, out_dir: Path, edited_cases: frozenset[str] = frozenset()) -> dict[str, str]:
    """Write ``<out_dir>/<case>`` for each drifted case; return ``{case: reason}``."""
    import yaml  # lazy: see case_roles

    out_dir.mkdir(parents=True, exist_ok=True)
    marked: dict[str, str] = {}
    for task in tasks:
        name = task.parent.name
        try:
            roles = case_roles(task)
        except (OSError, yaml.YAMLError) as exc:
            print(f"WARNING: fixture drift: {task} does not read ({exc}); {name} is graded as usual", file=sys.stderr)
            continue
        reason = drift_reason(roles, scan)
        if reason and name in edited_cases:
            print(f"Fixture drift: {name} needs a drifted fixture, but this change edits the case, so it is graded as usual.", file=sys.stderr)
        elif reason:
            (out_dir / name).write_text(reason + "\n", encoding="utf-8")
            marked[name] = reason
    return marked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", required=True, help="hack/fleet-fixture-state.py --report output for the leased project")
    parser.add_argument("--out-dir", required=True, help="where to write one reason file per drifted case")
    parser.add_argument("--published", required=True, help="the hourly scan's published fixture-state.json")
    parser.add_argument("--changed-files", help="the paths the change under test edits, one per line, from the repository root")
    parser.add_argument("tasks", nargs="*", help="the matrix's task.yaml paths")
    args = parser.parse_args(argv)
    changed: set[str] = set()
    if args.changed_files:
        try:
            changed = {line.strip() for line in Path(args.changed_files).read_text(encoding="utf-8").splitlines() if line.strip()}
        except OSError as exc:
            print(f"Fixture drift: the change's file list {args.changed_files} does not read ({exc}); every case is graded as usual.", file=sys.stderr)
            return 0
    deciding = sorted(changed & DRIFT_DECIDING_PATHS)
    if deciding:
        print(
            f"Fixture drift: this change edits {', '.join(deciding)}, which decide what the scan calls drift; "
            "every case is graded as usual.",
            file=sys.stderr,
        )
        return 0
    edited_cases = frozenset(p[len(CASES_PREFIX):].split("/", 1)[0] for p in changed if p.startswith(CASES_PREFIX))
    try:
        scan = load_scan(Path(args.report))
    except ScanUnusable as exc:
        print(f"Fixture drift: {exc}; every case is graded as usual.", file=sys.stderr)
        return 0
    if not scan.drifted:
        print(f"Fixture drift: the lease-time scan of {scan.project} saw no drifted role; every case is graded as usual.", file=sys.stderr)
        return 0
    try:
        sustained = sustained_roles(Path(args.published), scan.project)
    except ScanUnusable as exc:
        print(f"Fixture drift: {exc}; every case is graded as usual.", file=sys.stderr)
        return 0
    passing = sorted(set(scan.drifted) - sustained)
    if passing:
        print(f"Fixture drift: {', '.join(passing)} read drifted at lease time but not on the last two hourly scans of {scan.project}; not excused.", file=sys.stderr)
    scan = LeaseScan(project=scan.project, drifted={r: d for r, d in scan.drifted.items() if r in sustained})
    marked = mark([Path(t) for t in args.tasks], scan, Path(args.out_dir), edited_cases)
    print(
        f"Fixture drift: the lease-time scan of {scan.project} saw {', '.join(sorted(scan.drifted))} drifted; "
        f"{len(marked)} case(s) in the matrix need one and are recorded as infrastructure without running.",
        file=sys.stderr,
    )
    for name, reason in sorted(marked.items()):
        print(f"  {name}: {reason}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
