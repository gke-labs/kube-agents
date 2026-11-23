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
"""Tests for scripts/eval_front_door_ab.py, the front-door A/B compare (#6539)."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_front_door_ab as ab


def write_case(directory: Path, case: str, passes: int, scored: int) -> None:
    (directory / f"case-{case}.json").write_text(
        json.dumps({"case": case, "passes": passes, "scored": scored}), encoding="utf-8"
    )


class CompareTest(unittest.TestCase):
    def run_ab(
        self, a_cases: dict[str, tuple[int, int]], b_cases: dict[str, tuple[int, int]]
    ) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a"), Path(tmp, "b")
            a.mkdir()
            b.mkdir()
            for case, (passes, scored) in a_cases.items():
                write_case(a, case, passes, scored)
            for case, (passes, scored) in b_cases.items():
                write_case(b, case, passes, scored)
            (b / ab.FRONT_DOOR_FILE).write_text("session\n", encoding="utf-8")
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = ab.main([str(a), str(b)])
            return code, out.getvalue(), err.getvalue()

    def test_each_class(self) -> None:
        code, out, _ = self.run_ab(
            {
                "lost": (2, 3),
                "dropped": (3, 3),
                "gained": (1, 3),
                "held": (3, 3),
                "weather": (2, 3),
                "gone": (3, 3),
            },
            {
                "lost": (0, 3),
                "dropped": (2, 3),
                "gained": (3, 3),
                "held": (3, 3),
                "weather": (0, 0),
                "new": (1, 3),
            },
        )
        self.assertEqual(code, 1)
        rows = {
            line.split("|")[1].strip(" `"): line.split("|")[2].strip()
            for line in out.splitlines()
            if line.startswith("| `")
        }
        self.assertEqual(
            rows,
            {
                "lost": "regression",
                "dropped": "worse",
                "gained": "better",
                "held": "same",
                "weather": "unscored",
                "gone": "a-only",
                "new": "b-only",
            },
        )
        # The regression leads the table.
        self.assertTrue(
            out.split("|---|---|---|---|\n")[1].startswith(
                "| `lost` | regression | 2/3 | 0/3 |"
            )
        )

    def test_no_regression_exits_zero(self) -> None:
        code, out, _ = self.run_ab(
            {"held": (3, 3), "flaky": (0, 3)}, {"held": (3, 3), "flaky": (0, 3)}
        )
        self.assertEqual(code, 0)
        self.assertIn("2 cases: 2 same", out)

    def test_a_case_that_failed_on_both_is_not_a_regression(self) -> None:
        code, _, _ = self.run_ab({"broken": (0, 3)}, {"broken": (0, 3)})
        self.assertEqual(code, 0)

    def test_an_unreadable_run_exits_two(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp, "a")
            a.mkdir()
            (a / "case-bad.json").write_text("{not json", encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(ab.main([str(a), str(a)]), 2)
                self.assertEqual(ab.main([str(Path(tmp, "missing")), str(a)]), 2)
            self.assertIn("case-bad.json", err.getvalue())

    def test_a_run_b_where_nothing_was_graded_cannot_pass(self) -> None:
        """A session route that broke before any case ran leaves every B case
        unscored, which must not read as no regression."""
        code, _, err = self.run_ab({"held": (3, 3), "other": (2, 3)}, {"held": (0, 0)})
        self.assertEqual(code, 2)
        self.assertIn("never ran a case", err)

    def test_no_case_graded_on_both_runs_cannot_pass(self) -> None:
        """Run A graded nothing, or the two runs graded different cases: no
        case was compared, which must not read as no regression."""
        for a_cases, b_cases in (
            ({"held": (0, 0), "other": (0, 0)}, {"held": (0, 3), "other": (3, 3)}),
            ({"case1": (3, 3), "case2": (0, 0)}, {"case1": (0, 0), "case2": (0, 3)}),
        ):
            with self.subTest(a=a_cases, b=b_cases):
                code, _, err = self.run_ab(a_cases, b_cases)
                self.assertEqual(code, 2)
                self.assertIn("no case was graded on both runs", err)

    def test_an_empty_run_cannot_pass(self) -> None:
        code, _, err = self.run_ab({}, {"held": (3, 3)})
        self.assertEqual(code, 2)
        self.assertIn("has no case-*.json", err)

    def test_swapped_runs_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a"), Path(tmp, "b")
            for d, door in ((a, "session"), (b, "platform")):
                d.mkdir()
                write_case(d, "held", 3, 3)
                (d / ab.FRONT_DOOR_FILE).write_text(door, encoding="utf-8")
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                self.assertEqual(ab.main([str(a), str(b)]), 2)
            self.assertIn("run A deployed session and run B platform", err.getvalue())

    def test_booleans_are_not_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "case-x.json").write_text(
                json.dumps({"case": "x", "passes": True, "scored": 3}), encoding="utf-8"
            )
            with self.assertRaises(ab.UnreadableRun):
                ab.read_run(Path(tmp))

    def run_ab_with_words(
        self, a_words: dict[str, list[int]], b_words: dict[str, list[int]], *extra: str
    ) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a"), Path(tmp, "b")
            for d, words in ((a, a_words), (b, b_words)):
                d.mkdir()
                log = []
                for case, reps in words.items():
                    write_case(d, case, 3, 3)
                    log.append(f"Task {case} Result: [PASSED]")
                    log += [
                        f"  rep {i + 1} shape: opens_on_verdict=1 answer_words={w}"
                        for i, w in enumerate(reps)
                    ]
                (d / ab.GRADING_LOG).write_text("\n".join(log) + "\n", encoding="utf-8")
            (b / ab.FRONT_DOOR_FILE).write_text("session\n", encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                code = ab.main([*extra, str(a), str(b)])
            return code, out.getvalue()

    def test_answer_length_is_compared_from_the_grading_logs(self) -> None:
        """A case whose median answer on B runs over A's by more than the margin
        is flagged wordier; within the margin it isn't."""
        code, out = self.run_ab_with_words(
            {"long": [100, 100, 100], "close": [200, 200, 200]},
            {"long": [200, 200, 200], "close": [240, 240, 240]},
        )
        self.assertEqual(code, 0)
        self.assertIn("Answer length: 1 case wordier on B", out)
        self.assertIn("| `long` | same | 3/3 | 3/3 | 100 | 200 | wordier |", out)
        self.assertIn("| `close` | same | 3/3 | 3/3 | 200 | 240 |  |", out)

    def test_the_length_gate_fails_a_wordier_case(self) -> None:
        code, _ = self.run_ab_with_words(
            {"long": [100]}, {"long": [200]}, "--length-gate"
        )
        self.assertEqual(code, 1)
        code, _ = self.run_ab_with_words(
            {"long": [100]}, {"long": [110]}, "--length-gate"
        )
        self.assertEqual(code, 0)

    def test_infra_repetitions_are_left_out_and_the_median_is_taken(self) -> None:
        """An infrastructure repetition's shape doesn't count, and one long
        repetition among three moves the median, not the whole figure."""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / ab.GRADING_LOG).write_text(
                "Task c Result: [PASSED]\n"
                "  rep 1: pass -- ok\n  rep 1 shape: answer_words=100\n"
                "  rep 2: infra -- cut short\n  rep 2 shape: answer_words=5\n"
                "  rep 3: pass -- ok\n  rep 3 shape: answer_words=900\n"
                "  rep 4: pass -- ok\n  rep 4 shape: answer_words=120\n"
                "  rep 9 report: quoted text with rep 1 shape: answer_words=99999\n",
                encoding="utf-8",
            )
            self.assertEqual(ab.read_answer_words(d), {"c": 120})

    def test_the_length_gate_needs_shapes_on_both_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a"), Path(tmp, "b")
            for d in (a, b):
                d.mkdir()
                write_case(d, "held", 3, 3)
            (b / ab.FRONT_DOOR_FILE).write_text("session\n", encoding="utf-8")
            errout = io.StringIO()
            with redirect_stdout(io.StringIO()), redirect_stderr(errout):
                self.assertEqual(ab.main(["--length-gate", str(a), str(b)]), 2)
            self.assertIn("--length-gate needs answer shapes", errout.getvalue())

    def test_the_length_gate_needs_a_case_with_shapes_on_both_runs(self) -> None:
        """Shapes for different cases on the two runs compare nothing, which
        the gate refuses rather than passes."""
        code, _ = self.run_ab_with_words(
            {"x": [100], "y": []}, {"x": [], "y": [100]}, "--length-gate"
        )
        self.assertEqual(code, 2)

    def test_a_small_absolute_difference_is_not_wordier(self) -> None:
        self.assertFalse(ab.wordier(0, 5))
        self.assertFalse(ab.wordier(10, 19))
        self.assertTrue(ab.wordier(10, 30))

    def test_runs_without_grading_logs_show_no_length_columns(self) -> None:
        code, out, _ = self.run_ab({"held": (3, 3)}, {"held": (3, 3)})
        self.assertEqual(code, 0)
        self.assertNotIn("A words", out)

    def test_counts_that_do_not_add_up_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_case(Path(tmp), "odd", 4, 3)
            with self.assertRaises(ab.UnreadableRun):
                ab.read_run(Path(tmp))

    def test_it_reads_the_hand_off_bench_gate_writes(self) -> None:
        """The fields it reads are the ones CaseVerdict.to_dict writes, so a
        rename on that side fails here, not on a lane. Read from the source:
        this job doesn't install the bench's dependencies."""
        scoring = (
            Path(__file__).resolve().parent.parent
            / "bench"
            / "kube_agents_bench"
            / "scoring.py"
        ).read_text(encoding="utf-8")
        to_dict = scoring[
            scoring.index(
                '    def to_dict(self) -> dict[str, Any]:\n        """The hand-off the shell writes per case'
            ) :
        ]
        for field in (
            '"case": self.case_id',
            '"passes": self.passes',
            '"scored": len(self.scored_reps)',
        ):
            self.assertIn(field, to_dict[:2000])
        self.assertIn(
            '--json-out "${ARTIFACT_DIR}/case-${name}.json"',
            (
                Path(__file__).resolve().parent.parent / "hack" / "ci-eval-pr.sh"
            ).read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
