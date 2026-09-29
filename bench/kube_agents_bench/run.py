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

"""Run a task's prompt against the agent: ``bench-run TASK DIR``.

Writes ``DIR/trajectory.json`` for ``bench-score`` to grade. Provisions
nothing and judges nothing; the agent is reached as the harness always does.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from kube_agents_bench import transcript
from kube_agents_bench.harness import KubeAgentsHarness
from kube_agents_bench.score import load_task


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench-run", description=__doc__.splitlines()[0])
    parser.add_argument("task", type=Path, help="path to bench/tasks/<id>/task.yaml")
    parser.add_argument("dir", type=Path, help="directory to write trajectory.json to")
    args = parser.parse_args(argv)
    prompt = load_task(args.task)["prompt"]
    result = KubeAgentsHarness().run(prompt)
    args.dir.mkdir(parents=True, exist_ok=True)
    transcript.dump(args.dir / "trajectory.json", prompt)
    for err in result.errors:
        print(err)
    return 1 if result.has_errors() else 0
