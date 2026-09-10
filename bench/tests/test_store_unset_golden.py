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

"""The gate's output with ``EVAL_BASELINE_STORE`` unset, pinned byte for byte.

``fixtures/golden/store-unset/`` was captured from ``main`` BEFORE the
record-governs-admission change, by running this same file with
``BENCH_UPDATE_GOLDEN=1`` against the shipped, empty ``bench/baselines/``
directory -- which is exactly what the presubmit reads today. The test
replays the same three cases and two suites and compares every artifact the
shell would keep: the per-case JSON hand-off, the suite markdown, the suite
JSON, and what was printed. So a change to admission, the aggregate or the
verdict markdown can only alter the store-unset presubmit by also altering
these files in the same diff, where a reviewer sees it.

Two things are normalised, and nothing else: the absolute fixture path inside
``run_dir`` (the checkout moves; the record does not), and the additive
``admission_source`` key, which the store-unset presubmit never displays and
which did not exist when the golden was captured.

Regenerate with ``BENCH_UPDATE_GOLDEN=1 uv run pytest tests/test_store_unset_golden.py``
and read the diff: it IS the behaviour change you are about to ship.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from conftest import FIXTURE_RUNS, GREEN_RUNS, RED_RUNS
from kube_agents_bench.gate import main

GOLDEN = Path(__file__).parent / "fixtures" / "golden" / "store-unset"
#: The shipped store, empty of evidence: what an unset EVAL_BASELINE_STORE reads.
SHIPPED_BASELINES = Path(__file__).resolve().parents[1] / "baselines"
JUDGE = "gemini-3.1-pro-preview"
PATH_TOKEN = "<FIXTURE_RUNS>"
#: Additive since the golden was captured; popped before comparing.
ADDITIVE_KEYS = ("admission_source",)
UPDATE_ENV = "BENCH_UPDATE_GOLDEN"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (
        "BOOTSTRAP_ADMITTED",
        "JUDGE_MODEL",
        "DETERMINISTIC_CORRECTNESS_FLOOR",
        "EVAL_AGGREGATE_MARGIN",
        "EVAL_AGGREGATE_MIN_SCORED",
        "EVAL_AGGREGATE_ARMED",
        "EVAL_ADMISSION_RATE",
        "EVAL_ADMISSION_MIN_RUNS",
        "EVAL_JUDGED_MARGIN",
        "EVAL_JUDGED_METRICS",
        "PULL_NUMBER",
        "RC_COMMIT_SHA",
        "EVAL_BASELINE_STORE",
        "EVAL_BASELINE_MAX_OBJECTS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JUDGE_MODEL", JUDGE)


def _normalise(text: str) -> str:
    return text.replace(str(FIXTURE_RUNS), PATH_TOKEN)


def _strip_additive(doc: dict) -> dict:
    for key in ADDITIVE_KEYS:
        doc.pop(key, None)
    for case in doc.get("cases") or []:
        if isinstance(case, dict):
            for key in ADDITIVE_KEYS:
                case.pop(key, None)
    return doc


def _json_text(path: Path) -> str:
    doc = _strip_additive(json.loads(path.read_text(encoding="utf-8")))
    # The same serialisation `bench-gate` writes, so a byte diff is a real diff.
    return _normalise(json.dumps(doc, indent=2) + "\n")


def _captured(rc: int, captured) -> str:
    text = f"exit {rc}\n{_normalise(captured.out)}"
    if captured.err:
        text += f"--- stderr ---\n{_normalise(captured.err)}"
    return text


def replay(kanban_task: Path, tmp_path: Path, monkeypatch, capsys) -> dict[str, str]:
    """Three cases and two suites, the way the shell drives them."""
    artifacts: dict[str, str] = {}
    reds = [FIXTURE_RUNS / n for n in RED_RUNS]
    greens = [FIXTURE_RUNS / n for n in GREEN_RUNS + GREEN_RUNS[:1]]

    def case(name: str, runs: list[Path], bootstrap: str | None) -> Path:
        if bootstrap is None:
            monkeypatch.delenv("BOOTSTRAP_ADMITTED", raising=False)
        else:
            monkeypatch.setenv("BOOTSTRAP_ADMITTED", bootstrap)
        out = tmp_path / f"case-{name}.json"
        argv = ["case", "--task", str(kanban_task), "--baseline-dir", str(SHIPPED_BASELINES)]
        for run in runs:
            argv += ["--result", str(run)]
        rc = main([*argv, "--json-out", str(out)])
        artifacts[f"case-{name}.stdout"] = _captured(rc, capsys.readouterr())
        artifacts[f"case-{name}.json"] = _json_text(out)
        return out

    def suite(name: str, *cases: Path, bootstrap: str | None) -> None:
        if bootstrap is None:
            monkeypatch.delenv("BOOTSTRAP_ADMITTED", raising=False)
        else:
            monkeypatch.setenv("BOOTSTRAP_ADMITTED", bootstrap)
        md, js = tmp_path / f"{name}.md", tmp_path / f"{name}.json"
        argv = ["suite", "--baseline-dir", str(SHIPPED_BASELINES)]
        for path in cases:
            argv += ["--case-result", str(path)]
        rc = main([*argv, "--markdown-out", str(md), "--json-out", str(js)])
        artifacts[f"{name}.stdout"] = _captured(rc, capsys.readouterr())
        # `.md.txt`, not `.md`: a captured verdict is a test input, and the
        # documentation map (`make docs-check`) inventories every `*.md`.
        artifacts[f"{name}.md.txt"] = _normalise(md.read_text(encoding="utf-8"))
        artifacts[f"{name}.json"] = _json_text(js)

    collapse = case("collapse", reds, "agent-kanban-smoke")
    unadmitted = case("unadmitted", reds, None)
    green = case("green", greens, "agent-kanban-smoke")
    suite("suite-red", collapse, green, bootstrap="agent-kanban-smoke")
    suite("suite-green", unadmitted, green, bootstrap=None)
    return artifacts


def test_the_store_unset_gate_output_matches_the_golden(
    kanban_task, tmp_path, monkeypatch, capsys
):
    got = replay(kanban_task, tmp_path, monkeypatch, capsys)

    if os.environ.get(UPDATE_ENV):
        GOLDEN.mkdir(parents=True, exist_ok=True)
        for name, text in got.items():
            (GOLDEN / name).write_text(text, encoding="utf-8")

    assert set(got) == {p.name for p in GOLDEN.iterdir()}, (
        f"artifact set changed; regenerate with {UPDATE_ENV}=1 and review the diff"
    )
    for name, text in got.items():
        assert text == (GOLDEN / name).read_text(encoding="utf-8"), (
            f"{name} differs from the pre-change golden; regenerate with "
            f"{UPDATE_ENV}=1 and review the diff"
        )
