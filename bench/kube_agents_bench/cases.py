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

__all__ = ["CaseSpec", "CaseSpecError", "load_case"]

# A task that provisions nothing has no infra excuse for a missing record, so
# the scorer refuses to classify its failures as INFRA. Matches the carve-out
# ci-eval-pr.sh already applies, and the default devops-bench assumes when a
# task declares no `infrastructure:` block at all.
NOOP_DEPLOYER = "noop"


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

    return CaseSpec(
        case_id=case_id,
        name=name,
        domain=domain,
        deployer=deployer,
        declares_verification_spec=declares_spec,
        expected_fail=expected_fail,
        path=path,
    )
