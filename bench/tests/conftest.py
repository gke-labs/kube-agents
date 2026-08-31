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

"""Shared fixtures for the scoring tests.

EVERY failure mode is a MUTATION OF A REAL RECORD, never a hand-written dict.
The five directories under ``fixtures/runs/`` are captured devops-bench output
(see their README for provenance and the one redacted field), and the helpers
here copy one and change
exactly the field under test. That discipline is not stylistic: the first draft
of the ladder gated rung 3 on ``metadata.session_id``, a key a devops-bench
record does not have, and hand-written fixtures would have agreed with it
happily. A mutation starts from the truth and can only drift one field at a
time.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

FIXTURE_RUNS = Path(__file__).parent / "fixtures" / "runs"
TASKS = Path(__file__).resolve().parents[1] / "tasks"

#: The three captured repetitions of the pre-#893 `agent-kanban-smoke`, all
#: three of which fail `report-states-the-probe-title` at correctness 0.5.
RED_RUNS = ("kanban_red_1", "kanban_red_2", "kanban_red_3")

#: Two captured runs of the local prompt variant that passes honestly at 1.0.
GREEN_RUNS = ("kanban_green_1", "kanban_green_2")


def read_fixture(name: str) -> dict[str, Any]:
    """The three JSON files of one captured run, as a mutable dict."""
    run = FIXTURE_RUNS / name
    out: dict[str, Any] = {}
    for stem in ("results", "rows", "manifest"):
        out[stem] = json.loads((run / f"{stem}.json").read_text(encoding="utf-8"))
    return out


def write_run(directory: Path, payload: dict[str, Any]) -> Path:
    """Materialise a run directory from a :func:`read_fixture` payload."""
    directory.mkdir(parents=True, exist_ok=True)
    for stem, doc in payload.items():
        (directory / f"{stem}.json").write_text(
            json.dumps(doc, indent=2), encoding="utf-8"
        )
    return directory


@pytest.fixture
def make_run(tmp_path: Path):
    """Build a run directory from a real capture, optionally mutated.

    ``mutate`` receives the FIRST record in ``results.json`` -- the only one
    devops-bench writes per invocation -- and edits it in place. Returns the
    directory, which is what the scorer's API takes.
    """
    counter = {"n": 0}

    def _make(source: str = "kanban_green_1", mutate=None, *, name: str | None = None) -> Path:
        payload = copy.deepcopy(read_fixture(source))
        if mutate is not None:
            mutate(payload["results"][0])
        counter["n"] += 1
        target = tmp_path / (name or f"run_{counter['n']:03d}")
        return write_run(target, payload)

    return _make


@pytest.fixture
def kanban_task() -> Path:
    """The real `agent-kanban-smoke` task file: noop deployer, declares checks."""
    return TASKS / "agent-kanban-smoke" / "task.yaml"


@pytest.fixture
def write_task(tmp_path: Path):
    """Write a synthetic ``<case-id>/task.yaml`` and return its path.

    Used only where a real task file cannot express the case under test --
    an `expected_fail` marker (no task declares one yet) or an id/directory
    disagreement (which the repository correctly does not contain).
    """
    import yaml as _yaml

    def _write(case_id: str, doc: dict[str, Any] | str) -> Path:
        directory = tmp_path / "tasks" / case_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "task.yaml"
        text = doc if isinstance(doc, str) else _yaml.safe_dump(doc, sort_keys=False)
        path.write_text(text, encoding="utf-8")
        return path

    return _write
