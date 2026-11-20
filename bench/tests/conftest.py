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
The six directories under ``fixtures/runs/`` are captured devops-bench output
(see their README for provenance and the two redacted fields), and the helpers
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
import os
import shlex
import stat
from pathlib import Path
from typing import Any

import pytest
from kube_agents_bench import harness

#: The variables that select and configure a forge: GitLab, a GitHub
#: Enterprise Server host, another GitHub API root, a private CA, the renewed
#: GitHub grading-token file (which outranks a fixture's token). Every check
#: reads them from the process environment, so a developer's shell that
#: exports them (as a GitLab dev install is told to) would turn the GitHub
#: tests' answers into another forge's. Cleared before every test; a forge
#: test's own fixture sets back what it needs.
FORGE_ENV_VARS = (
    "BENCH_FORGE",
    "BENCH_GITLAB_HOST",
    "BENCH_GITLAB_TOKEN",
    "BENCH_GITLAB_AGENT_LOGIN",
    "BENCH_GITHUB_HOST",
    "BENCH_GITHUB_API_ROOT",
    "BENCH_FORGE_CA_FILE",
    "BENCH_GITHUB_TOKEN_FILE",
)


@pytest.fixture(autouse=True)
def _no_forge_from_the_shell(monkeypatch):
    for name in FORGE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


#: What a `kubectl` without a kubeconfig reads next: the in-cluster service of a
#: pod, and a default server.
KUBECTL_FALLBACK_ENV_VARS = ("KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT", "KUBERNETES_MASTER")


@pytest.fixture(autouse=True)
def _no_cluster_from_the_shell(monkeypatch):
    """`make test-bench` runs with the caller's environment, and the code under
    test starts `kubectl` (the harness reads the agent through `kubectl exec`).
    With the caller's kubeconfig, which is KUBECONFIG or `~/.kube/config` when
    that is unset, a call that a test does not stub goes to the cluster of the
    caller's current context. Each test gets KUBECONFIG set to the null device, which names no
    cluster and keeps no write, and loses the variables in
    KUBECTL_FALLBACK_ENV_VARS. A test that needs a kubeconfig sets its own.

    This covers a test's own time only, not an import or a session fixture, and
    a `kubectl` with no cluster still tries `localhost:8080`. A test that would
    start `kubectl` must stub the call."""
    monkeypatch.setenv("KUBECONFIG", os.devnull)
    for name in KUBECTL_FALLBACK_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


#: The harness's one way into the agent's pod, `kubectl exec`, as the module has it.
_REAL_AGENT_SHELL = harness._agent_shell


@pytest.fixture(autouse=True)
def no_cluster_exec(monkeypatch) -> list[str]:
    """No test execs into an agent's pod through the harness.
    `KubeAgentsHarness.run` reads the pod's clock before each run, and after it
    settles the cards: it reads their files and then deletes them. Each step
    goes through `harness._agent_shell`, which starts `kubectl exec`. Each test
    gets a stand-in that answers as `_agent_shell` does when `kubectl` finds no
    cluster: with no text. Returns the scripts that would have run. A test of
    `_agent_shell` itself asks for `real_agent_shell`. Code with its own
    `kubectl exec` is not covered; its tests stub their own call."""
    scripts: list[str] = []

    def _record(script: str, timeout: float) -> str:
        scripts.append(script)
        return ""

    monkeypatch.setattr(harness, "_agent_shell", _record)
    return scripts


@pytest.fixture
def real_agent_shell(monkeypatch, no_cluster_exec) -> None:
    """The module's own `_agent_shell`, for a test that stubs the `subprocess`
    call below it."""
    monkeypatch.setattr(harness, "_agent_shell", _REAL_AGENT_SHELL)


@pytest.fixture(scope="session")
def _recording_kubectl(tmp_path_factory):
    """A `kubectl` first on PATH, from the first test to the last. It records
    each start on one line and fails, as `kubectl` fails where it finds no
    cluster. A test that puts its own `kubectl` first, or replaces PATH, does
    not reach it."""
    folder = tmp_path_factory.mktemp("no-host-kubectl")
    starts = folder / "starts.log"
    starts.write_text("")
    kubectl = folder / "kubectl"
    record = shlex.quote(str(starts))
    kubectl.write_text(f"#!/bin/sh\nprintf '%s' \"$*\" | tr '\\n' ' ' >> {record}\necho >> {record}\nexit 1\n")
    kubectl.chmod(kubectl.stat().st_mode | stat.S_IXUSR)
    patch = pytest.MonkeyPatch()
    patch.setenv("PATH", f"{folder}{os.pathsep}{os.environ.get('PATH', '')}")
    yield starts
    patch.undo()


@pytest.fixture(autouse=True)
def _no_test_starts_the_hosts_kubectl(_recording_kubectl):
    """A test that starts the `kubectl` on PATH gets an error here, with the
    commands: with a developer's kubeconfig that start goes to a real cluster.
    The record holds each start since the last test's check, so a start by a
    module fixture counts for the first test that uses it."""
    yield
    started = _recording_kubectl.read_text().splitlines()
    if started:
        _recording_kubectl.write_text("")
        pytest.fail(
            "this test, or a fixture set up for it, started the kubectl on PATH. Stub the call that starts it: "
            + "; ".join(f"kubectl {args.strip()}" for args in started)
        )


@pytest.fixture(autouse=True)
def _no_forge_rate_limit_wait(monkeypatch):
    """A grading read under a rate limit waits for the reset (#6105); in
    tests it doesn't, unless the test sets a budget and stubs the sleep."""
    from kube_agents_bench import verifiers

    monkeypatch.setenv("BENCH_FORGE_RATE_LIMIT_WAIT_SEC", "0")
    monkeypatch.setattr(verifiers, "_forge_rate_limit_waited", 0.0)


@pytest.fixture(autouse=True)
def _no_grader_sha_from_the_shell(monkeypatch):
    """`bench-gate suite` reads EVAL_GRADER_SHA, which hack/ci-eval-pr.sh
    exports; a shell that has it would add a key and a line to every suite
    output the tests compare, the store-unset golden's included."""
    monkeypatch.delenv("EVAL_GRADER_SHA", raising=False)


@pytest.fixture(autouse=True)
def _no_judge_skip_from_the_shell(monkeypatch):
    """hack/ci-eval-pr.sh exports KUBE_AGENTS_SKIP_REPORT_ONLY_JUDGES on a pull
    request; a shell that has it would patch two devops-bench evaluators off
    in the first test that builds a harness, for the rest of the session."""
    monkeypatch.delenv("KUBE_AGENTS_SKIP_REPORT_ONLY_JUDGES", raising=False)


FIXTURE_RUNS = Path(__file__).parent / "fixtures" / "runs"
TASKS = Path(__file__).resolve().parents[1] / "tasks"

#: The three captured repetitions of the pre-#893 `agent-kanban-smoke`, all
#: three of which fail `report-states-the-probe-title` at correctness 0.5.
RED_RUNS = ("kanban_red_1", "kanban_red_2", "kanban_red_3")

#: Two captured runs of the local prompt variant that passes honestly at 1.0.
GREEN_RUNS = ("kanban_green_1", "kanban_green_2")

#: One captured repetition of the current `agent-kanban-smoke` through the
#: inject transport (the 2026-09-25 next-mode measurement run): a correct
#: answer, a trajectory that is the transport's envelope and nothing else,
#: null tokens, and `the-kanban-card-was-actually-filed` failed at
#: correctness 0.5 because no tool call reaches that record. `results.json`
#: only: the artifact set carries no manifest or rows.
INJECT_RUN = "kanban_inject_1"


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
def measured_kanban_task(write_task) -> Path:
    """`agent-kanban-smoke` as the inject measurement run (#2039) declared it.

    The answer check, and the router-scope `kanban_create` check #4004 dropped
    from the real task file. The captured inject record (``INJECT_RUN``)
    carries both entries, so the tests of how the lane grades a check its
    transport blinds read the case this way.
    """
    return write_task(
        "agent-kanban-smoke",
        {
            "id": "agent-kanban-smoke",
            "name": "Agent kanban smoke test",
            "prompt": "Which GKE clusters are we running in this project right now? Give me the list.",
            "infrastructure": {"deployer": "noop"},
            "verification_spec": [
                {
                    "name": "the-answer-names-the-planted-fleet",
                    "role": "objective",
                    "mode": "assert",
                    "check": {"type": "report_contains", "required_phrases": ["seeded-a", "seeded-b", "seeded-c"]},
                },
                {
                    "name": "the-kanban-card-was-actually-filed",
                    "role": "objective",
                    "mode": "assert",
                    "check": {"type": "tool_called", "tool_names": ["kanban_create"], "require_success": True},
                },
            ],
        },
    )


@pytest.fixture
def write_task(tmp_path: Path):
    """Write a synthetic ``<case-id>/task.yaml`` and return its path.

    Used only where a real task file cannot express the case under test --
    an `expected_fail` marker (a real task's marker comes off when its fix
    lands) or an id/directory disagreement (which the repository correctly
    does not contain).
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


def check_passes(task_id: str, check_name: str, report: str) -> bool:
    """Whether one named ``report_contains`` check of a case passes ``report``.

    The whole check as the verifier grades it, phrases and patterns together,
    loaded from the case file, so a test that pins a case's wording cannot
    drift onto a surface or a clause the verifier does not use.
    """
    import yaml

    from kube_agents_bench import transcript
    from kube_agents_bench.verifiers import ReportContainsVerifier

    task = Path(__file__).resolve().parent.parent / "tasks" / task_id / "task.yaml"
    doc = yaml.safe_load(task.read_text())
    (check,) = [c for c in doc["verification_spec"] if c.get("name") == check_name]
    transcript.set(report, [], final_message=report, started_at=1.0)
    return ReportContainsVerifier(**check["check"]).verify(5.0).status == "pass"
