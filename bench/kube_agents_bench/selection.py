"""Which cases a local run covers, and what each one needs to run.

``bench-run`` never runs "everything" by accident: a selector is required,
and it resolves to case directories under ``bench/tasks/`` in a stated order.
Selectors, all combinable:

* a case id (``cost-idle-pool-probe``), a glob on ids (``cost-*``), or a path
  to a ``task.yaml``;
* ``--roster presubmit|blocking|nightly|all`` -- the files under
  ``hack/eval/`` that the presubmit and the nightly read, so a local run can
  reproduce exactly what CI will run;
* ``--domain <slug>`` -- every case whose ``task.yaml`` claims that domain.

Beside the id, each selected case carries what the runner needs to schedule
it: whether it provisions a tofu stack (one at a time, and only when asked),
whether it reads the seeded fleet (needs ``BENCH_FLEET_KUBECONFIG_DIR``), and
whether repetitions of it may overlap. ``kube_agents_bench.cases`` parses the
five fields the gate needs and nothing else, deliberately; this module reads
the extra ones and leaves that contract alone.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

import yaml

from kube_agents_bench.cases import CaseSpec, CaseSpecError, load_case

__all__ = [
    "ROSTER_FILES",
    "ROSTER_ALL",
    "SelectedCase",
    "SelectionError",
    "bench_dir",
    "repo_root",
    "roster_entries",
    "select_cases",
]

# The three roster files the presubmit and the nightly read, keyed by the
# name a developer types. `all` is presubmit plus nightly, the nightly job's
# matrix. Relative to the repository root.
ROSTER_FILES = {
    "presubmit": Path("hack/eval/presubmit-cases.txt"),
    "blocking": Path("hack/eval/blocking-roster.txt"),
    "nightly": Path("hack/eval/nightly-cases.txt"),
}
ROSTER_ALL = "all"

# Where the cases live, relative to the bench directory, and the file each
# case directory must hold.
TASKS_DIR = Path("tasks")
TASK_FILE = "task.yaml"

# Check types whose verifier writes to or reads a shared external artifact
# -- the ledger issue on the audit repository, the pull requests on the eval
# GitOps repository -- so the verifier needs a GitHub token, and two
# concurrent repetitions of one case would grade each other's output.
SHARED_ARTIFACT_CHECKS = frozenset({"ledger_issue_contains", "pull_request_opened"})

# Check types that read the seeded fleet through a fixture role's kubeconfig.
FLEET_CHECKS = frozenset({"fleet_resource_property"})

# The deployer value that means no infrastructure is provisioned.
NOOP_DEPLOYER = "noop"


class SelectionError(ValueError):
    """A selector that names nothing, or a roster that cannot be read."""


@dataclass(frozen=True)
class SelectedCase:
    """One case the run will cover, with what scheduling it needs."""

    spec: CaseSpec
    task_path: Path
    """Absolute path to the ``task.yaml``."""
    fixtures: tuple[str, ...]
    """Fixture roles the case declares (``fixtures:``)."""
    has_stack: bool
    """True when ``infrastructure.deployer`` is not ``noop``: the case runs a
    tofu stack and needs ``PROJECT_ID``/``CLUSTER_NAME``."""
    reads_fleet: bool
    """True when the case names fixtures or a fleet check: it needs
    ``BENCH_FLEET_KUBECONFIG_DIR``."""
    writes_shared_artifact: bool
    """True when a check reads or writes a shared external artifact (a ledger
    issue, a pull request): the run needs a GitHub token, and repetitions must
    not overlap."""
    exclusive: bool
    """True when repetitions must not overlap even under ``--overlap-reps``:
    a tofu stack or a shared artifact."""
    check_types: tuple[str, ...]

    @property
    def case_id(self) -> str:
        return self.spec.case_id


def repo_root(start: Path | None = None) -> Path:
    """The repository root: the nearest ancestor holding ``bench/tasks``."""
    here = (start or Path(__file__)).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "bench" / TASKS_DIR).is_dir():
            return candidate
    raise SelectionError(f"no bench/{TASKS_DIR} above {here}; run from a checkout")


def bench_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / "bench"


def roster_entries(text: str) -> list[str]:
    """Entries of a roster file: ``#`` comments and blank lines dropped."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            out.append(line)
    return out


def _roster_case_ids(root: Path, roster: str) -> list[str]:
    # The nightly job's matrix is presubmit plus nightly; the blocking roster
    # is a subset of presubmit and would only duplicate.
    names = ["presubmit", "nightly"] if roster == ROSTER_ALL else [roster]
    ids: list[str] = []
    for name in names:
        path = ROSTER_FILES.get(name)
        if path is None:
            raise SelectionError(
                f"unknown roster {roster!r}; one of {', '.join(ROSTER_FILES)}, {ROSTER_ALL}"
            )
        full = root / path
        if not full.is_file():
            raise SelectionError(f"roster file {full} is missing")
        for entry in roster_entries(full.read_text(encoding="utf-8")):
            # `./tasks/<id>/task.yaml` in the case files; bare ids in the
            # blocking roster. Take the id either way.
            parts = Path(entry).parts
            case_id = parts[-2] if entry.endswith(TASK_FILE) and len(parts) >= 2 else entry
            if case_id not in ids:
                ids.append(case_id)
    return ids


def _load_selected(task_path: Path) -> SelectedCase:
    spec = load_case(task_path)
    doc = yaml.safe_load(task_path.read_text(encoding="utf-8")) or {}
    fixtures_raw = doc.get("fixtures") or []
    fixtures = tuple(str(f) for f in fixtures_raw) if isinstance(fixtures_raw, list) else ()
    check_types: list[str] = []
    for entry in doc.get("verification_spec") or []:
        if not isinstance(entry, dict):
            continue
        check = entry.get("check")
        if isinstance(check, dict) and check.get("type"):
            check_types.append(str(check["type"]))
    types = frozenset(check_types)
    has_stack = spec.deployer != NOOP_DEPLOYER
    shared = bool(types & SHARED_ARTIFACT_CHECKS)
    return SelectedCase(
        spec=spec,
        task_path=task_path,
        fixtures=fixtures,
        has_stack=has_stack,
        reads_fleet=bool(fixtures) or bool(types & FLEET_CHECKS),
        writes_shared_artifact=shared,
        exclusive=has_stack or shared,
        check_types=tuple(check_types),
    )


def select_cases(
    selectors: list[str],
    *,
    roster: str | None = None,
    domains: list[str] | None = None,
    root: Path | None = None,
) -> list[SelectedCase]:
    """Resolve selectors to cases, in selector order, each case once.

    Raises :class:`SelectionError` when a selector matches nothing: a typo
    that silently ran nothing is the failure mode this refuses.
    """
    root = root or repo_root()
    tasks = bench_dir(root) / TASKS_DIR
    all_ids = sorted(p.name for p in tasks.iterdir() if (p / TASK_FILE).is_file())

    ordered: list[str] = []

    def add(case_id: str) -> None:
        if case_id not in ordered:
            ordered.append(case_id)

    for selector in selectors:
        as_path = Path(selector)
        case_dir = None
        if as_path.name == TASK_FILE and as_path.is_file():
            case_dir = as_path.resolve().parent
        elif as_path.is_dir() and (as_path / TASK_FILE).is_file():
            case_dir = as_path.resolve()
        if case_dir is not None:
            # A path is taken literally: one outside bench/tasks/ would
            # otherwise run the in-tree case of the same name in its place.
            if case_dir.parent != tasks.resolve():
                raise SelectionError(f"{selector!r} is not a case under {tasks}")
            add(case_dir.name)
            continue
        matched = [i for i in all_ids if fnmatch.fnmatchcase(i, selector)]
        if not matched:
            raise SelectionError(f"{selector!r} matches no case under {tasks}")
        for case_id in matched:
            add(case_id)

    if roster:
        for case_id in _roster_case_ids(root, roster):
            add(case_id)

    if domains:
        wanted = set(domains)
        found = False
        for case_id in all_ids:
            try:
                spec = load_case(tasks / case_id / TASK_FILE)
            except CaseSpecError:
                continue
            if spec.domain in wanted:
                add(case_id)
                found = True
        if not found:
            raise SelectionError(f"no case claims domain(s) {sorted(wanted)}")

    if not ordered:
        raise SelectionError(
            "nothing selected: name a case id, a glob, a task.yaml path, "
            "--roster, or --domain"
        )

    selected: list[SelectedCase] = []
    for case_id in ordered:
        task_path = tasks / case_id / TASK_FILE
        if not task_path.is_file():
            raise SelectionError(f"{case_id}: no {task_path}")
        selected.append(_load_selected(task_path))
    return selected
