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

"""A pull request is graded by the merge target's grader.

``hack/ci-eval-pr.sh`` extracts ``bench/`` at ``PULL_BASE_SHA`` into a scratch
directory, puts the change's ``bench/tasks/`` in place of main's, and points
``BENCH_DIR`` there, so the change's cases run under main's harness, verifiers
and ``bench-gate``. A case main's grader cannot read stops the job red before
the fan-out. These tests run the script's own text: the extraction over a
throwaway repository, the read check through the real devops-bench and this
package (so they live here, where both are installed), and the step that ties
them together.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_EVAL_PR = REPO_ROOT / "hack" / "ci-eval-pr.sh"
BENCH = REPO_ROOT / "bench"
SMOKE = "agent-kanban-smoke"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
# Stands in for `uv run python`: the interpreter running these tests has
# devops-bench and this package installed, which is what the bench venv holds.
UV_SHIM = 'uv() { shift 2; "${GRADER_PY}" "$@"; }'
MESSAGE = "this case needs a grader change that is not on main yet; land the grader change first"


def _lift(pattern: str, flags: int = re.MULTILINE | re.DOTALL) -> str:
    match = re.search(pattern, CI_EVAL_PR.read_text(encoding="utf-8"), flags)
    assert match, f"{pattern!r} not found in hack/ci-eval-pr.sh"
    return match.group(0)


def _constants() -> str:
    return _lift(r'^readonly EVAL_GRADER_TREE=.*?\nreadonly EVAL_GRADER_CASES_DIR=[^\n]*')


def _function(name: str) -> str:
    return _lift(rf"^{name}\(\) \{{.*?^\}}")


def _bash(body: str, env: dict | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + body],
        capture_output=True,
        text=True,
        check=False,
        cwd=cwd,
        env={**os.environ, "GRADER_PY": sys.executable, **(env or {})},
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True, env={**os.environ, **GIT_ENV},
    ).stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def change(tmp_path):
    """A repository whose HEAD edits the grader and the cases on top of a base.

    Base: grader.py says "main", cases `kept` and `gone`. Change: grader.py
    says "change", `kept` edited, `gone` deleted, `added` new.
    """
    repo = tmp_path / "repo"
    _write(repo / "hack" / ".keep", "")
    _write(repo / "bench" / "kube_agents_bench" / "grader.py", "main\n")
    _write(repo / "bench" / "tasks" / "kept" / "task.yaml", "id: kept  # main\n")
    _write(repo / "bench" / "tasks" / "gone" / "task.yaml", "id: gone\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo / "bench" / "kube_agents_bench" / "grader.py", "change\n")
    _write(repo / "bench" / "tasks" / "kept" / "task.yaml", "id: kept  # change\n")
    shutil.rmtree(repo / "bench" / "tasks" / "gone")
    _write(repo / "bench" / "tasks" / "added" / "task.yaml", "id: added\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "change")
    return repo, base


# --------------------------------------------------------------------------
# pin_grader: main's bench/, the change's bench/tasks/
# --------------------------------------------------------------------------


def test_the_grader_comes_from_the_base_and_the_cases_from_the_change(change, tmp_path):
    repo, base = change
    out = tmp_path / "out"
    out.mkdir()
    proc = _bash("\n".join([_constants(), _function("pin_grader"), f'pin_grader "{repo}" "{base}" "{out}"']))
    assert proc.returncode == 0, proc.stderr
    bench = out / "bench"
    # The change's grader edit is ignored.
    assert (bench / "kube_agents_bench" / "grader.py").read_text(encoding="utf-8") == "main\n"
    # Its cases are taken whole: edited, added, and deleted.
    assert (bench / "tasks" / "kept" / "task.yaml").read_text(encoding="utf-8") == "id: kept  # change\n"
    assert (bench / "tasks" / "added" / "task.yaml").is_file()
    assert not (bench / "tasks" / "gone").exists()


def test_a_sha_that_is_not_a_commit_fails_the_extraction(change, tmp_path):
    repo, _ = change
    out = tmp_path / "out"
    out.mkdir()
    proc = _bash("\n".join([_constants(), _function("pin_grader"), f'pin_grader "{repo}" "{"0" * 40}" "{out}"']))
    assert proc.returncode != 0


# --------------------------------------------------------------------------
# grader_unreadable_cases: what main's grader cannot read
# --------------------------------------------------------------------------


def _unreadable(bench_dir: Path, *tasks: str) -> subprocess.CompletedProcess:
    body = "\n".join([UV_SHIM, f'BENCH_DIR="{bench_dir}"', _function("grader_unreadable_cases"), 'grader_unreadable_cases "$@"'])
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + body, "bash", *tasks],
        capture_output=True, text=True, check=False,
        env={**os.environ, "GRADER_PY": sys.executable},
    )


def test_every_case_in_the_tree_reads_under_its_own_grader():
    """No false red: the check reads every case this tree carries, presubmit and
    nightly, under this tree's grader, with nothing to say."""
    tasks = sorted(f"./{p.relative_to(BENCH).as_posix()}" for p in (BENCH / "tasks").glob("*/task.yaml"))
    assert tasks
    proc = _unreadable(BENCH, *tasks)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", proc.stdout


@pytest.fixture
def mutated(tmp_path):
    """A bench directory holding a copy of a real presubmit case to edit, with
    this tree's tf/ so a stack the grader has resolves."""
    bench = tmp_path / "bench"
    (bench / "tasks").mkdir(parents=True)
    (bench / "tf").symlink_to(BENCH / "tf")
    shutil.copytree(BENCH / "tasks" / SMOKE, bench / "tasks" / SMOKE)
    path = bench / "tasks" / SMOKE / "task.yaml"

    def edit(fn):
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        fn(spec)
        path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
        return _unreadable(bench, f"./tasks/{SMOKE}/task.yaml")

    return edit


def test_the_unedited_copy_reads(mutated):
    proc = mutated(lambda spec: None)
    assert (proc.returncode, proc.stdout) == (0, ""), proc.stderr


def test_a_check_type_the_grader_does_not_know_is_reported(mutated):
    def add(spec):
        spec["verification_spec"].append(
            {
                "name": "needs-a-new-verifier",
                "role": "objective",
                "mode": "assert",
                "check": {"type": "check_type_not_on_main", "phrase": "x"},
            }
        )

    proc = mutated(add)
    assert proc.returncode == 0, proc.stderr
    assert f"./tasks/{SMOKE}/task.yaml: verification entry needs-a-new-verifier:" in proc.stdout
    assert "check_type_not_on_main" in proc.stdout


def test_a_field_the_grader_does_not_know_is_reported(mutated):
    def add(spec):
        first = spec["verification_spec"][0]
        check = dict(first["check"], field_not_on_main=True)
        spec["verification_spec"].append(dict(first, name="needs-a-new-field", check=check))

    proc = mutated(add)
    assert proc.returncode == 0, proc.stderr
    assert "verification entry needs-a-new-field:" in proc.stdout
    assert "field_not_on_main" in proc.stdout


def test_a_stack_the_grader_does_not_have_is_reported(mutated):
    def stack(spec):
        spec["infrastructure"] = {"deployer": "tofu", "stack": "prebuilt/stack-not-on-main"}

    proc = mutated(stack)
    assert proc.returncode == 0, proc.stderr
    assert "its stack tf/prebuilt/stack-not-on-main is not in this grader" in proc.stdout


def test_a_case_bench_gate_refuses_is_reported(mutated):
    proc = mutated(lambda spec: spec.update(id="not-the-directory-name"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(f"./tasks/{SMOKE}/task.yaml: ")


# --------------------------------------------------------------------------
# The step: which runs pin, and what a red says
# --------------------------------------------------------------------------


def _step(repo: Path, env: dict, unreadable: str = "") -> subprocess.CompletedProcess:
    block = _lift(r"^# ─── Grade with the merge target's grader.*?^export EVAL_GRADER_SHA$")
    head, sep, tail = block.partition('\nEVAL_GRADER_SHA="$(git')
    assert sep, "the step's main line moved"
    stub = f"grader_unreadable_cases() {{ printf '%s' {unreadable!r}; }}"
    body = "\n".join(
        [
            _constants(),
            f'SCRIPT_DIR="{repo / "hack"}"',
            f'BENCH_DIR="{repo / "hack"}/../bench"',
            "TASKS=(./tasks/kept/task.yaml ./tasks/added/task.yaml)",
            head,
            stub,
            sep.lstrip("\n") + tail,
            'echo "BENCH_DIR=${BENCH_DIR}"',
            'echo "EVAL_GRADER_SHA=${EVAL_GRADER_SHA}"',
        ]
    )
    scrubbed = {k: v for k, v in os.environ.items() if k not in ("PULL_NUMBER", "JOB_TYPE", "PULL_BASE_SHA")}
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + body],
        capture_output=True, text=True, check=False, env={**scrubbed, **env},
    )


def _out(proc: subprocess.CompletedProcess, key: str) -> str:
    return re.search(rf"^{key}=(.*)$", proc.stdout, re.MULTILINE).group(1)


@pytest.mark.parametrize("env", [{"JOB_TYPE": "presubmit"}, {"JOB_TYPE": "batch"}, {"PULL_NUMBER": "7"}])
def test_a_change_is_graded_by_the_base_bench(change, env):
    repo, base = change
    proc = _step(repo, {**env, "PULL_BASE_SHA": base})
    assert proc.returncode == 0, proc.stderr
    bench = Path(_out(proc, "BENCH_DIR"))
    assert bench != repo / "bench"
    assert (bench / "kube_agents_bench" / "grader.py").read_text(encoding="utf-8") == "main\n"
    assert (bench / "tasks" / "added" / "task.yaml").is_file()
    assert _out(proc, "EVAL_GRADER_SHA") == base
    assert f"Grader: bench/ at PULL_BASE_SHA={base}" in proc.stdout


@pytest.mark.parametrize("env", [{}, {"JOB_TYPE": "periodic"}, {"JOB_TYPE": "postsubmit"}])
def test_the_nightly_and_postsubmit_grade_from_head(change, env):
    repo, _ = change
    proc = _step(repo, {**env, "PULL_BASE_SHA": "ignored"})
    assert proc.returncode == 0, proc.stderr
    assert _out(proc, "BENCH_DIR") == f"{repo / 'hack'}/../bench"
    assert _out(proc, "EVAL_GRADER_SHA") == _git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("base_sha", ["", "0" * 40])
def test_a_change_with_no_base_commit_stops_rather_than_grading_itself(change, base_sha):
    repo, _ = change
    proc = _step(repo, {"JOB_TYPE": "presubmit", "PULL_BASE_SHA": base_sha})
    assert proc.returncode == 1
    assert "main's grader cannot be extracted" in proc.stderr


def test_a_case_main_cannot_read_reds_the_job(change):
    repo, base = change
    reason = "./tasks/added/task.yaml: verification entry x: unknown verifier type 'y'"
    proc = _step(repo, {"JOB_TYPE": "presubmit", "PULL_BASE_SHA": base}, unreadable=reason)
    assert proc.returncode == 1
    assert MESSAGE in proc.stderr
    assert f"  {reason}" in proc.stderr
