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
  finding the A/B exists to catch, and the repository's collapse sense (every
  repetition failed): so A 1/3 against B 0/3 is a regression, while A 3/3
  against B 1/3 is only ``worse``. A ``worse`` row is read by a person, not
  gated.
- ``worse`` / ``better``: both passed at some rate, B's lower or higher.
- ``same``: the same pass rate.
- ``unscored``: a side has no scored repetition (infrastructure, or the inject
  lane set every repetition aside), so the case can't be compared.
- ``a-only`` / ``b-only``: the case ran on one side.

It prints a Markdown table and exits 1 when any case regressed, 0 otherwise.
When both runs carry their grading log (``eval-grading.log``), it also compares
each case's median answer words, infrastructure repetitions left out, and
flags a case whose answer runs over A's by more than ``LENGTH_MARGIN``; ``--length-gate`` makes such a case exit 1 too, since
answer length is part of front-door parity, and exits 2 when a run has no
shapes to compare.
It exits 2 when the two runs can't be compared: a directory it can't read, a
run with no case hand-offs (often a path one level too high), every case of B
unscored or missing (the session route broke before any case could be graded,
which the harness classes as infrastructure), no case graded on both runs (run
A graded nothing, or the two graded different cases), the runs' ``front-door.txt``
(written by the deploy) naming the doors the other way round, or a run whose
resolved route differs from the one it asked for, or wasn't recorded (#6942). It reads only;
nothing is written.

Usage: scripts/eval_front_door_ab.py <run A artifacts> <run B artifacts>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CASE_GLOB = "case-*.json"
# The deploy writes the front door it chose here (hack/ci-deploy.sh); a run
# without it predates the switch and is taken as platform.
FRONT_DOOR_FILE = "front-door.txt"
# The grading log the eval script writes beside the hand-offs: each graded
# repetition's delivered reply shape, `rep N shape: ... answer_words=W`, under
# its case's `Task <case> Result:` line (bench-gate case).
GRADING_LOG = "eval-grading.log"
TASK_LINE = re.compile(r"^Task (\S+) Result:")
REP_LINE = re.compile(r"^\s+rep (\d+): (\S+) --")
ANSWER_WORDS = re.compile(r"^\s+rep (\d+) shape: .*\banswer_words=(\d+)")
INFRA_OUTCOME = "infra"
# A case is wordier on B when its median answer runs longer than A's by more
# than this share: 3 repetitions carry noise, so small differences aren't
# flagged, nor a difference under MIN_EXTRA_WORDS.
LENGTH_MARGIN = 0.25
MIN_EXTRA_WORDS = 10


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
            or isinstance(passes, bool)
            or isinstance(scored, bool)
            or passes < 0
            or scored < passes
        ):
            raise UnreadableRun(
                f"{path}: passes and scored aren't counts ({passes!r}, {scored!r})"
            )
        cases[data["case"]] = Side(passes=passes, scored=scored)
    return cases


def front_door(directory: Path) -> str:
    """The front door a run's install resolved, platform when it recorded none.

    A deploy since #6942 records ``requested=<route>`` and ``resolved=<route>``
    (hack/ci-deploy.sh, verify_front_door), and the resolved route is the one
    the run graded. A run whose resolved route is unknown or differs from the
    requested one is refused: its deploy should have failed, and its record
    can't say which front door it graded. A record from before #6942 holds the
    requested route alone, which is all such a run can say.
    """
    path = directory / FRONT_DOOR_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return "platform"
    except OSError as err:
        raise UnreadableRun(f"{path}: {err}") from err
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    if not fields:
        return text or "platform"
    requested, resolved = fields.get("requested", "").strip(), fields.get("resolved", "").strip()
    if not resolved or resolved == "unknown":
        raise UnreadableRun(f"{path}: the route the install resolved was not recorded (requested {requested or 'nothing'})")
    if requested and requested != resolved:
        raise UnreadableRun(f"{path}: requested {requested} but the install resolved {resolved}, so the run graded {resolved}")
    return resolved


def check_comparable(
    run_a: Path, run_b: Path, a_run: dict[str, Side], b_run: dict[str, Side]
) -> None:
    """Refuse a comparison that would pass for the wrong reason."""
    for label, directory, cases in (("A", run_a, a_run), ("B", run_b, b_run)):
        if not cases:
            raise UnreadableRun(
                f"run {label} ({directory}) has no {CASE_GLOB}; is it the artifact directory itself?"
            )
    doors = (front_door(run_a), front_door(run_b))
    if doors != ("platform", "session"):
        raise UnreadableRun(
            f"run A deployed {doors[0]} and run B {doors[1]} ({FRONT_DOOR_FILE}); A is the platform run and B the session run"
        )
    if all(b_run.get(case) is None or b_run[case].rate is None for case in a_run):
        raise UnreadableRun(
            "no case of run A was graded on run B: every one is unscored or missing, so the session route never ran a case"
        )
    if not any(
        a.rate is not None
        and b_run.get(case) is not None
        and b_run[case].rate is not None
        for case, a in a_run.items()
    ):
        raise UnreadableRun(
            "no case was graded on both runs: run A graded nothing, or the two runs graded different cases"
        )


def read_answer_words(directory: Path) -> dict[str, float]:
    """Each case's answer words in a run, from its grading log: the
    nearest-rank median over its repetitions that weren't infrastructure, as
    the dashboard's shape rollup takes it. Empty when the run has no log (an
    older run, or one that delivered nothing)."""
    path = directory / GRADING_LOG
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise UnreadableRun(f"{path}: {err}") from err
    words: dict[str, list[int]] = {}
    case, infra = None, set()
    for line in lines:
        if m := TASK_LINE.match(line):
            case, infra = m.group(1), set()
        elif case and (m := REP_LINE.match(line)):
            if m.group(2) == INFRA_OUTCOME:
                infra.add(m.group(1))
        elif case and (m := ANSWER_WORDS.match(line)) and m.group(1) not in infra:
            words.setdefault(case, []).append(int(m.group(2)))
    return {c: sorted(w)[(len(w) - 1) // 2] for c, w in words.items() if w}


def wordier(a_words: float | None, b_words: float | None) -> bool:
    """Whether B's answer is longer than A's by more than LENGTH_MARGIN and
    by at least MIN_EXTRA_WORDS."""
    return (
        a_words is not None
        and b_words is not None
        and b_words > a_words * (1 + LENGTH_MARGIN)
        and b_words - a_words >= MIN_EXTRA_WORDS
    )


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


def markdown(
    rows: list[tuple[str, str, Side | None, Side | None]],
    a_words: dict[str, float] | None = None,
    b_words: dict[str, float] | None = None,
) -> str:
    counts = {kind: sum(1 for row in rows if row[1] == kind) for kind in ORDER}
    summary = ", ".join(f"{counts[kind]} {kind}" for kind in ORDER if counts[kind])
    lengths = bool(a_words) and bool(b_words)
    lines = [
        "Front-door A/B: platform (A) vs the default session (B)",
        "",
        f"{len(rows)} cases: {summary or 'none'}",
    ]
    if lengths:
        flagged = sum(
            1 for case, *_ in rows if wordier(a_words.get(case), b_words.get(case))
        )
        lines.append(
            f"Answer length: {flagged} {'case' if flagged == 1 else 'cases'} wordier on B "
            f"(median answer words over A's by more than {LENGTH_MARGIN:.0%} and {MIN_EXTRA_WORDS} words)"
        )
    lines += [
        "",
        "| Case | Class | A passes | B passes |"
        + (" A words | B words | |" if lengths else ""),
        "|---|---|---|---|" + ("---|---|---|" if lengths else ""),
    ]
    for case, kind, a, b in rows:
        row = f"| `{case}` | {kind} | {a.cell() if a else '-'} | {b.cell() if b else '-'} |"
        if lengths:
            aw, bw = a_words.get(case), b_words.get(case)
            cell = lambda w: "-" if w is None else str(round(w))
            row += (
                f" {cell(aw)} | {cell(bw)} | {'wordier' if wordier(aw, bw) else ''} |"
            )
        lines.append(row)
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "run_a", type=Path, help="run A's artifact directory (EVAL_FRONT_DOOR=platform)"
    )
    parser.add_argument(
        "run_b", type=Path, help="run B's artifact directory (EVAL_FRONT_DOOR=session)"
    )
    parser.add_argument(
        "--length-gate",
        action="store_true",
        help="also exit 1 when a case is wordier on B (answer length is part of front-door parity)",
    )
    args = parser.parse_args(argv)
    try:
        a_run, b_run = read_run(args.run_a), read_run(args.run_b)
        check_comparable(args.run_a, args.run_b, a_run, b_run)
        rows = compare(a_run, b_run)
        a_words, b_words = read_answer_words(args.run_a), read_answer_words(args.run_b)
    except UnreadableRun as err:
        print(f"ERROR: {err}", file=sys.stderr)
        return 2
    sys.stdout.write(markdown(rows, a_words, b_words))
    if any(kind == REGRESSION for _, kind, _, _ in rows):
        return 1
    if args.length_gate:
        if not any(c in a_words and c in b_words for c, *_ in rows):
            print(
                f"ERROR: --length-gate needs answer shapes for at least one case on both runs ({GRADING_LOG}); none overlap",
                file=sys.stderr,
            )
            return 2
        if any(wordier(a_words.get(c), b_words.get(c)) for c, *_ in rows):
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
