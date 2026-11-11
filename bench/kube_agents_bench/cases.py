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

"""``bench/tasks/<dir>/task.yaml`` as the scorer sees it.

Replaces the two regex parsers in ``hack/ci-eval-pr.sh`` (``task_deployer``
and ``task_has_spec``), which read YAML with ``grep`` and therefore cannot
tell a real ``deployer:`` from one inside a comment or a prompt block. Nothing
else in this package parses a task file — the harness never does, because
devops-bench has already parsed it by the time the harness is called.

THE CASE ID IS THE DIRECTORY NAME. Twelve of the thirteen task files declare
``id:``, ``gpu-stress-test-diagnosis`` declares ``task_id:`` instead, and
devops-bench itself joins on neither: it writes ``folder`` into
``results.json`` and ``taskFolder`` into ``rows.json``, both the directory
name. So the directory is the join key into ``bench/baselines/<id>.jsonl`` and
into the record, and a declared id is treated as an assertion about it rather
than as the identity. :func:`load_case` raises when the two disagree, which is
the only way the baseline file, the task directory and the record can be kept
from drifting apart.
"""

from __future__ import annotations


from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from devops_bench.tasks.schema import Task as DevopsBenchTask

from kube_agents_bench import pushed_report, scope_exclude, stop_hook, worker_trajectory
from kube_agents_bench.lane import REQUESTED_KEY as _REQUESTED_KEY
from kube_agents_bench.lane import LaneSafeguardsError as _LaneSafeguardsError
from kube_agents_bench.lane import declared_pull_requests as _lane_declared_pull_requests
from kube_agents_bench.lane import requested_pull_requests as _lane_requested_pull_requests

__all__ = [
    "CASE_KEYS",
    "HARNESS_WRITTEN_ENTRIES",
    "TRACE_GRADABLE_CHECK_TYPES",
    "TRANSPORT_BLIND_CHECK_TYPES",
    "WORKER_BLIND_CHECK_TYPES",
    "CaseSpec",
    "CaseSpecError",
    "load_case",
]

# Every top-level key a task.yaml may carry, which is this grader's schema for
# the top level: neither loader rejects a key it does not know -- devops-bench
# and load_case read the keys they want and drop the rest -- so
# hack/ci-eval-pr.sh reds a pull request's case carrying a key outside main's
# copy of this set ("land the grader change first"), and tests/test_cases.py
# holds every task in the tree to it. Two halves:
#
# - devops-bench's: its task model's fields, read off the installed model so a
#   bump that adds one is allowed without an edit here (``folder`` is set by
#   the loader from the directory, never written), plus the three aliases its
#   loader maps onto them (``Task.from_dict``: ``task_id`` for ``id``,
#   ``goal`` and ``input`` for ``prompt``), which the model cannot list.
# - this repository's: what this package and the case tooling read --
#   ``domain``, ``fixtures``, ``owner``, ``expected_fail``,
#   ``tool_calls_optional``, ``audit_streams``, ``scope_exclude`` (read by
#   ``scope_exclude.py``), ``writes_alone`` and ``requested_pull_requests``
#   (read by ``lane.py`` and by ``load_case``), ``suite``
#   (``certification``: the case runs on the certification lane only, read by
#   scripts/validate_bench_cases.py against hack/eval/certification-cases.txt)
#   -- and ``task_version``, a
#   case's own revision note that nothing reads. A new one is added here, in a
#   pull request before any case a pull-request job grades uses it (the
#   presubmit roster, or a lane list such as hack/eval/gitlab-cases.txt).
_DEVOPS_BENCH_LOADER_FIELDS = frozenset({"folder"})
_DEVOPS_BENCH_ALIASES = frozenset({"task_id", "goal", "input"})
DEVOPS_BENCH_CASE_KEYS = (
    frozenset(DevopsBenchTask.model_fields) - _DEVOPS_BENCH_LOADER_FIELDS
) | _DEVOPS_BENCH_ALIASES
REPOSITORY_CASE_KEYS = frozenset(
    {
        "domain", "fixtures", "owner", "expected_fail", "tool_calls_optional",
        "audit_streams", scope_exclude.KEY, "task_version", "writes_alone", "suite",
        _REQUESTED_KEY,
    }
)
CASE_KEYS = DEVOPS_BENCH_CASE_KEYS | REPOSITORY_CASE_KEYS

# A task that provisions nothing has no infra excuse for a missing record, so
# the scorer refuses to classify its failures as INFRA. Matches the carve-out
# ci-eval-pr.sh already applies, and the default devops-bench assumes when a
# task declares no `infrastructure:` block at all.
NOOP_DEPLOYER = "noop"

# The check types that read the run's own tool calls or worker logs rather
# than the answer or the cluster: ``tool_called`` reads the trajectory,
# ``worker_commands`` the delegated cards' worker logs (and, with
# ``after_result_pattern``, the workers' trajectory entries as typed), ``worker_agents`` the
# ``agent`` tags the harness puts on the workers' trajectory entries,
# ``worker_budget`` the cards' run rows the same read collects. On the
# inject transport they are blind in two different ways, and the scorer
# sets each aside under its own condition (``scoring.py``, the inject lane).
# ``tool_called`` in its default ``router`` scope reads the delegating
# turn's own calls, which the door's trace carries once the executor
# publishes it (the transport then writes an ``a2a.activity`` marker), so
# it is set aside only on a record without the marker -- unless it sets a
# ``result`` pattern, which the trace cannot serve. ``worker_commands``
# and ``worker_agents``, and ``tool_called`` in the ``workers`` or ``all``
# scope, read the delegated cards' logs and the workers' tagged entries,
# which the trace does not carry -- it holds calls without their results,
# so no card id can be read from it. The delegation wait reads them from
# the pod instead, when the turn ran under the bridge's api executor, and
# puts them on the trajectory tagged with the worker's profile. The scorer
# grades these checks on an inject record that carries tagged worker
# entries and no ``worker_trajectory.GAP_ENTRY`` (a capture with gaps), and
# sets them aside on any other inject record. Two kinds stay set aside on a
# record with the workers too: a check that also reads the router's own
# calls, on a record whose door did not vouch for them, and a check that
# reads what the trace never carries (a router call's result or argument
# values, or ``report_times_in_window``). A check set aside is reported as
# not applicable rather than failed or errored.
# Recorded per entry NAME, because the report devops-bench writes carries
# the entry's name and not its check type.
TRACE_GRADABLE_CHECK_TYPES = frozenset({"tool_called"})
# ``report_times_in_window`` reads the report, but also every tool output in
# the trajectory, the workers' included, to tell a time the agent read from
# one it guessed; the inject trace carries the router's calls without their
# tool outputs, so it stays blind on a record that carries the workers'.
WORKER_BLIND_CHECK_TYPES = frozenset({"worker_commands", "worker_agents", "worker_budget", "report_times_in_window"})
TRANSPORT_BLIND_CHECK_TYPES = TRACE_GRADABLE_CHECK_TYPES | WORKER_BLIND_CHECK_TYPES
# Entries the harness writes on the trajectory itself (``stop_hook.py``,
# ``pushed_report.py``, ``worker_trajectory.py``), not calls read off the door's trace, so a lossy
# trace says nothing about them: a ``tool_called`` leaf naming only these is
# graded on every record, a ``result`` pattern included, since the harness
# writes the result too.
HARNESS_WRITTEN_ENTRIES = frozenset(
    {
        stop_hook.STOP_ENTRY,
        stop_hook.CARDS_STOPPED_ENTRY,
        stop_hook.STOP_REPLY_ENTRY,
        pushed_report.PLANT_ENTRY,
        worker_trajectory.GAP_ENTRY,
    }
)
_TOOL_CALLED_NAMES_KEY = "tool_names"
# ``tool_called``'s scope key and the one scope the door's trace serves
# (``verifiers.ToolCalledVerifier``: ``router`` is the default).
_TOOL_CALLED_SCOPE_KEY = "scope"
_TOOL_CALLED_ROUTER_SCOPE = "router"
# The scope that reads the router's calls and the workers' together.
_TOOL_CALLED_ALL_SCOPE = "all"
# The worker-blind type that reads every tool output, the router's included.
_REPORT_TIMES_CHECK_TYPE = "report_times_in_window"
# ``tool_called``'s result-pattern key. The door's trace records no results,
# so a leaf that sets it cannot be graded from the trace in any scope.
_TOOL_CALLED_RESULT_KEY = "result"
# ``tool_called``'s argument-pattern key. The door's trace sends each of the
# router's arguments as its shape (``<string, 51 chars>``), not its value.
_TOOL_CALLED_ARGUMENTS_KEY = "arguments"
# The compound type that negates its children ("none of these tools was
# called"), which is how a safeguard against a forbidden call is written.
_NEGATED_COMPOUND_TYPE = "none"

# The keys a check subtree nests children under: compound nodes carry
# ``checks``; a single wrapped child would be ``check``.
_CHECK_CHILD_KEYS = ("checks", "check")


class CaseSpecError(ValueError):
    """A task.yaml the scorer refuses to grade against.

    Raised rather than defaulted, deliberately. Every field below has a safe
    default except identity, and a case whose declared id disagrees with its
    directory would silently score against the wrong baseline file — the one
    failure mode this module exists to make impossible.
    """


@dataclass(frozen=True)
class CaseSpec:
    """The five things the ladder needs from a task file."""

    case_id: str
    """The directory name. The join key everywhere."""

    name: str
    """Human label for the verdict summary. Falls back to the case id."""

    domain: str | None
    """``domain:``, for per-domain reporting. scripts/validate_bench_cases.py
    rejects a task without one, so None appears only for a file read outside
    that lint."""

    deployer: str
    """``infrastructure.deployer``, defaulting to ``noop``."""

    declares_verification_spec: bool
    """Whether ``verification_spec:`` is present and non-empty. Rung 2 needs
    this to fail closed: a task that declares checks but produced no
    deterministic scores did not run them, and falling back to the judge there
    is the silent-green path the gate exists to close."""

    expected_fail: bool
    """``expected_fail:``, the eval-driven-development marker. Absent means
    False, so no existing task file needs editing."""

    path: Path
    """The task.yaml this was read from, for error messages."""

    transport_blind_checks: frozenset[str] = frozenset()
    """The names of the ``verification_spec`` entries whose check subtree is
    made of leaves of a type in :data:`TRANSPORT_BLIND_CHECK_TYPES` and
    nothing else -- a plain ``tool_called``, or a ``none``/``any``/``all``
    compound of them. A compound that mixes a blind leaf with an applicable
    one is NOT in the set: its applicable leaf can fail on any transport,
    and setting the entry aside would hide that failure, so it grades as it
    always has and fails on the inject transport the way it did before.
    Empty for a task with no such check; never consulted for a record on the
    api transport. The union of the two sets below."""

    trace_blind_checks: frozenset[str] = frozenset()
    """The entries in ``transport_blind_checks`` whose every leaf is a
    ``tool_called`` in the ``router`` scope with no ``result`` pattern:
    blind only on an inject record whose door showed no tool-call trace (no
    ``a2a.activity`` marker), and graded in full on one that did."""

    negated_trace_blind_checks: frozenset[str] = frozenset()
    """The entries in ``trace_blind_checks`` whose every leaf sits under an
    odd number of ``none`` compounds: "this tool was never called". A
    failed one is positive evidence -- the trace shows the forbidden call
    -- so on a record whose door showed the trace the scorer keeps it
    graded even when the marker reports a loss; a loss makes such a check's
    pass uncertain, never its fail. A ``none`` under a ``none`` undoes the
    negation and is not in the set: that check fails on an absence, which
    a lossy trace cannot vouch for."""

    worker_blind_checks: frozenset[str] = frozenset()
    """The entries in ``transport_blind_checks`` with any leaf that reads the
    delegated workers -- ``worker_commands``, ``worker_agents``,
    ``worker_budget``, or a ``tool_called`` in the ``workers`` or ``all`` scope -- or any
    ``tool_called`` with a ``result`` pattern, bar one over harness-written
    entries only (:data:`HARNESS_WRITTEN_ENTRIES`). The trace carries no
    results, so no card id can be read from it and no result pattern can
    match. The delegation wait reads the workers from the pod when the turn
    ran under the bridge's api executor and tags their entries, so the
    scorer grades these entries on an inject record that carries tagged
    worker entries and no capture gap, and sets them aside on any other
    inject record (``scoring._inject_lane_view``). A compound mixing such a
    leaf with a router-scope ``tool_called`` lands here, not in
    ``trace_blind_checks``: its worker leaf would still error with the
    trace shown, and grading the compound would block on it. The two sets
    below are the entries that need more than the workers."""

    worker_router_checks: frozenset[str] = frozenset()
    """The entries in ``worker_blind_checks`` with a leaf that also reads the
    router's own calls: a ``tool_called`` in the ``router`` or ``all`` scope
    with no ``result`` pattern. On a record that carries the workers they
    grade only when the door's trace vouches for the router's calls, as
    ``trace_blind_checks`` do."""

    worker_untraced_checks: frozenset[str] = frozenset()
    """The entries in ``worker_blind_checks`` with a leaf that reads what the
    inject trace never carries: a ``tool_called`` in the ``router`` or
    ``all`` scope with a ``result`` or ``arguments`` pattern (the trace
    holds the router's calls with no results and with each argument's shape
    in place of its value), or ``report_times_in_window`` (it reads every
    tool output, the router's included). Set aside on every inject record,
    workers or none."""

    tool_calls_optional: bool = False
    """``tool_calls_optional:``, for a case a correct agent can answer without
    calling a tool -- a first-turn greeting, a reply that names a command.
    Rung 3 otherwise reads an empty trajectory as no agent having run; for a
    case that sets it, a non-empty reply stands in for the trajectory as the
    run evidence, beside the billed tokens and latency every case needs.
    Absent means False."""

    requested_pull_requests: int = 0
    """How many pull requests the case requests: the larger of what its own
    requesting checks ask for (leaves of a type in
    ``lane.REQUESTING_CHECK_TYPES``) and the count it declares at its top
    level under ``requested_pull_requests``, for a case that opens more pull
    requests by design than its checks grade or a placeholder for a case the
    persona answers with a pull request before its own check lands. The
    write-safeguard carve-outs in ``scoring.classify_rep`` fire only when this
    is non-zero, and hold the charged writes to it: on a case that requested
    none a write is a genuine forbidden action and still blocks. Both counts
    are in the task file, which is why they live there: the scorer is pinned
    to the ``bench/`` tree and cannot read the lane file under ``hack/``."""


def _leaves(node: Any) -> list[dict[str, Any]]:
    """Every leaf check in a subtree, in order (a leaf that is not a mapping
    is kept as an empty one, so it counts as a leaf of no type)."""
    if isinstance(node, dict):
        children = [node.get(key) for key in _CHECK_CHILD_KEYS if node.get(key) is not None]
        if not children:
            return [node]
        return [leaf for child in children for leaf in _leaves(child)]
    if isinstance(node, list):
        return [leaf for item in node for leaf in _leaves(item)]
    return [{}]


def _harness_written(leaf: dict[str, Any]) -> bool:
    """Whether a leaf is a ``tool_called`` over harness-written entries
    only, which no transport blinds, whatever its ``result`` pattern."""
    names = leaf.get(_TOOL_CALLED_NAMES_KEY) or []
    return (
        str(leaf.get("type") or "") in TRACE_GRADABLE_CHECK_TYPES
        and bool(names)
        and set(names) <= HARNESS_WRITTEN_ENTRIES
    )


def _reads_the_workers(leaf: dict[str, Any]) -> bool:
    """Whether a transport-blind leaf is blind on every inject record: a
    worker type, a ``tool_called`` outside the ``router`` scope, or a
    ``tool_called`` with a ``result`` pattern, which the trace cannot serve."""
    kind = str(leaf.get("type") or "")
    if kind in WORKER_BLIND_CHECK_TYPES:
        return True
    if kind not in TRACE_GRADABLE_CHECK_TYPES:
        return False
    if leaf.get(_TOOL_CALLED_RESULT_KEY) is not None:
        return True
    return leaf.get(_TOOL_CALLED_SCOPE_KEY) not in (None, _TOOL_CALLED_ROUTER_SCOPE)


def _reads_the_router(leaf: dict[str, Any]) -> bool:
    """Whether a worker-reading leaf also reads the router's own calls: a
    ``tool_called`` in the ``router`` or ``all`` scope with no ``result``
    pattern."""
    return (
        str(leaf.get("type") or "") in TRACE_GRADABLE_CHECK_TYPES
        and leaf.get(_TOOL_CALLED_RESULT_KEY) is None
        and leaf.get(_TOOL_CALLED_SCOPE_KEY) in (None, _TOOL_CALLED_ROUTER_SCOPE, _TOOL_CALLED_ALL_SCOPE)
    )


def _reads_untraced_output(leaf: dict[str, Any]) -> bool:
    """Whether a worker-reading leaf reads something the inject trace never
    carries: ``report_times_in_window``, or a ``tool_called`` in the
    ``router`` or ``all`` scope with a ``result`` or ``arguments`` pattern."""
    kind = str(leaf.get("type") or "")
    if kind == _REPORT_TIMES_CHECK_TYPE:
        return True
    return (
        kind in TRACE_GRADABLE_CHECK_TYPES
        and (
            leaf.get(_TOOL_CALLED_RESULT_KEY) is not None
            or leaf.get(_TOOL_CALLED_ARGUMENTS_KEY) is not None
        )
        and leaf.get(_TOOL_CALLED_SCOPE_KEY) in (None, _TOOL_CALLED_ROUTER_SCOPE, _TOOL_CALLED_ALL_SCOPE)
    )


def _negates_every_leaf(node: Any, *, negations: int = 0) -> bool:
    """Whether every leaf of a check subtree sits under an odd number of
    ``none`` compounds, so the check as a whole FAILS only when a named
    call is present in the trajectory.

    One ``none`` over leaves, or over ``any``/``all`` of leaves, negates
    them; a ``none`` under a ``none`` undoes it, and that check's fail means
    a call is absent, which a lossy trace cannot vouch for. A subtree with
    no leaf negates nothing.
    """
    if isinstance(node, dict):
        children = [node.get(key) for key in _CHECK_CHILD_KEYS if node.get(key) is not None]
        if not children:
            return negations % 2 == 1
        if node.get("type") == _NEGATED_COMPOUND_TYPE:
            negations += 1
        return all(_negates_every_leaf(child, negations=negations) for child in children)
    if isinstance(node, list):
        return bool(node) and all(_negates_every_leaf(item, negations=negations) for item in node)
    return False


def _transport_blind_checks(
    spec: Any,
) -> tuple[frozenset[str], frozenset[str], frozenset[str], frozenset[str], frozenset[str]]:
    """The names of the spec's entries whose every leaf is transport-blind,
    split into the trace-gradable ones, the ``none``-compound subset of
    those, the worker-reading ones, and the two subsets of the
    worker-reading ones that also read the router's calls or an output the
    inject trace never carries.

    An entry without a ``name`` cannot be matched to its report line and is
    left out: the scorer then grades it as it always has, which fails closed
    rather than silently.
    """
    trace: set[str] = set()
    negated: set[str] = set()
    workers: set[str] = set()
    router: set[str] = set()
    untraced: set[str] = set()
    if not isinstance(spec, list):
        return frozenset(), frozenset(), frozenset(), frozenset(), frozenset()
    for entry in spec:
        if not isinstance(entry, dict) or entry.get("name") is None:
            continue
        check = entry.get("check")
        leaves = _leaves(check)
        if not leaves or not all(
            str(leaf.get("type") or "") in TRANSPORT_BLIND_CHECK_TYPES and not _harness_written(leaf)
            for leaf in leaves
        ):
            continue
        name = str(entry["name"])
        if any(_reads_the_workers(leaf) for leaf in leaves):
            workers.add(name)
            if any(_reads_the_router(leaf) for leaf in leaves):
                router.add(name)
            if any(_reads_untraced_output(leaf) for leaf in leaves):
                untraced.add(name)
            continue
        trace.add(name)
        if _negates_every_leaf(check):
            negated.add(name)
    return (
        frozenset(trace),
        frozenset(negated),
        frozenset(workers),
        frozenset(router),
        frozenset(untraced),
    )


def _coerce_bool(value: Any, *, field: str, path: Path) -> bool:
    """YAML's bool, and nothing looser.

    ``expected_fail: "false"`` is a string, which is truthy, which would flip
    a case into expected-fail and invert its verdict. PyYAML already maps the
    unquoted spellings (``true``/``yes``/``on`` and their negatives) to bool,
    so anything arriving here as a string was quoted on purpose or by mistake
    — either way the author did not get what they typed, and saying so beats
    guessing.
    """
    if isinstance(value, bool):
        return value
    raise CaseSpecError(
        f"{path}: {field} must be a YAML boolean, got {type(value).__name__} "
        f"({value!r}). Write `{field}: true`, unquoted."
    )


def load_case(task_yaml: str | Path) -> CaseSpec:
    """Parse one ``task.yaml`` into a :class:`CaseSpec`.

    ``task_yaml`` is the path to the file; the case id comes from its parent
    directory. Raises :class:`CaseSpecError` on anything the scorer cannot
    grade honestly — a missing file, a document that is not a mapping, or a
    declared id that disagrees with the directory.

    The case's ``requested_pull_requests`` is the larger of the count of its
    own requesting checks and the top-level ``requested_pull_requests`` key.
    """
    path = Path(task_yaml)
    if not path.is_file():
        raise CaseSpecError(f"{path}: no such task file")

    try:
        # safe_load, not load: a task.yaml is repository content, but this
        # parser also runs over whatever a pull request adds, and full_load
        # would let a task file construct arbitrary Python objects in CI.
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CaseSpecError(f"{path}: not parseable as YAML: {exc}") from exc

    if not isinstance(doc, dict):
        raise CaseSpecError(
            f"{path}: expected a YAML mapping at the top level, got "
            f"{type(doc).__name__}"
        )

    case_id = path.parent.name

    # Accept both spellings and require agreement with the directory. The
    # repository is inconsistent here (`id:` in twelve tasks, `task_id:` in
    # gpu-stress-test-diagnosis), and normalising the task files is a separate
    # change from teaching the scorer to read them.
    for field in ("id", "task_id"):
        declared = doc.get(field)
        if declared is None:
            continue
        if str(declared).strip() != case_id:
            raise CaseSpecError(
                f"{path}: declares {field}: {declared!r} but lives in "
                f"directory {case_id!r}. devops-bench reports the DIRECTORY as "
                f"`folder`, so the baseline file and the record would disagree. "
                f"Rename one to match the other."
            )

    infrastructure = doc.get("infrastructure")
    if infrastructure is None:
        deployer = NOOP_DEPLOYER
    elif isinstance(infrastructure, dict):
        deployer = str(infrastructure.get("deployer") or NOOP_DEPLOYER).strip()
    else:
        raise CaseSpecError(
            f"{path}: infrastructure: must be a mapping, got "
            f"{type(infrastructure).__name__}"
        )

    spec = doc.get("verification_spec")
    # An empty list is not a declaration. `verification_spec: []` produces no
    # checks, so treating it as "declares a spec" would trip rung 2 on every
    # run of a task that deliberately has none yet.
    declares_spec = isinstance(spec, list) and len(spec) > 0

    expected_fail_raw = doc.get("expected_fail", False)
    try:
        declared = _lane_declared_pull_requests(doc, where=str(path))
    except _LaneSafeguardsError as exc:
        raise CaseSpecError(str(exc)) from exc

    expected_fail = _coerce_bool(expected_fail_raw, field="expected_fail", path=path)
    tool_calls_optional = _coerce_bool(
        doc.get("tool_calls_optional", False), field="tool_calls_optional", path=path
    )

    domain_raw = doc.get("domain")
    domain = str(domain_raw).strip() if domain_raw is not None else None

    name_raw = doc.get("name")
    name = str(name_raw).strip() if name_raw is not None else case_id

    trace_blind, negated_trace_blind, worker_blind, worker_router, worker_untraced = (
        _transport_blind_checks(spec)
    )
    return CaseSpec(
        case_id=case_id,
        name=name,
        domain=domain,
        deployer=deployer,
        declares_verification_spec=declares_spec,
        expected_fail=expected_fail,
        path=path,
        transport_blind_checks=trace_blind | worker_blind,
        trace_blind_checks=trace_blind,
        negated_trace_blind_checks=negated_trace_blind,
        worker_blind_checks=worker_blind,
        worker_router_checks=worker_router,
        worker_untraced_checks=worker_untraced,
        tool_calls_optional=tool_calls_optional,
        requested_pull_requests=max(_lane_requested_pull_requests(spec), declared),
    )
