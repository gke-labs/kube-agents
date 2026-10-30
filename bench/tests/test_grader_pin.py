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
    return _lift(r'^readonly EVAL_GRADER_TREE=.*?\nreadonly EVAL_FLEET_CATALOG=[^\n]*')


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
    _write(repo / "bench" / "tf" / "fleet" / "fixtures.json", '{"catalog": "main"}\n')
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _write(repo / "bench" / "kube_agents_bench" / "grader.py", "change\n")
    _write(repo / "bench" / "tasks" / "kept" / "task.yaml", "id: kept  # change\n")
    shutil.rmtree(repo / "bench" / "tasks" / "gone")
    _write(repo / "bench" / "tasks" / "added" / "task.yaml", "id: added\n")
    _write(repo / "bench" / "tf" / "fleet" / "fixtures.json", '{"catalog": "change"}\n')
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


def _unreadable(bench_dir: Path, *tasks: str, catalog: Path | None = None) -> subprocess.CompletedProcess:
    body = "\n".join([UV_SHIM, f'BENCH_DIR="{bench_dir}"', _function("grader_unreadable_cases"), 'grader_unreadable_cases "$@"'])
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + body, "bash", *tasks],
        capture_output=True, text=True, check=False,
        env={
            **{k: v for k, v in os.environ.items() if k != "FLEET_CATALOG"},
            "GRADER_PY": sys.executable,
            **({"FLEET_CATALOG": str(catalog)} if catalog else {}),
        },
    )


def test_every_case_in_the_tree_reads_under_its_own_grader():
    """No false red: the check reads every case this tree carries, presubmit and
    nightly, under this tree's grader and fleet catalog, with nothing to say.
    A case parked in the validator's FIXTURE_NOT_READY is left out: it names a
    role the catalog does not have yet, is registered nowhere, and no job runs
    it."""
    from test_fleet_verifier import _parked_cases

    parked = _parked_cases()
    tasks = sorted(
        f"./{p.relative_to(BENCH).as_posix()}"
        for p in (BENCH / "tasks").glob("*/task.yaml")
        if p.parent.name not in parked
    )
    assert tasks
    proc = _unreadable(BENCH, *tasks, catalog=BENCH / "tf" / "fleet" / "fixtures.json")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "", proc.stdout


@pytest.fixture
def mutated(tmp_path):
    """A bench directory holding a copy of a real presubmit case to edit, with
    this tree's tf/ so a stack the grader has resolves."""
    bench = tmp_path / "bench"
    (bench / "tasks").mkdir(parents=True)
    # A tf/ of real directories, so a stack resolves inside it and the check
    # decides on whether the directory is there.
    (bench / "tf" / "prebuilt" / "present").mkdir(parents=True)
    shutil.copytree(BENCH / "tasks" / SMOKE, bench / "tasks" / SMOKE)
    path = bench / "tasks" / SMOKE / "task.yaml"
    # main's fleet catalog, as the pin exports it: one role.
    catalog = tmp_path / "fixtures.json"
    catalog.write_text('{"roles": {"role-on-main": {}}}', encoding="utf-8")

    def edit(fn):
        spec = yaml.safe_load(path.read_text(encoding="utf-8"))
        fn(spec)
        path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
        return _unreadable(bench, f"./tasks/{SMOKE}/task.yaml", catalog=catalog)

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


def test_a_top_level_key_the_grader_does_not_know_is_reported(mutated):
    proc = mutated(lambda spec: spec.update(case_key_not_on_main={"x": 1}))
    assert proc.returncode == 0, proc.stderr
    assert f"./tasks/{SMOKE}/task.yaml: top-level key case_key_not_on_main is not in this grader" in proc.stdout


def test_a_stack_the_grader_has_reads(mutated):
    proc = mutated(lambda spec: spec.update(infrastructure={"deployer": "tofu", "stack": "prebuilt/present"}))
    assert (proc.returncode, proc.stdout) == (0, ""), proc.stderr


def test_a_stack_the_grader_does_not_have_is_reported(mutated):
    def stack(spec):
        spec["infrastructure"] = {"deployer": "tofu", "stack": "prebuilt/stack-not-on-main"}

    proc = mutated(stack)
    assert proc.returncode == 0, proc.stderr
    assert "its stack prebuilt/stack-not-on-main is not a directory under this grader" in proc.stdout


@pytest.mark.parametrize("escape", ["dotdot", "absolute"])
def test_a_stack_outside_the_graders_tf_is_reported(mutated, tmp_path, escape):
    """A stack shipped in the case folder of the change, reached by `..` or by
    an absolute path, would run the change's own Terraform under the pin."""
    outside = tmp_path / "bench" / "tasks" / SMOKE / "stack"
    outside.mkdir(parents=True)
    stack = f"../tasks/{SMOKE}/stack" if escape == "dotdot" else str(outside)

    def point(spec):
        spec["infrastructure"] = {"deployer": "tofu", "stack": stack}

    proc = mutated(point)
    assert proc.returncode == 0, proc.stderr
    assert f"its stack {stack} is not a directory under this grader" in proc.stdout


@pytest.mark.parametrize("key,value", [("fixture_role", "role-not-on-main"), ("fixture_roles", ["role-on-main", "role-not-on-main"])])
def test_a_fleet_role_the_grader_catalog_lacks_is_reported(mutated, key, value):
    def add(spec):
        check = {"type": "fleet_resource_property", key: value, "kind": "Pod", "property": "x", "equals": "y"}
        spec["verification_spec"].append({"name": "needs-a-new-role", "role": "objective", "mode": "assert", "check": check})

    proc = mutated(add)
    assert proc.returncode == 0, proc.stderr
    assert f"./tasks/{SMOKE}/task.yaml: fixture role role-not-on-main is not in the fleet catalog of this grader" in proc.stdout
    assert "role role-on-main " not in proc.stdout


def test_a_case_bench_gate_refuses_is_reported(mutated):
    proc = mutated(lambda spec: spec.update(id="not-the-directory-name"))
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith(f"./tasks/{SMOKE}/task.yaml: ")


# --------------------------------------------------------------------------
# The extraction at the fleet step: which runs pin, and the fleet catalog
# --------------------------------------------------------------------------

SCRUBBED = ("PULL_NUMBER", "JOB_TYPE", "PULL_BASE_SHA", "FLEET_CATALOG", "BENCH_DIR", "GRADER_BENCH_DIR")


def _run(body: str, env: dict) -> subprocess.CompletedProcess:
    scrubbed = {k: v for k, v in os.environ.items() if k not in SCRUBBED}
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + body],
        capture_output=True, text=True, check=False, env={**scrubbed, **env},
    )


def _extract(repo: Path, env: dict) -> subprocess.CompletedProcess:
    block = _lift(r"^# ─── Extract the merge target's grader.*?^GRADER_BENCH_DIR=\"\"\nif grader_pin_applies; then.*?^fi$")
    body = "\n".join(
        [_constants(), f'SCRIPT_DIR="{repo / "hack"}"', block,
         'echo "GRADER_BENCH_DIR=${GRADER_BENCH_DIR}"', 'echo "FLEET_CATALOG=${FLEET_CATALOG:-}"']
    )
    return _run(body, env)


def _out(proc: subprocess.CompletedProcess, key: str) -> str:
    return re.search(rf"^{key}=(.*)$", proc.stdout, re.MULTILINE).group(1)


@pytest.mark.parametrize("env", [{"JOB_TYPE": "presubmit"}, {"JOB_TYPE": "batch"}, {"PULL_NUMBER": "7"}])
def test_a_change_is_graded_by_the_base_bench(change, env):
    repo, base = change
    proc = _extract(repo, {**env, "PULL_BASE_SHA": base})
    assert proc.returncode == 0, proc.stderr
    bench = Path(_out(proc, "GRADER_BENCH_DIR"))
    assert (bench / "kube_agents_bench" / "grader.py").read_text(encoding="utf-8") == "main\n"
    assert (bench / "tasks" / "added" / "task.yaml").is_file()
    # The fleet catalog the fleet step and the lease scan read is main's.
    assert Path(_out(proc, "FLEET_CATALOG")).read_text(encoding="utf-8") == '{"catalog": "main"}\n'


def test_a_catalog_from_the_environment_does_not_survive_the_pin(change):
    repo, base = change
    proc = _extract(repo, {"JOB_TYPE": "presubmit", "PULL_BASE_SHA": base, "FLEET_CATALOG": "/elsewhere.json"})
    assert proc.returncode == 0, proc.stderr
    assert _out(proc, "FLEET_CATALOG") == f"{_out(proc, 'GRADER_BENCH_DIR')}/tf/fleet/fixtures.json"


@pytest.mark.parametrize("env", [{}, {"JOB_TYPE": "periodic"}, {"JOB_TYPE": "postsubmit"}])
def test_the_nightly_and_postsubmit_extract_nothing(change, env):
    repo, _ = change
    proc = _extract(repo, {**env, "PULL_BASE_SHA": "ignored"})
    assert proc.returncode == 0, proc.stderr
    assert _out(proc, "GRADER_BENCH_DIR") == ""
    assert _out(proc, "FLEET_CATALOG") == ""


@pytest.mark.parametrize("base_sha", ["", "0" * 40])
def test_a_change_with_no_base_commit_stops_rather_than_grading_itself(change, base_sha):
    repo, _ = change
    proc = _extract(repo, {"JOB_TYPE": "presubmit", "PULL_BASE_SHA": base_sha})
    assert proc.returncode == 1
    assert "main's grader cannot be extracted" in proc.stderr


def test_the_lease_scan_imports_fixture_drift_from_the_grader():
    assert 'PYTHONPATH="${GRADER_BENCH_DIR:-${SCRIPT_DIR}/../bench}"' in CI_EVAL_PR.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# Section 6: BENCH_DIR, the venv sync, the read check, the grader SHA
# --------------------------------------------------------------------------


def _step(repo: Path, grader_bench: str, *, uv: str = "uv() { :; }",
          env: dict | None = None) -> subprocess.CompletedProcess:
    block = _lift(r"^# ─── Grade with the merge target's grader.*?^export EVAL_GRADER_SHA$")
    head, sep, tail = block.partition('\nEVAL_GRADER_SHA="$(git')
    assert sep, "the step's main line moved"
    body = "\n".join(
        [
            _constants(),
            f'SCRIPT_DIR="{repo / "hack"}"',
            f'BENCH_DIR="{repo / "hack"}/../bench"',
            f'GRADER_BENCH_DIR="{grader_bench}"',
            "PULL_BASE_SHA=base0",
            "TASKS=(./tasks/kept/task.yaml ./tasks/added/task.yaml)",
            uv,
            head,
            sep.lstrip("\n") + tail,
            'echo "BENCH_DIR=${BENCH_DIR}"',
            'echo "EVAL_GRADER_SHA=${EVAL_GRADER_SHA}"',
        ]
    )
    return _run(body, env or {})


def test_a_pinned_run_grades_from_the_grader_and_names_its_sha(change, tmp_path):
    repo, _ = change
    proc = _step(repo, str(tmp_path))
    assert proc.returncode == 0, proc.stderr
    assert _out(proc, "BENCH_DIR") == str(tmp_path)
    assert _out(proc, "EVAL_GRADER_SHA") == "base0"
    assert "Grader: bench/ at PULL_BASE_SHA=base0" in proc.stdout


def test_an_unpinned_run_names_head(change):
    repo, _ = change
    proc = _step(repo, "")
    assert proc.returncode == 0, proc.stderr
    assert _out(proc, "BENCH_DIR") == f"{repo / 'hack'}/../bench"
    assert _out(proc, "EVAL_GRADER_SHA") == _git(repo, "rev-parse", "HEAD")


def _check(grader_bench: str, lane_dir: Path, unreadable: str = "") -> subprocess.CompletedProcess:
    """The after-lane read check, with grader_unreadable_cases recording what
    it was handed."""
    block = _lift(r"^# ─── main's grader reads the cases as run.*?^fi$")
    body = "\n".join(
        [
            _function("unit_task_path"),
            f'GRADER_BENCH_DIR="{grader_bench}"',
            f'INJECT_LANE_TASKS_DIR="{lane_dir}"',
            "PULL_BASE_SHA=base0",
            'EVAL_GRADER_TREE=bench',
            "TASKS=(./tasks/kept/task.yaml ./tasks/added/task.yaml)",
            f"grader_unreadable_cases() {{ printf 'HANDED %s\\n' \"$@\" >&2; printf '%s' {unreadable!r}; }}",
            block,
            "echo passed",
        ]
    )
    return _run(body, {})


def test_the_read_check_reads_the_lane_copies_devops_bench_is_handed(tmp_path):
    lane = tmp_path / "lane"
    _write(lane / "kept" / "task.yaml", "id: kept\n")
    proc = _check(str(tmp_path), lane)
    assert proc.returncode == 0, proc.stderr
    # The lane's copy where it made one, the file under tasks/ otherwise.
    assert f"HANDED {lane}/kept/task.yaml" in proc.stderr
    assert "HANDED ./tasks/added/task.yaml" in proc.stderr
    assert "HANDED ./tasks/kept/task.yaml" not in proc.stderr


def test_a_case_main_cannot_read_reds_the_job(tmp_path):
    reason = "./tasks/added/task.yaml: verification entry x: unknown verifier type 'y'"
    proc = _check(str(tmp_path), tmp_path / "lane", unreadable=reason)
    assert proc.returncode == 1
    assert MESSAGE in proc.stderr
    assert f"  {reason}" in proc.stderr


def test_an_unpinned_run_checks_nothing(tmp_path):
    proc = _check("", tmp_path / "lane", unreadable="x: y")
    assert proc.returncode == 0, proc.stderr
    assert "HANDED" not in proc.stderr


def test_one_failed_venv_sync_is_retried(change, tmp_path):
    repo, _ = change
    count = tmp_path / "syncs"
    uv = f'uv() {{ echo x >> "{count}"; [ "$(wc -l < "{count}")" -ge 2 ]; }}'
    proc = _step(repo, str(tmp_path), uv=uv)
    assert proc.returncode == 0, proc.stderr
    assert count.read_text().count("x") == 2


def test_two_failed_venv_syncs_stop_the_job(change, tmp_path):
    repo, _ = change
    proc = _step(repo, str(tmp_path), uv="uv() { return 1; }")
    assert proc.returncode == 1
    assert "could not sync main's grader's venv" in proc.stderr


# --------------------------------------------------------------------------
# Run directories: collected from where devops-bench wrote them
# --------------------------------------------------------------------------


def _collect(script_dir: Path, artifacts: Path, bench_dir: str | None) -> subprocess.CompletedProcess:
    src = (REPO_ROOT / "hack" / "ci-env.sh").read_text(encoding="utf-8")
    fn = re.search(r"^collect_bench_results\(\) \{.*?^\}", src, re.M | re.S).group(0)
    script_dir.mkdir(parents=True, exist_ok=True)
    lines = [f'SCRIPT_DIR="{script_dir}"', f'ARTIFACTS="{artifacts}"']
    if bench_dir is not None:
        lines.append(f'BENCH_DIR="{bench_dir}"')
    return _run("\n".join([*lines, fn, "collect_bench_results"]), {})


def test_run_directories_are_collected_from_the_grader_bench_dir(tmp_path):
    grader = tmp_path / "grader" / "bench"
    _write(grader / "results" / "run_1" / "results.json", "{}")
    _write(tmp_path / "repo" / "bench" / "results" / "stale" / "results.json", "{}")
    artifacts = tmp_path / "artifacts"
    proc = _collect(tmp_path / "repo" / "hack", artifacts, str(grader))
    assert proc.returncode == 0, proc.stderr
    assert (artifacts / "run_1" / "results.json").is_file()
    assert not (artifacts / "stale").exists()


def test_without_a_bench_dir_the_checkouts_results_are_collected(tmp_path):
    _write(tmp_path / "repo" / "bench" / "results" / "run_1" / "results.json", "{}")
    artifacts = tmp_path / "artifacts"
    proc = _collect(tmp_path / "repo" / "hack", artifacts, None)
    assert proc.returncode == 0, proc.stderr
    assert (artifacts / "run_1" / "results.json").is_file()
