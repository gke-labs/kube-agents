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

"""Grade a finished run from its output directory: ``bench-score TASK DIR``.

Reads ``DIR/trajectory.json``, runs the task's verification_spec and
writes ``DIR/verdict.json``. Verifiers that read the cluster or GitHub still
need that access from wherever this runs.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
from pathlib import Path
from typing import Any, Sequence

import yaml
from devops_bench.verification import VerifierAgent, parse_entries
from devops_bench.verification.rollup import rollup

import kube_agents_bench.verifiers  # noqa: F401 - registers worker_agents
from kube_agents_bench import transcript

TIMEOUT_SEC = 120.0


def load_task(task_path: Path) -> dict[str, Any]:
    """The task.yaml at ``task_path``, with the placeholders the env supplies filled in."""
    text = task_path.read_text()
    for var in ("PROJECT_ID", "CLUSTER_NAME"):
        text = text.replace("{{" + var + "}}", os.environ.get(var, ""))
    return yaml.safe_load(text)


def score(task_path: Path, out_dir: Path) -> dict[str, Any]:
    entries, errors = parse_entries(load_task(task_path).get("verification_spec"))

    transcript.load(out_dir / "trajectory.json")
    agent = VerifierAgent()
    report = []
    for entry in entries:
        try:
            r = agent.run_entry(entry, timeout_sec=TIMEOUT_SEC)
            outcome = {"success": r.success, "status": r.status, "reason": r.reason}
        except Exception as exc:  # noqa: BLE001 - one entry must not abort the rest
            outcome = {"success": False, "status": "error", "reason": f"evaluation error: {exc}"}
        report.append(
            {"name": entry.name, "role": entry.role, "severity": entry.severity, "weight": entry.weight}
            | outcome
        )
    scores = rollup(report, parse_error_count=len(errors))
    return {
        "verification_report": report,
        "verification_parse_errors": errors,
        "scores": dataclasses.asdict(scores),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench-score", description=__doc__.splitlines()[0])
    parser.add_argument("task", type=Path, help="path to bench/tasks/<id>/task.yaml")
    parser.add_argument("dir", type=Path, help="run directory holding trajectory.json")
    args = parser.parse_args(argv)
    verdict = score(args.task, args.dir)
    (args.dir / "verdict.json").write_text(json.dumps(verdict, indent=2))
    return 0 if all(e["success"] for e in verdict["verification_report"]) else 1
