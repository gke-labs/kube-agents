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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from kube_agents_bench import scoring


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

    def test_counts_that_do_not_add_up_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            write_case(Path(tmp), "odd", 4, 3)
            with self.assertRaises(ab.UnreadableRun):
                ab.read_run(Path(tmp))

    def test_it_reads_the_hand_off_bench_gate_writes(self) -> None:
        """The two fields it reads are the ones CaseVerdict.to_dict writes, so
        a rename on that side fails here, not on a lane."""
        verdict = scoring.CaseVerdict(
            case_id="chat-routing-fleet-question",
            name="n",
            domain=None,
            rung=scoring.Rung.NOT_A_REAL_RUN,
            blocking=False,
            reason="",
            reps=[],
            admitted=False,
            expected_fail=False,
        )
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "case-chat-routing-fleet-question.json").write_text(
                json.dumps(verdict.to_dict()), encoding="utf-8"
            )
            self.assertEqual(
                ab.read_run(Path(tmp)),
                {"chat-routing-fleet-question": ab.Side(passes=0, scored=0)},
            )


if __name__ == "__main__":
    unittest.main()
