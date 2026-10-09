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

__all__ = [
    "TRACE_GRADABLE_CHECK_TYPES",
    "TRANSPORT_BLIND_CHECK_TYPES",
    "WORKER_BLIND_CHECK_TYPES",
    "CaseSpec",
    "CaseSpecError",
    "load_case",
]

# A task that provisions nothing has no infra excuse for a missing record, so
# the scorer refuses to classify its failures as INFRA. Matches the carve-out
# ci-eval-pr.sh already applies, and the default devops-bench assumes when a
# task declares no `infrastructure:` block at all.
NOOP_DEPLOYER = "noop"

# The check types that read the run's own tool calls or worker logs rather
# than the answer or the cluster: ``tool_called`` reads the trajectory,
# ``worker_commands`` the delegated cards' worker logs, ``worker_agents`` the
# ``agent`` tags the harness puts on the workers' trajectory entries. On the
# inject transport they are blind in two different ways, and the scorer
# sets each aside under its own condition (``scoring.py``, the inject lane).
# ``tool_called`` in its default ``router`` scope reads the delegating
# turn's own calls, which the door's trace carries once the executor
# publishes it (the transport then writes an ``a2a.activity`` marker), so
# it is set aside only on a record without the marker. ``worker_commands``
# and ``worker_agents``, and ``tool_called`` in the ``workers`` or ``all``
# scope, read the delegated cards' logs and the workers' tagged entries,
# which the trace does not carry -- it holds calls without their results,
# so no card id can be read from it. The delegation wait reads them from
# the pod instead, when the turn ran under the bridge's api executor, but
# the scorer still sets these checks aside on every inject record, marker
# or not, until it grades them on a record that carries the workers. A
# check is reported as not applicable there rather than failed or errored.
# Recorded per entry NAME, because the report devops-bench writes carries
# the entry's name and not its check type.
TRACE_GRADABLE_CHECK_TYPES = frozenset({"tool_called"})
WORKER_BLIND_CHECK_TYPES = frozenset({"worker_commands", "worker_agents"})
TRANSPORT_BLIND_CHECK_TYPES = TRACE_GRADABLE_CHECK_TYPES | WORKER_BLIND_CHECK_TYPES
# ``tool_called``'s scope key and the one scope the door's trace serves
# (``verifiers.ToolCalledVerifier``: ``router`` is the default).
_TOOL_CALLED_SCOPE_KEY = "scope"
_TOOL_CALLED_ROUTER_SCOPE = "router"
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
    """``domain:``, for per-domain reporting. None is a real answer: a task
    that claims no domain covers no journey, which ``gpu-stress-test-diagnosis``
    documents at length in its own comments."""

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
    ``tool_called`` in the ``router`` scope: blind only on an inject record
    whose door showed no tool-call trace (no ``a2a.activity`` marker), and
    graded in full on one that did."""

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
    delegated workers -- ``worker_commands``, ``worker_agents``, or a
    ``tool_called`` in the ``workers`` or ``all`` scope. Blind on every
    inject record, trace or none: the trace carries no results, so no card
    id can be read from it. The delegation wait reads the workers from the
    pod when the turn ran under the bridge's api executor; the set-aside
    stays until the scorer grades them on a record that carries them. A
    compound mixing such a leaf with a router-scope ``tool_called`` lands
    here, not in ``trace_blind_checks``: its worker leaf would still error
    with the trace shown, and grading the compound would block on it."""


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


def _reads_the_workers(leaf: dict[str, Any]) -> bool:
    """Whether a transport-blind leaf reads the delegated workers rather than
    the delegating turn's own calls: a worker type, or a ``tool_called``
    outside the ``router`` scope."""
    kind = str(leaf.get("type") or "")
    if kind in WORKER_BLIND_CHECK_TYPES:
        return True
    scope = leaf.get(_TOOL_CALLED_SCOPE_KEY)
    return kind in TRACE_GRADABLE_CHECK_TYPES and scope not in (None, _TOOL_CALLED_ROUTER_SCOPE)


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
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """The names of the spec's entries whose every leaf is transport-blind,
    split into the trace-gradable ones, the ``none``-compound subset of
    those, and the worker-reading ones.

    An entry without a ``name`` cannot be matched to its report line and is
    left out: the scorer then grades it as it always has, which fails closed
    rather than silently.
    """
    trace: set[str] = set()
    negated: set[str] = set()
    workers: set[str] = set()
    if not isinstance(spec, list):
        return frozenset(), frozenset(), frozenset()
    for entry in spec:
        if not isinstance(entry, dict) or entry.get("name") is None:
            continue
        check = entry.get("check")
        leaves = _leaves(check)
        if not leaves or not all(
            str(leaf.get("type") or "") in TRANSPORT_BLIND_CHECK_TYPES for leaf in leaves
        ):
            continue
        name = str(entry["name"])
        if any(_reads_the_workers(leaf) for leaf in leaves):
            workers.add(name)
            continue
        trace.add(name)
        if _negates_every_leaf(check):
            negated.add(name)
    return frozenset(trace), frozenset(negated), frozenset(workers)


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
    expected_fail = _coerce_bool(expected_fail_raw, field="expected_fail", path=path)

    domain_raw = doc.get("domain")
    domain = str(domain_raw).strip() if domain_raw is not None else None

    name_raw = doc.get("name")
    name = str(name_raw).strip() if name_raw is not None else case_id

    trace_blind, negated_trace_blind, worker_blind = _transport_blind_checks(spec)

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
    )
