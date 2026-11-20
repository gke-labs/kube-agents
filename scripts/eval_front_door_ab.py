#!/usr/bin/env python3
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
"""Compare two eval runs of the same cases through the two front doors (#6539).

Run A deploys the platform front door (``EVAL_FRONT_DOOR=platform``, today's
default); run B deploys the default session (``EVAL_FRONT_DOOR=session``,
hack/ci-deploy.sh). Each run's artifact directory holds one ``case-<name>.json``
per case, the hand-off ``bench-gate case`` writes (scoring.CaseVerdict.to_dict).
This reads both and puts every case in one class:

- ``regression``: A passed at least once and B never did, both scored. The
  finding the A/B exists to catch.
- ``worse`` / ``better``: both passed at some rate, B's lower or higher.
- ``same``: the same pass rate.
- ``unscored``: a side has no scored repetition (infrastructure, or the inject
  lane set every repetition aside), so the case can't be compared.
- ``a-only`` / ``b-only``: the case ran on one side.

It prints a Markdown table, and exits 1 when any case regressed (0 otherwise,
2 on a directory it can't read), so a lane can gate on it. It reads only;
nothing is written.

Usage: scripts/eval_front_door_ab.py <run A artifacts> <run B artifacts>
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CASE_GLOB = "case-*.json"


class UnreadableRun(Exception):
    """A run directory, or a hand-off in it, that can't be compared."""


REGRESSION = "regression"
WORSE = "worse"
BETTER = "better"
SAME = "same"
UNSCORED = "unscored"
A_ONLY = "a-only"
B_ONLY = "b-only"

# Most consequential first: the order of the table and the summary.
ORDER = (REGRESSION, WORSE, UNSCORED, A_ONLY, B_ONLY, BETTER, SAME)


@dataclass(frozen=True)
class Side:
    """One case's result on one front door."""

    passes: int
    scored: int

    @property
    def rate(self) -> float | None:
        return self.passes / self.scored if self.scored else None

    def cell(self) -> str:
        return f"{self.passes}/{self.scored}" if self.scored else "unscored"


def read_run(directory: Path) -> dict[str, Side]:
    """Every case hand-off in one run's artifact directory, by case id."""
    if not directory.is_dir():
        raise UnreadableRun(f"{directory} is not a directory")
    cases: dict[str, Side] = {}
    for path in sorted(directory.glob(CASE_GLOB)):
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            raise UnreadableRun(f"{path}: {err}") from err
        if not isinstance(data, dict) or not isinstance(data.get("case"), str):
            raise UnreadableRun(f"{path}: not a case hand-off (no case id)")
        passes, scored = data.get("passes"), data.get("scored")
        if (
            not isinstance(passes, int)
            or not isinstance(scored, int)
            or passes < 0
            or scored < passes
        ):
            raise UnreadableRun(
                f"{path}: passes and scored aren't counts ({passes!r}, {scored!r})"
            )
        cases[data["case"]] = Side(passes=passes, scored=scored)
    return cases


def classify(a: Side | None, b: Side | None) -> str:
    if a is None:
        return B_ONLY
    if b is None:
        return A_ONLY
    if a.rate is None or b.rate is None:
        return UNSCORED
    if a.passes > 0 and b.passes == 0:
        return REGRESSION
    if b.rate < a.rate:
        return WORSE
    if b.rate > a.rate:
        return BETTER
    return SAME


def compare(
    a_run: dict[str, Side], b_run: dict[str, Side]
) -> list[tuple[str, str, Side | None, Side | None]]:
    rows = [
        (
            case,
            classify(a_run.get(case), b_run.get(case)),
            a_run.get(case),
            b_run.get(case),
        )
        for case in set(a_run) | set(b_run)
    ]
    return sorted(rows, key=lambda row: (ORDER.index(row[1]), row[0]))


def markdown(rows: list[tuple[str, str, Side | None, Side | None]]) -> str:
    counts = {kind: sum(1 for row in rows if row[1] == kind) for kind in ORDER}
    summary = ", ".join(f"{counts[kind]} {kind}" for kind in ORDER if counts[kind])
    lines = [
        "Front-door A/B: platform (A) vs the default session (B)",
        "",
        f"{len(rows)} cases: {summary or 'none'}",
        "",
        "| Case | Class | A passes | B passes |",
        "|---|---|---|---|",
    ]
    for case, kind, a, b in rows:
        lines.append(
            f"| `{case}` | {kind} | {a.cell() if a else '-'} | {b.cell() if b else '-'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "run_a", type=Path, help="run A's artifact directory (EVAL_FRONT_DOOR=platform)"
    )
    parser.add_argument(
        "run_b", type=Path, help="run B's artifact directory (EVAL_FRONT_DOOR=session)"
    )
    args = parser.parse_args(argv)
    try:
        rows = compare(read_run(args.run_a), read_run(args.run_b))
    except UnreadableRun as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    sys.stdout.write(markdown(rows))
    return 1 if any(kind == REGRESSION for _, kind, _, _ in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
