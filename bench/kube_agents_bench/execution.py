"""Running units: one ``devops-bench`` subprocess per repetition, graded.

The execution half of ``bench-run`` (:mod:`kube_agents_bench.runner` is the
CLI, :mod:`kube_agents_bench.report` the summaries). Each repetition is one
``python -m devops_bench <task> --agent-type kubeagents`` subprocess, the
presubmit's invocation with the presubmit's delegation ceilings, with its own
``--results-root`` so the run directory is known rather than grepped out of a
log, and its own ``AGENT_LOCAL_PORT`` so concurrent port-forwards do not
share a listener. Every repetition is graded in-process by the same
:func:`kube_agents_bench.scoring.classify_rep` the gate uses.

Scheduling copies the presubmit's locks: repetitions of one case run one at
a time (concurrent identical prompts put identical cards on one board and
stop being comparable); different cases overlap up to the lane count; a tofu
stack holds the one infra lock. ``overlap_reps`` lets a case's repetitions
overlap for speed, which the presubmit never does and which never applies to
a case that provisions a stack or writes a shared artifact.

Each unit runs in its own session and this module is the only thing that
signals it: on Ctrl-C, ``kill -INT``, the hard timeout or a sibling's crash
a unit gets exactly one SIGINT, a grace sized for a stack teardown if it has
one, then termination. devops-bench tears a stack down in a ``finally`` that
runs on SIGINT and on nothing else, and a second SIGINT would abort it.
"""

from __future__ import annotations

import concurrent.futures
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from kube_agents_bench import priors as priors_mod
from kube_agents_bench.fleet import FLEET_KUBECONFIG_DIR_ENV
from kube_agents_bench.scoring import RepResult, classify_rep, load_run
from kube_agents_bench.selection import SelectedCase
from kube_agents_bench.verifiers import LEDGER_TOKEN_ENV_VARS

__all__ = [
    "Aborted",
    "CaseRun",
    "Children",
    "Interrupted",
    "RunOptions",
    "Unit",
    "delegation_timeout_s",
    "preflight",
    "run_cases",
    "skip_reason",
    "token_command",
    "unit_command",
]

# Concurrent units. The presubmit's 4: bounded by the one gateway, LiteLLM
# and the judge quota rather than by the laptop.
DEFAULT_PARALLEL = 4
UNIT_LOG = "rep{rep}.log"
# The subprocess each repetition runs, module form so the venv's interpreter
# is the one `uv run bench-run` already started. BENCH_RUN_COMMAND overrides
# it (a shell-split string) -- the tests point it at a scripted stand-in.
DEVOPS_BENCH_MODULE = "devops_bench"
RUN_COMMAND_ENV = "BENCH_RUN_COMMAND"
AGENT_TYPE = "kubeagents"
# Environment the runner sets per unit. The harness reads the first; the
# other two are the presubmit's values (BENCH_NO_INFRA false so noop units
# still run their checks; BENCH_PARALLEL false so devops-bench does not
# derive a per-run cluster name).
LOCAL_PORT_ENV = "AGENT_LOCAL_PORT"
NO_INFRA_ENV = "BENCH_NO_INFRA"
PARALLEL_ENV = "BENCH_PARALLEL"
ENV_FALSE = "false"
# First local port for the per-unit port-forwards; unit i gets base + i. The
# presubmit's base, kept so a developer reading its logs sees familiar
# numbers.
BASE_LOCAL_PORT = 28642
# Seconds between unit launches, so N units do not open their first model
# call in the same second (burst 429s are the fan-out's failure mode).
LAUNCH_STAGGER_S = 5
# The delegation ceiling the harness waits for delegated work. The harness's
# own default is 1800s; the presubmit exports 2700 for every unit and 3000
# for the six audit-shaped cases below (hack/ci-eval-pr.sh,
# unit_delegation_timeout). A local run under the harness default would cut
# those audits short and grade as failures what the presubmit grades as
# passes, so the runner sets the presubmit's values unless the developer's
# environment already names one. tests/test_runner.py checks this list
# against the shell function.
DELEGATION_TIMEOUT_ENV = "AGENT_DELEGATION_TIMEOUT"
PRESUBMIT_DELEGATION_TIMEOUT_S = 2700
AUDIT_DELEGATION_TIMEOUT_S = 3000
AUDIT_CASES = frozenset(
    {
        "compliance-rbac-overgrant",
        "obtainability-planted-pdb",
        "stockout-pinned-pool",
        "upgrade-readiness-lagging-cluster",
        "consistency-drift-outlier",
        "fleet-cost-idle-pool",
    }
)
# Hard ceiling on one unit: the longest delegation ceiling plus what
# surrounds it (startup, the opening turn, settle, grading, teardown). A
# unit past this is hung, not slow.
UNIT_TIMEOUT_MARGIN_S = 1200
UNIT_TIMEOUT_S = AUDIT_DELEGATION_TIMEOUT_S + UNIT_TIMEOUT_MARGIN_S
# Stopping a unit. Each unit runs in its own session, so a terminal's Ctrl-C
# reaches the runner alone and the runner is the only thing that signals a
# unit: exactly one SIGINT, whether the stop came from Ctrl-C, `kill -INT`,
# the hard timeout or a sibling's crash. devops-bench tears a tofu stack down
# in a `finally` that runs on SIGINT and on nothing else, and a second SIGINT
# would abort that destroy, hence the one signal and a grace sized for a
# destroy on a stack unit before it is terminated and then killed.
INTERRUPT_GRACE_S = 10
STACK_TEARDOWN_GRACE_S = 900
KILL_GRACE_S = 10
# The file devops-bench writes at the root of a run directory; the grader
# finds the directory by it.
RESULTS_FILE = "results.json"
# The bearer token, and where to read it from when the environment lacks it:
# the secret the install writes, in the agent's namespace.
TOKEN_ENV = "PLATFORM_AGENT_TOKEN"
TOKEN_SECRET = "platform-agent-secrets"
TOKEN_SECRET_KEY = "API_SERVER_KEY"
CONTEXT_ENV = "AGENT_CLUSTER_CONTEXT"
NAMESPACE_ENV = "AGENT_NAMESPACE"
DEFAULT_NAMESPACE = "kubeagents-system"
KUBECTL = "kubectl"
KUBECTL_TIMEOUT_S = 30
# The judge. devops-bench constructs one even though only the deterministic
# checks decide, and fails the run without these. The values are the
# presubmit's, offered in the error message rather than defaulted: a judge
# model is a version-key component and should be chosen on purpose.
JUDGE_PROVIDER_ENV = "JUDGE_PROVIDER"
JUDGE_MODEL_ENV = "JUDGE_MODEL"
JUDGE_PROJECT_ENV = "GCP_PROJECT_ID"
PRESUBMIT_JUDGE_PROVIDER = "google"
PRESUBMIT_JUDGE_MODEL = "gemini-3.1-pro-preview"
# Tofu stacks and the seeded fleet.
TF_ROOT_ENV = "BENCH_TF_ROOT"
DEFAULT_TF_ROOT = "tf"
FLEET_SCRIPT = "hack/fleet-kubeconfigs.sh"
PROJECT_ENV = "PROJECT_ID"
CLUSTER_ENV = "CLUSTER_NAME"
# Duration assumed for a case with no record, for ordering and estimates:
# the presubmit's default cost hint for a probe.
DEFAULT_UNIT_SECONDS = 200
# Outcome tokens, shared with scoring.RepResult.
OUTCOME_PASS = "pass"
OUTCOME_FAIL = "fail"
OUTCOME_INFRA = "infra"
OUTCOME_BLOCKED = "blocked"


# ─── Data ────────────────────────────────────────────────────────────────────


@dataclass
class Unit:
    case: SelectedCase
    rep: int
    seq: int
    results_root: Path
    log: Path
    started: float = 0.0
    finished: float = 0.0
    run_dir: Path | None = None
    returncode: int | None = None
    result: RepResult | None = None
    failed_checks: list[str] = field(default_factory=list)
    cancelled: bool = False

    @property
    def seconds(self) -> float | None:
        if self.started and self.finished:
            return round(self.finished - self.started, 1)
        return None


@dataclass
class CaseRun:
    case: SelectedCase
    units: list[Unit] = field(default_factory=list)
    skipped: str | None = None

    @property
    def graded(self) -> list[Unit]:
        return [u for u in self.units if u.result is not None]

    @property
    def counts(self) -> Counter[str]:
        return Counter(u.result.outcome for u in self.graded)  # type: ignore[union-attr]

    @property
    def passes(self) -> int:
        return self.counts[OUTCOME_PASS]

    @property
    def fails(self) -> int:
        return self.counts[OUTCOME_FAIL]

    @property
    def scored(self) -> int:
        return self.passes + self.fails


# ─── What a case needs to run here ───────────────────────────────────────────


def skip_reason(case: SelectedCase, env: dict[str, str], include_infra: bool) -> str | None:
    if case.has_stack and not include_infra:
        return "provisions a tofu stack; pass --include-infra"
    if case.has_stack and not (env.get(PROJECT_ENV) and env.get(CLUSTER_ENV)):
        return f"provisions a tofu stack; needs {PROJECT_ENV} and {CLUSTER_ENV}"
    if case.reads_fleet and not env.get(FLEET_KUBECONFIG_DIR_ENV):
        return f"reads the seeded fleet; needs {FLEET_KUBECONFIG_DIR_ENV} (see {FLEET_SCRIPT})"
    if case.writes_shared_artifact and not any(env.get(name) for name in LEDGER_TOKEN_ENV_VARS):
        return f"its checks read GitHub; needs {' or '.join(LEDGER_TOKEN_ENV_VARS)}"
    return None


def token_command(env: dict[str, str]) -> list[str]:
    cmd = [KUBECTL]
    if env.get(CONTEXT_ENV):
        cmd += ["--context", env[CONTEXT_ENV]]
    cmd += [
        "get", "secret", TOKEN_SECRET,
        "-n", env.get(NAMESPACE_ENV, DEFAULT_NAMESPACE),
        "-o", f"go-template={{{{index .data \"{TOKEN_SECRET_KEY}\" | base64decode}}}}",
    ]
    return cmd


def _fetch_token(env: dict[str, str]) -> str | None:
    if shutil.which(KUBECTL) is None:
        return None
    try:
        proc = subprocess.run(
            token_command(env), capture_output=True, text=True, timeout=KUBECTL_TIMEOUT_S, check=False
        )
    except subprocess.TimeoutExpired:
        return None
    token = proc.stdout.strip()
    return token if proc.returncode == 0 and token else None


def preflight(env: dict[str, str], bench: Path) -> list[str]:
    """Fill in what can be derived, and return the problems that cannot."""
    problems: list[str] = []
    env.setdefault(TF_ROOT_ENV, str(bench / DEFAULT_TF_ROOT))
    if env.get(RUN_COMMAND_ENV):
        # A scripted stand-in needs neither a token nor a judge.
        return problems
    if not env.get(TOKEN_ENV):
        token = _fetch_token(env)
        if token:
            env[TOKEN_ENV] = token
        else:
            problems.append(
                f"{TOKEN_ENV} is unset and could not be read from secret {TOKEN_SECRET} "
                f"(set {CONTEXT_ENV} to your install's kubectl context)"
            )
    missing_judge = [name for name in (JUDGE_PROVIDER_ENV, JUDGE_MODEL_ENV) if not env.get(name)]
    if missing_judge:
        problems.append(
            f"{' and '.join(missing_judge)} unset; the presubmit uses "
            f"{JUDGE_PROVIDER_ENV}={PRESUBMIT_JUDGE_PROVIDER} {JUDGE_MODEL_ENV}={PRESUBMIT_JUDGE_MODEL} "
            f"with {JUDGE_PROJECT_ENV} set for Vertex AI"
        )
    elif env.get(JUDGE_PROVIDER_ENV) == PRESUBMIT_JUDGE_PROVIDER and not env.get(JUDGE_PROJECT_ENV):
        problems.append(f"{JUDGE_PROJECT_ENV} unset; the {PRESUBMIT_JUDGE_PROVIDER} judge needs it")
    return problems


# ─── Running units ───────────────────────────────────────────────────────────


class Locks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._per_case: dict[str, threading.Lock] = {}
        self.infra = threading.Lock()
        self.launch = threading.Lock()

    def for_case(self, case_id: str) -> threading.Lock:
        with self._guard:
            return self._per_case.setdefault(case_id, threading.Lock())


class Children:
    """In-flight unit subprocesses, so a stop can reach every one of them."""

    def __init__(self, log: Callable[[str], Any] = print) -> None:
        self._guard = threading.Lock()
        self._procs: dict[int, tuple[subprocess.Popen[bytes], str, bool]] = {}
        self.stopping = threading.Event()
        self._log = log

    def start(self, cmd: list[str], *, case_id: str, has_stack: bool, **popen: Any) -> subprocess.Popen[bytes] | None:
        """Start a unit and register it, or return None once stopping.

        The check and the registration share one lock, so a unit cannot
        start between :meth:`stop_all`'s snapshot and its signal. Each unit
        gets its own session (see INTERRUPT_GRACE_S): the terminal's Ctrl-C
        does not reach it, and the runner sends the one SIGINT it gets.
        """
        with self._guard:
            if self.stopping.is_set():
                return None
            proc = subprocess.Popen(cmd, start_new_session=True, **popen)
            self._procs[proc.pid] = (proc, case_id, has_stack)
            return proc

    def remove(self, proc: subprocess.Popen[bytes]) -> None:
        with self._guard:
            self._procs.pop(proc.pid, None)

    def stop_all(self) -> None:
        """Send every running unit one SIGINT, wait its grace, then terminate
        and kill what is left. Idempotent, and it survives a second Ctrl-C."""
        with self._guard:
            self.stopping.set()
            procs = list(self._procs.values())
        for proc, case_id, has_stack in procs:
            grace = STACK_TEARDOWN_GRACE_S if has_stack else INTERRUPT_GRACE_S
            if has_stack and proc.poll() is None:
                self._log(f"waiting up to {grace}s for {case_id} to tear its stack down")
            while True:
                try:
                    stop_process(proc, grace_s=grace, interrupt=True)
                    break
                except KeyboardInterrupt:
                    # A second Ctrl-C while waiting: keep waiting rather than
                    # abandoning the unit mid-teardown.
                    continue


def stop_process(proc: subprocess.Popen[bytes], *, grace_s: float, interrupt: bool = False) -> None:
    """Wait ``grace_s`` for ``proc`` to exit, then terminate, then kill.

    ``interrupt`` sends SIGINT first: devops-bench runs its stack teardown on
    SIGINT only, and the unit's own session means nothing else sent one.
    """
    if proc.poll() is not None:
        return
    if interrupt:
        proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=grace_s)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.terminate()
    try:
        proc.wait(timeout=KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def unit_command(env: dict[str, str]) -> list[str]:
    override = env.get(RUN_COMMAND_ENV)
    if override:
        return shlex.split(override)
    return [sys.executable, "-m", DEVOPS_BENCH_MODULE]


def delegation_timeout_s(case_id: str, env: dict[str, str]) -> str:
    if env.get(DELEGATION_TIMEOUT_ENV):
        return env[DELEGATION_TIMEOUT_ENV]
    if case_id in AUDIT_CASES:
        return str(AUDIT_DELEGATION_TIMEOUT_S)
    return str(PRESUBMIT_DELEGATION_TIMEOUT_S)


def _find_run_dir(results_root: Path) -> Path | None:
    if not results_root.is_dir():
        return None
    candidates = sorted(p.parent for p in results_root.rglob(RESULTS_FILE))
    return candidates[-1] if candidates else None


def run_unit(
    unit: Unit,
    *,
    env: dict[str, str],
    locks: Locks,
    children: Children,
    cwd: Path,
    stagger_s: float,
    overlap_reps: bool,
    unit_timeout_s: float = UNIT_TIMEOUT_S,
) -> Unit:
    case = unit.case
    held: list[threading.Lock] = []
    try:
        if case.exclusive or not overlap_reps:
            lock = locks.for_case(case.case_id)
            lock.acquire()
            held.append(lock)
        if case.has_stack:
            locks.infra.acquire()
            held.append(locks.infra)
        with locks.launch:
            time.sleep(stagger_s)
        unit_env = dict(env)
        unit_env[LOCAL_PORT_ENV] = str(BASE_LOCAL_PORT + unit.seq)
        unit_env[NO_INFRA_ENV] = ENV_FALSE
        unit_env[PARALLEL_ENV] = ENV_FALSE
        unit_env[DELEGATION_TIMEOUT_ENV] = delegation_timeout_s(case.case_id, env)
        unit.results_root.mkdir(parents=True, exist_ok=True)
        cmd = [
            *unit_command(env),
            str(case.task_path),
            "--agent-type",
            AGENT_TYPE,
            "--results-root",
            str(unit.results_root),
            "--run-id",
            f"{case.case_id}-rep{unit.rep}",
        ]
        unit.started = time.time()
        with unit.log.open("w", encoding="utf-8") as log:
            log.write(f"$ {shlex.join(cmd)}\n")
            log.flush()
            proc = children.start(
                cmd, case_id=case.case_id, has_stack=case.has_stack,
                cwd=str(cwd), env=unit_env, stdout=log, stderr=subprocess.STDOUT,
            )
            if proc is None:
                unit.cancelled = True
                return unit
            try:
                unit.returncode = proc.wait(timeout=unit_timeout_s)
            except subprocess.TimeoutExpired:
                grace = STACK_TEARDOWN_GRACE_S if case.has_stack else INTERRUPT_GRACE_S
                log.write(f"\nbench-run: unit exceeded {unit_timeout_s}s; sent SIGINT, waiting {grace}s\n")
                log.flush()
                stop_process(proc, grace_s=grace, interrupt=True)
                unit.returncode = proc.returncode
            finally:
                children.remove(proc)
        unit.finished = time.time()
        if children.stopping.is_set():
            unit.cancelled = True
            return unit
    finally:
        for lock in reversed(held):
            lock.release()
    unit.run_dir = _find_run_dir(unit.results_root)
    unit.result = classify_rep(case.spec, unit.run_dir, unit.rep)
    record = load_run(unit.run_dir) if unit.run_dir else None
    if record is not None:
        unit.failed_checks = [
            str(entry.get("name") or "<unnamed>")
            for entry in record.verification_report
            if str(entry.get("status") or "").lower() == OUTCOME_FAIL
        ]
    return unit


# ─── The run ─────────────────────────────────────────────────────────────────


@dataclass
class RunOptions:
    reps: int
    parallel: int
    stagger_s: float
    out_dir: Path
    include_infra: bool
    overlap_reps: bool = False
    unit_timeout_s: float = UNIT_TIMEOUT_S


class Interrupted(Exception):
    """Ctrl-C arrived. ``runs`` carries what completed."""

    def __init__(self, runs: list[CaseRun]) -> None:
        super().__init__(runs)
        self.runs = runs


class Aborted(Exception):
    """A unit raised. ``runs`` carries what completed; ``cause`` the error."""

    def __init__(self, runs: list[CaseRun], cause: BaseException) -> None:
        super().__init__(runs, cause)
        self.runs = runs
        self.cause = cause


def run_cases(
    cases: list[SelectedCase],
    priors: dict[str, priors_mod.Prior],
    options: RunOptions,
    *,
    env: dict[str, str],
    bench: Path,
    log: Callable[[str], Any] = print,
    continue_case: Callable[[CaseRun, int], bool] | None = None,
) -> list[CaseRun]:
    """Run the selection: ``options.reps`` repetitions of every runnable case.

    ``continue_case(run, planned)`` is asked, once a case has all its
    repetitions in and none in flight, whether to launch one more; None means
    a fixed count. Raises :class:`Interrupted` on Ctrl-C and :class:`Aborted`
    when a unit raised, in both cases after stopping every unit, with what
    completed on the exception's ``runs``.
    """
    runs = [CaseRun(case=c, skipped=skip_reason(c, env, options.include_infra)) for c in cases]
    active = [r for r in runs if r.skipped is None]
    for r in runs:
        if r.skipped:
            log(f"skip {r.case.case_id}: {r.skipped}")
    if not active:
        return runs

    locks = Locks()
    children = Children(log)
    seq = 0
    in_flight: dict[concurrent.futures.Future[Unit], CaseRun] = {}
    planned: Counter[str] = Counter()

    def cost(r: CaseRun) -> float:
        prior = priors.get(r.case.case_id)
        return (prior.median_seconds if prior and prior.median_seconds else None) or DEFAULT_UNIT_SECONDS

    def launch(pool: concurrent.futures.ThreadPoolExecutor, r: CaseRun) -> None:
        nonlocal seq
        seq += 1
        rep = planned[r.case.case_id] + 1
        planned[r.case.case_id] = rep
        case_dir = options.out_dir / r.case.case_id
        unit = Unit(
            case=r.case,
            rep=rep,
            seq=seq,
            results_root=case_dir / f"rep{rep}",
            log=case_dir / UNIT_LOG.format(rep=rep),
        )
        case_dir.mkdir(parents=True, exist_ok=True)
        r.units.append(unit)
        log(f">>> launching {r.case.case_id} rep {rep}")
        future = pool.submit(
            run_unit, unit, env=env, locks=locks, children=children, cwd=bench,
            stagger_s=options.stagger_s, overlap_reps=options.overlap_reps,
            unit_timeout_s=options.unit_timeout_s,
        )
        in_flight[future] = r

    def wants_more(r: CaseRun) -> bool:
        planned_here = planned[r.case.case_id]
        if planned_here < options.reps:
            return True
        return continue_case is not None and continue_case(r, planned_here)

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=options.parallel)
    try:
        # Rep-ascending, cost-descending: the presubmit's order, so the long
        # units start first and a same-case successor does not park a lane.
        for _rep in range(1, options.reps + 1):
            for r in sorted(active, key=cost, reverse=True):
                launch(pool, r)
        while in_flight:
            done, _ = concurrent.futures.wait(in_flight, return_when=concurrent.futures.FIRST_COMPLETED)
            for future in done:
                r = in_flight.pop(future)
                unit = future.result()
                assert unit.result is not None
                log(
                    f"<<< {r.case.case_id} rep {unit.rep}: {unit.result.outcome} in "
                    f"{unit.seconds or 0:.0f}s -- {unit.result.reason}"
                )
                outstanding = sum(1 for other in in_flight.values() if other is r)
                if outstanding == 0 and wants_more(r):
                    launch(pool, r)
    except KeyboardInterrupt:
        log("interrupted: stopping in-flight units and dropping the queue")
        _stop(in_flight, children, pool, runs)
        raise Interrupted(runs) from None
    except Exception as exc:
        # A unit that raised rather than graded (a missing command, a scorer
        # bug): stop the rest instead of letting them run on behind a
        # traceback, and hand the completed units up with the error.
        log("a unit raised; stopping in-flight units and dropping the queue")
        _stop(in_flight, children, pool, runs)
        raise Aborted(runs, exc) from exc
    pool.shutdown(wait=True)
    return runs


def _stop(
    in_flight: dict[concurrent.futures.Future[Unit], CaseRun],
    children: Children,
    pool: concurrent.futures.ThreadPoolExecutor,
    runs: list[CaseRun],
) -> None:
    for future in in_flight:
        future.cancel()
    children.stop_all()
    while True:
        try:
            pool.shutdown(wait=True, cancel_futures=True)
            break
        except KeyboardInterrupt:
            continue
    for r in runs:
        r.units = [u for u in r.units if u.result is not None]
