#!/usr/bin/env python3
"""Run every fleet_resource_property check in the cluster-debugging cases
against a live cluster, through the real verifier.

This is the verification half of a bench case and nothing else: no agent runs
and nothing is deployed. It answers one question per check -- does this
safeguard resolve its fixture role, find its object, and pass against the
fixture as planted? A safeguard that cannot do that would have graded a real
run wrong, and the presubmit is an expensive place to discover it.

It drives `FleetResourcePropertyVerifier` out of the live registry rather than
re-implementing what it does, because a mirror of the verifier's logic
validates the mirror.

Usage:
    hack/fleet-kubeconfigs.sh                       # writes <role>.kubeconfig
    export BENCH_FLEET_KUBECONFIG_DIR=<that dir>
    cd bench
    uv run python tools/live_check_fleet_safeguards.py [task.yaml ...]

`uv run` rather than a bare `python3`, for the same reason every other bench
invocation in this repository uses it: `devops_bench` is a uv-managed
dependency pinned to a git SHA, so an interpreter that has not been resolved
against `bench/pyproject.toml` fails at the import on line 42 rather than at
anything to do with the fleet.

Exit status is 0 only when every check reported `pass`.

The seeded fleet lives in the Boskos pool projects, where `container.clusters.get`
is the Prow service account's and not a developer's, so `hack/fleet-kubeconfigs.sh`
will not produce those kubeconfigs on a laptop. To exercise the checks anyway,
apply the `bench/tf/fleet/` fixture objects to a cluster you do control, copy its
kubeconfig to `<role>.kubeconfig` for each role the cases name, and write the
`<role>.confirmed` sidecar the runner would have written -- the role's implicit
`namespace/<ns>` probe plus each probe in `bench/tf/fleet/fixtures.json`. Without
that sidecar every `op: absent` check reports `error` rather than `pass`, because
an absence the runner never confirmed is an unplanted fixture and not a clean run.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

import kube_agents_bench.verifiers  # noqa: F401  -- registers the custom types
from devops_bench.verification.base import VERIFIERS

REPO = Path(__file__).resolve().parents[2]
DEFAULT_CASES = [
    "cluster-agent-crashloop-misleading-symptom",
    "cluster-agent-crashloop-evidence-chain",
    "cluster-agent-healthy-workload-no-finding",
    "cluster-agent-pending-replicas-capped-pool",
]
TIMEOUT_SEC = 60.0


def fleet_checks(task_path: Path):
    """Yield (check_name, role, severity, check_body) for each fleet check."""
    spec = yaml.safe_load(task_path.read_text()).get("verification_spec") or []
    for entry in spec:
        check = entry.get("check") or {}
        if check.get("type") == "fleet_resource_property":
            yield entry["name"], entry.get("role"), entry.get("severity"), check


def main(argv: list[str]) -> int:
    if not os.environ.get("BENCH_FLEET_KUBECONFIG_DIR"):
        print(
            "BENCH_FLEET_KUBECONFIG_DIR is unset. Run hack/fleet-kubeconfigs.sh "
            "first and point this at the directory it wrote.",
            file=sys.stderr,
        )
        return 2

    paths = (
        [Path(a) for a in argv]
        if argv
        else [REPO / "bench" / "tasks" / c / "task.yaml" for c in DEFAULT_CASES]
    )

    tally = {"pass": 0, "fail": 0, "error": 0}
    for task_path in paths:
        print(f"\n=== {task_path.parent.name} ===")
        found = False
        for name, role, severity, check in fleet_checks(task_path):
            found = True
            body = dict(check)
            verifier = VERIFIERS.get(body["type"])(name=name, **body)
            result = verifier.verify(TIMEOUT_SEC)
            status = result.status
            tally[status] = tally.get(status, 0) + 1
            mark = {"pass": "PASS", "fail": "FAIL", "error": "ERR "}.get(status, status)
            print(f"  [{mark}] {name}  (role={role}, severity={severity})")
            if status != "pass" and result.reason:
                print(f"         {result.reason}")
        if not found:
            print("  (no fleet_resource_property checks)")

    print(f"\n{tally}")
    return 0 if tally.get("fail", 0) == 0 and tally.get("error", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
