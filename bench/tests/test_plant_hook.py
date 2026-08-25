"""The run-time fixture-planting hook: ``hack/plant-fixtures.sh``.

Some fixtures cannot be pre-planted into the standing seeded fleet and cannot
justify a per-run GKE cluster either. ``gpu-stress-test-diagnosis`` is the
shape: its entire planted incident is two ``gcloud logging write`` calls, whose
entries expire on a retention window and which no kubectl-shaped fleet probe
can confirm. The hook is the third option, and everything about it is a failure
mode if it is wrong:

* a plant that fails while the eval runs anyway grades the agent against an
  incident that was never planted, and reports the harness's mistake as the
  agent's -- the silent-green outcome this repository spends the whole
  two-speed gate preventing;
* a plant that hangs eats a job budgeted at 85 minutes;
* a plant that writes to a seeded cluster poisons every later run in that
  project, long after the run that did it is gone.

So the tests below drive the real shell, with real subprocesses and real
timeouts. Nothing here needs a cloud, a cluster, or a network: a plant script
in a test is a few lines of bash. The two that matter most, and the two named
in the change's own justification, are
``test_a_case_without_a_plant_script_is_completely_untouched`` and
``test_a_plant_that_fails_is_reported_as_a_failure``.

The last group is a lint rather than a behaviour test: it reads
``hack/ci-eval-pr.sh`` and asserts the hook is wired where it has to be. That
file is 500 lines of Prow-only orchestration whose other dependencies are a
leased GCP project and a deployed agent, so ``scripts/test_task_registration.py``
already parses it rather than runs it, and this follows that precedent.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess
import time

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "hack" / "plant-fixtures.sh"
_CI_SCRIPT = _REPO / "hack" / "ci-eval-pr.sh"
_TASKS_DIR = _REPO / "bench" / "tasks"


@pytest.fixture
def hook(tmp_path):
    """Run a snippet against a sourced ``hack/plant-fixtures.sh``.

    ``set -euo pipefail`` is on because ci-eval-pr.sh sources this file under
    exactly that, and a helper that trips errexit on a path the caller meant to
    handle kills the job instead of the case.
    """

    def _run(snippet: str, **env: str) -> subprocess.CompletedProcess:
        base = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path / "home"),
            "TMPDIR": str(tmp_path / "tmp"),
            "PROJECT_ID": "kube-agents-evals-3",
            "ARTIFACTS": str(tmp_path / "artifacts"),
        }
        base.update(env)
        (tmp_path / "home").mkdir(exist_ok=True)
        (tmp_path / "tmp").mkdir(exist_ok=True)
        return subprocess.run(
            [
                "bash",
                "-c",
                f'set -euo pipefail; source "{_SCRIPT}"; {snippet}',
            ],
            capture_output=True,
            text=True,
            check=False,
            env={k: v for k, v in base.items() if v is not None},
        )

    return _run


def _case(tmp_path, name: str, body: str | None = None) -> pathlib.Path:
    """A task directory, with a plant.sh when one is asked for."""
    case = tmp_path / "tasks" / name
    case.mkdir(parents=True, exist_ok=True)
    (case / "task.yaml").write_text(f"task_id: {name}\n")
    if body is not None:
        plant = case / "plant.sh"
        plant.write_text(body)
        plant.chmod(0o755)
    return case


def _call(case: pathlib.Path) -> str:
    return (
        f'plant_task_fixtures "{case}" "{case.name}" && rc=0 || rc=$?; '
        'echo "rc=${rc} status=${PLANT_STATUS} log=${PLANT_LOG}"'
    )


def _summary(stdout: str) -> dict[str, str]:
    line = next(ln for ln in stdout.splitlines() if ln.startswith("rc="))
    return dict(part.split("=", 1) for part in line.split(" ") if "=" in part)


# ---------------------------------------------------------------------------
# The no-plant path, which is almost every case in TASKS.
# ---------------------------------------------------------------------------


def test_a_case_without_a_plant_script_is_completely_untouched(hook, tmp_path):
    """The overwhelming majority of cases have no plant.sh.

    "No added latency, no new failure mode" is a claim about this path, and it
    is checkable: no subprocess, no artifact, no scratch directory, nothing on
    stderr, status `absent`. Anything the hook leaves behind here is something
    every case in TASKS pays for so that one case can plant.
    """
    case = _case(tmp_path, "no-plant-here")
    done = hook(_call(case))

    assert done.returncode == 0, done.stderr
    assert _summary(done.stdout) == {"rc": "0", "status": "absent", "log": ""}
    assert done.stderr == ""
    # The artifacts directory is created lazily, by the plant path only.
    assert not (tmp_path / "artifacts").exists()
    assert list((tmp_path / "tmp").iterdir()) == []


def test_a_directory_that_does_not_resolve_is_not_an_error(hook, tmp_path):
    """ci-eval-pr.sh hands over an empty TASK_DIR when the task path does not
    resolve, and the run must then behave exactly as it did before this hook
    existed -- devops-bench is still the thing that reports a missing task."""
    done = hook('plant_task_fixtures "" "ghost" && rc=0 || rc=$?; echo "rc=${rc} status=${PLANT_STATUS}"')
    assert done.returncode == 0, done.stderr
    assert _summary(done.stdout)["status"] == "absent"


# ---------------------------------------------------------------------------
# Loud failure.
# ---------------------------------------------------------------------------


def test_a_plant_that_fails_is_reported_as_a_failure(hook, tmp_path):
    """The whole point. A plant that exits non-zero must return non-zero, name
    itself, and leave a status the caller can turn into its own red line --
    never a quiet 0 that lets the eval run against a fixture nobody created."""
    case = _case(
        tmp_path,
        "broken-plant",
        "echo 'gcloud: PERMISSION_DENIED on logging.logEntries.create' >&2\nexit 3\n",
    )
    done = hook(_call(case))

    summary = _summary(done.stdout)
    assert summary["rc"] == "3"
    assert summary["status"] == "failed"
    assert "ERROR: fixture plant for broken-plant exited 3" in done.stderr
    assert "kube-agents-evals-3" in done.stderr


def test_a_failed_plant_puts_its_reason_in_the_job_log_not_only_the_artifact(
    hook, tmp_path
):
    """Prow artifacts are a download away. The reason a case went red belongs
    next to the red line, or the first thing every reader does is guess."""
    case = _case(tmp_path, "loud-plant", "echo 'quota exceeded: log entries' >&2\nexit 1\n")
    done = hook(_call(case))

    assert "quota exceeded: log entries" in done.stderr
    assert "end of plant log" in done.stderr


def test_a_failed_plant_still_writes_its_full_log_to_the_artifacts_directory(
    hook, tmp_path
):
    case = _case(tmp_path, "logged-plant", "echo line-one\necho line-two >&2\nexit 4\n")
    done = hook(_call(case))

    log = pathlib.Path(_summary(done.stdout)["log"])
    assert log == tmp_path / "artifacts" / "plant_logged-plant.log"
    text = log.read_text()
    assert "line-one" in text and "line-two" in text
    assert "=== project kube-agents-evals-3" in text
    assert "=== FAILED" in text and "exit 4" in text


def test_a_plant_that_succeeds_is_reported_and_leaves_its_log(hook, tmp_path):
    case = _case(tmp_path, "good-plant", "echo 'wrote 2 log entries'\n")
    done = hook(_call(case))

    summary = _summary(done.stdout)
    assert summary == {
        "rc": "0",
        "status": "planted",
        "log": str(tmp_path / "artifacts" / "plant_good-plant.log"),
    }
    assert "Fixture plant for good-plant: planted in kube-agents-evals-3" in done.stderr
    assert "wrote 2 log entries" in pathlib.Path(summary["log"]).read_text()


def test_a_lost_executable_bit_does_not_silently_skip_the_plant(hook, tmp_path):
    """A plant.sh committed without +x, or unpacked by something that drops the
    mode, must not become a case that plants nothing and runs green. The hook
    executes the file with bash rather than by its shebang precisely so this
    failure class does not exist."""
    case = _case(tmp_path, "not-executable", "echo planted\n")
    (case / "plant.sh").chmod(0o644)

    done = hook(_call(case))
    assert _summary(done.stdout)["status"] == "planted"
    assert "planted" in pathlib.Path(_summary(done.stdout)["log"]).read_text()


# ---------------------------------------------------------------------------
# The timeout.
# ---------------------------------------------------------------------------


def test_a_hung_plant_is_killed_at_its_budget_with_its_own_status(hook, tmp_path):
    """85 minutes is the job's whole budget; a plant script's share of it is
    bounded. The status is distinct from a plain failure because the remedies
    are: an exit code is a bug in the plant, a timeout is usually a control
    plane that is not answering."""
    case = _case(tmp_path, "hung-plant", "sleep 120\n")
    start = time.monotonic()
    done = hook(_call(case), BENCH_PLANT_TIMEOUT_SECONDS="1")
    elapsed = time.monotonic() - start

    summary = _summary(done.stdout)
    assert summary["rc"] == "124"
    assert summary["status"] == "timeout"
    assert elapsed < 60, "the hook waited far past the budget it advertised"
    assert "exceeded its 1s budget" in done.stderr
    assert "exceeded the 1s plant budget" in pathlib.Path(summary["log"]).read_text()


def test_the_default_budget_is_the_documented_one(hook, tmp_path):
    """300s is argued for in the script's header; a silent drift away from it
    would move a number the header still claims."""
    done = hook('echo "budget=$(_plant_budget_seconds)"')
    assert "budget=300" in done.stdout


def test_a_nonsense_budget_warns_and_falls_back_rather_than_killing_the_job(
    hook, tmp_path
):
    for bad in ("abc", "0", "-5", "5s"):
        done = hook('_plant_budget_seconds', BENCH_PLANT_TIMEOUT_SECONDS=bad)
        assert done.returncode == 0, done.stderr
        assert done.stdout == "300", f"{bad!r} did not fall back to the default"
        assert "is not a positive integer" in done.stderr


def test_the_bash_watchdog_bounds_a_hang_when_coreutils_timeout_is_absent(tmp_path):
    """The fallback is a real implementation, not a warning.

    A developer laptop without GNU coreutils is exactly where a new plant
    script is being written and is most likely to hang, so "no timeout binary
    here, running unbounded" would remove the bound on the machine that needs
    it most. PATH is cut down to bash and sleep so `command -v timeout` and
    `command -v gtimeout` both miss.
    """
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    for tool in ("bash", "sleep"):
        found = shutil.which(tool)
        assert found, f"{tool} is not on PATH; the test cannot build a stub PATH"
        (stub_bin / tool).symlink_to(found)

    start = time.monotonic()
    done = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'set -euo pipefail; source "{_SCRIPT}"; '
            "_plant_run_bounded 1 sleep 120 && rc=0 || rc=$?; "
            'echo "rc=${rc}"',
        ],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": str(stub_bin)},
    )
    elapsed = time.monotonic() - start

    assert "rc=124" in done.stdout, f"{done.stdout!r} {done.stderr!r}"
    assert elapsed < 60


# ---------------------------------------------------------------------------
# What the plant script receives, and what it must not.
# ---------------------------------------------------------------------------


def _env_probe() -> str:
    return (
        "{\n"
        '  echo "PROJECT_ID=${PROJECT_ID-<unset>}"\n'
        '  echo "TASK_NAME=${TASK_NAME-<unset>}"\n'
        '  echo "TASK_DIR=${TASK_DIR-<unset>}"\n'
        '  echo "PLANT_SCRATCH_DIR=${PLANT_SCRATCH_DIR-<unset>}"\n'
        '  echo "KUBECONFIG=${KUBECONFIG-<unset>}"\n'
        '  echo "BENCH_FLEET_KUBECONFIG_DIR=${BENCH_FLEET_KUBECONFIG_DIR-<unset>}"\n'
        '  echo "PLATFORM_AGENT_TOKEN=${PLATFORM_AGENT_TOKEN-<unset>}"\n'
        '  echo "JUDGE_API_KEY=${JUDGE_API_KEY-<unset>}"\n'
        '  echo "GEMINI_API_KEY=${GEMINI_API_KEY-<unset>}"\n'
        '  echo "SOMETHING_FROM_THE_JOB=${SOMETHING_FROM_THE_JOB-<unset>}"\n'
        '  echo "CWD=$(pwd -P)"\n'
        '  echo "KUBECONFIG_BYTES=$(wc -c <"${KUBECONFIG}" | tr -d " ")"\n'
        '  echo "SCRATCH_WRITABLE=$([ -w "${PLANT_SCRATCH_DIR}" ] && echo yes || echo no)"\n'
        "}\n"
    )


def _probed(hook, tmp_path, **env) -> dict[str, str]:
    case = _case(tmp_path, "env-probe", _env_probe())
    done = hook(_call(case), **env)
    assert _summary(done.stdout)["status"] == "planted", done.stderr
    log = pathlib.Path(_summary(done.stdout)["log"]).read_text()
    return dict(
        ln.split("=", 1) for ln in log.splitlines() if "=" in ln and not ln.startswith("===")
    )


def test_the_plant_receives_the_leased_project_and_its_own_identity(hook, tmp_path):
    seen = _probed(hook, tmp_path, SOMETHING_FROM_THE_JOB="inherited")

    assert seen["PROJECT_ID"] == "kube-agents-evals-3"
    assert seen["TASK_NAME"] == "env-probe"
    assert seen["TASK_DIR"] == str(tmp_path / "tasks" / "env-probe")
    assert seen["CWD"] == str((tmp_path / "tasks" / "env-probe").resolve())
    assert seen["SCRATCH_WRITABLE"] == "yes"
    # The rest of the job's environment is inherited: a plant is a gcloud
    # script, and stripping to a whitelist would break gcloud in ways nobody
    # can debug from a Prow log.
    assert seen["SOMETHING_FROM_THE_JOB"] == "inherited"


def test_the_plant_cannot_reach_any_cluster_through_the_ambient_kubeconfig(
    hook, tmp_path
):
    """The fleet is standing and never torn down, so one plant that mutates a
    shared cluster poisons every later run in that project. kubectl prefers
    KUBECONFIG over ~/.kube/config, so pointing it at an empty file inside a
    private scratch directory takes away platform-agent-host as well as the
    fleet -- and an ABSENT file would fall back rather than take anything away.
    """
    ambient = tmp_path / "home" / ".kube"
    ambient.mkdir(parents=True, exist_ok=True)
    (ambient / "config").write_text("apiVersion: v1\nkind: Config\nclusters: []\n")

    seen = _probed(
        hook,
        tmp_path,
        BENCH_FLEET_KUBECONFIG_DIR=str(tmp_path / "fleet"),
        PLATFORM_AGENT_TOKEN="a-real-bearer-token",
        JUDGE_API_KEY="a-real-model-key",
        GEMINI_API_KEY="a-real-model-key",
    )

    assert seen["KUBECONFIG"] != str(ambient / "config")
    assert seen["KUBECONFIG_BYTES"] == "0"
    assert seen["BENCH_FLEET_KUBECONFIG_DIR"] == "<unset>"


def test_the_plant_does_not_get_the_keys_that_grade_the_run(hook, tmp_path):
    """A plant needs a project and a cloud credential. The agent's bearer token
    and the judge's model key are the run's own instruments."""
    seen = _probed(
        hook,
        tmp_path,
        PLATFORM_AGENT_TOKEN="a-real-bearer-token",
        JUDGE_API_KEY="a-real-model-key",
        GEMINI_API_KEY="a-real-model-key",
    )
    assert seen["PLATFORM_AGENT_TOKEN"] == "<unset>"
    assert seen["JUDGE_API_KEY"] == "<unset>"
    assert seen["GEMINI_API_KEY"] == "<unset>"


def test_the_scratch_directory_does_not_survive_the_plant(hook, tmp_path):
    case = _case(
        tmp_path,
        "scratch-user",
        'echo "${PLANT_SCRATCH_DIR}" >"${TASK_DIR}/scratch-path"\n'
        'echo junk >"${PLANT_SCRATCH_DIR}/leftover"\n',
    )
    done = hook(_call(case))
    assert _summary(done.stdout)["status"] == "planted", done.stderr

    scratch = pathlib.Path((case / "scratch-path").read_text().strip())
    assert not scratch.exists()
    assert not scratch.parent.exists()


def test_a_plant_with_no_project_is_refused_before_it_runs(hook, tmp_path):
    """"Scoped to the leased project" is the only thing that makes a plant safe
    to run at all. Without PROJECT_ID a `gcloud logging write` lands in
    whatever project the runner's config last pointed at -- so the plant does
    not start, and the case goes red."""
    case = _case(tmp_path, "no-project", 'touch "${TASK_DIR}/it-ran"\n')
    done = hook(_call(case), PROJECT_ID="")

    summary = _summary(done.stdout)
    assert summary["rc"] == "2"
    assert summary["status"] == "unusable"
    assert not (case / "it-ran").exists(), "the plant ran without a project"
    assert "refusing to plant into an unnamed project" in done.stderr


# ---------------------------------------------------------------------------
# Idempotency: the hook's half of the contract is to state that it has none.
# ---------------------------------------------------------------------------


def test_the_hook_deduplicates_nothing_between_runs(hook, tmp_path):
    """Boskos re-leases projects, so a plant runs again in a project an earlier
    run already planted, and a retried Prow job replants within minutes. The
    hook keeps no state and skips nothing -- idempotency is the plant script's
    job, and this test pins the behaviour the docs promise rather than letting
    a future "skip if already planted" appear without one."""
    case = _case(tmp_path, "counting-plant", 'echo run >>"${TASK_DIR}/plant-count"\n')

    for _ in range(2):
        done = hook(_call(case))
        assert _summary(done.stdout)["status"] == "planted", done.stderr

    assert (case / "plant-count").read_text().split() == ["run", "run"]


# ---------------------------------------------------------------------------
# Repository lints. A convention nothing enforces is a convention that fails
# quietly the first time somebody misspells it.
# ---------------------------------------------------------------------------


def _plant_scripts() -> list[pathlib.Path]:
    return sorted(_TASKS_DIR.glob("*/plant.sh"))


def test_no_task_directory_carries_a_misnamed_plant_script():
    """`plant.sh` is discovered by its exact name and by nothing else.

    `plant.bash`, `setup.sh`, `plant.py`, `plant.sh.txt`: each looks like a
    plant to a reader, is discovered by nothing, and leaves a case evaluating
    an incident that was never planted. Deriving the path from the task
    directory is what removes the "forgot to register it" failure; this is what
    removes the "named it something else" one.
    """
    stray = []
    for candidate in sorted(_TASKS_DIR.glob("*/*")):
        if not candidate.is_file():
            continue
        name = candidate.name
        if name == "plant.sh":
            continue
        if name.startswith("plant") or name in {"setup.sh", "seed.sh", "fixtures.sh"}:
            stray.append(candidate.relative_to(_REPO))

    assert not stray, (
        "These files look like fixture-planting scripts but are discovered by "
        f"nothing: {[str(p) for p in stray]}. The hook in hack/plant-fixtures.sh "
        "runs bench/tasks/<case>/plant.sh and only that. Rename it, or move the "
        "helper somewhere it cannot be mistaken for the hook's entry point."
    )


# Patterns a plant script may not contain, with the reason each is refused.
# The hook already takes the ambient and fleet kubeconfigs away at run time
# (see test_the_plant_cannot_reach_any_cluster_through_the_ambient_kubeconfig),
# but the runner's identity holds roles/container.admin on the leased project,
# so a plant could still write itself a credential. Nothing at run time can
# stop that; this is where it is stopped.
_FORBIDDEN_IN_A_PLANT = (
    (
        r"\bkubectl\b",
        "a plant runs with an empty KUBECONFIG and reaches no cluster; a case "
        "that must plant INTO a cluster needs its own (deployer: tofu), and "
        "note the plant runs before devops-bench provisions it",
    ),
    (
        r"get-credentials",
        "writing a kubeconfig would route around the empty KUBECONFIG the hook "
        "hands over, and the seeded fleet is standing -- one write poisons "
        "every later run in that project",
    ),
    (
        r"environment=seeded|managed-by=kube-agents-seeded-fleet",
        "these are the seeded fleet's labels; a plant must never address the "
        "standing fleet at all",
    ),
)


def _fleet_violations(text: str) -> list[str]:
    found = []
    for pattern, reason in _FORBIDDEN_IN_A_PLANT:
        match = re.search(pattern, text)
        if match:
            found.append(f"{match.group(0)!r}: {reason}")
    return found


def test_no_plant_script_in_the_tree_can_reach_the_seeded_fleet():
    """Vacuous today and deliberately so: no case plants yet -- this change is
    the harness, and converting gpu-stress-test-diagnosis is a separate one.
    The check below keeps it from being a lint nobody proved works."""
    for script in _plant_scripts():
        violations = _fleet_violations(script.read_text())
        assert not violations, (
            f"{script.relative_to(_REPO)}: {violations}. See hack/plant-fixtures.sh, "
            "'IT MUST NOT WRITE TO THE SEEDED FLEET'."
        )


def test_the_fleet_lint_catches_the_shapes_it_claims_to():
    """What a plant script that routed around the empty KUBECONFIG looks like."""
    assert _fleet_violations(
        'kubectl -n seeded-debug scale deployment/payments-api --replicas=0\n'
    )
    assert _fleet_violations(
        'gcloud container clusters get-credentials seeded-a --zone us-central1-a\n'
    )
    assert _fleet_violations(
        'gcloud container clusters list --filter=resourceLabels.environment=seeded\n'
    )
    # And what an honest one looks like: project-scoped, no cluster in sight.
    assert not _fleet_violations(
        'gcloud logging write container "{...}" --project="${PROJECT_ID}" '
        "--severity=ERROR --payload-type=json\n"
    )


# ---------------------------------------------------------------------------
# The wiring in ci-eval-pr.sh.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def ci_script() -> str:
    return _CI_SCRIPT.read_text()


def test_the_ci_script_sources_the_hook(ci_script):
    assert 'source "${SCRIPT_DIR}/plant-fixtures.sh"' in ci_script


def test_the_hook_runs_inside_the_task_loop_and_before_the_eval(ci_script):
    """Order is the whole safety property. A plant that ran after the agent
    would be planting an incident into a transcript that is already written."""
    loop = ci_script.index("for TASK in ")
    call = ci_script.index("plant_task_fixtures ", loop)
    eval_call = ci_script.index("uv run devops-bench", loop)
    done = ci_script.index("\ndone\n", loop)

    assert loop < call < eval_call, (
        "plant_task_fixtures must be called inside the task loop and before the "
        "devops-bench invocation it prepares the fixture for"
    )
    assert call < done, "the call escaped the task loop; it is per-case"


def test_a_plant_failure_reds_the_case_and_skips_its_eval(ci_script):
    """The failure branch, read as a unit.

    Three things have to be true together, and any one of them alone is the
    silent-green bug: the eval must not run (`continue`), the case must be
    counted against the pull request (`FAILED_TASKS`), and it must NOT be
    filed as infrastructure weather (`INFRA_FAILED_TASKS`), which is
    deliberately non-blocking for OpenTofu stockouts and would make a plant
    failure exit 0.
    """
    start = ci_script.index("if ! plant_task_fixtures ")
    end = ci_script.index("\n  fi\n", start)
    branch = ci_script[start:end]

    assert "FAILED_TASKS+=(" in branch
    assert "INFRA_FAILED_TASKS" not in branch, (
        "a plant failure must block: it is repository code failing, not weather, "
        "and the eval after it would grade an incident that was never planted"
    )
    assert re.search(r"^\s+continue$", branch, re.M), (
        "without `continue` the eval runs anyway against a fixture that does "
        "not exist -- the exact silent-green failure the hook exists to prevent"
    )


def test_the_two_plant_failure_statuses_are_distinguishable(ci_script):
    """A distinguishable status is what lets an infra owner grep for the class
    without reading every case's line, the way RESOURCE_PREPARATION_FAILED
    already works."""
    assert "[PLANT_FAILED]" in ci_script
    assert "[PLANT_TIMED_OUT]" in ci_script
