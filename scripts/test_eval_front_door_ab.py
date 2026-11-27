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

    def test_the_front_door_is_the_route_the_install_resolved(self) -> None:
        """Since #6942 a deploy records the requested and the resolved route,
        and the resolved one is what the run graded. A record from before it
        holds the requested route alone, and no record means platform."""
        cases = {
            "session\n": "session",
            "requested=session\nresolved=session\n": "session",
            "requested=platform\nresolved=platform\n": "platform",
        }
        for text, want in cases.items():
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, ab.FRONT_DOOR_FILE).write_text(text, encoding="utf-8")
                self.assertEqual(ab.front_door(Path(tmp)), want)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ab.front_door(Path(tmp)), "platform")

    def test_a_run_that_resolved_another_route_is_refused(self) -> None:
        """A session run whose install resolved platform graded platform: the
        compare refuses it rather than read it as the session run, and one
        whose resolved route is unknown can't say what it graded."""
        for text, says in (
            ("requested=session\nresolved=platform\n", "requested session but the install resolved platform"),
            ("requested=session\nresolved=unknown\n", "was not recorded"),
            ("requested=session\n", "was not recorded"),
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                Path(tmp, ab.FRONT_DOOR_FILE).write_text(text, encoding="utf-8")
                with self.assertRaises(ab.UnreadableRun) as raised:
                    ab.front_door(Path(tmp))
                self.assertIn(says, str(raised.exception))

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


PLATFORM_RECORD = "requested=platform\nresolved=platform\n"
SESSION_RECORD = "requested=session\nresolved=session\n"


class TwoRunFormTest(unittest.TestCase):
    """The form of before the pair mode: run A, then run B."""

    def test_its_output_did_not_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a"), Path(tmp, "b")
            for directory, record, cases, words in (
                (a, PLATFORM_RECORD, {"lost": (2, 3), "held": (3, 3), "gone": (3, 3)}, 40),
                (b, SESSION_RECORD, {"lost": (0, 3), "held": (3, 3), "new": (1, 3)}, 90),
            ):
                directory.mkdir()
                for case, (passes, scored) in cases.items():
                    write_case(directory, case, passes, scored)
                (directory / ab.FRONT_DOOR_FILE).write_text(record, encoding="utf-8")
                (directory / ab.GRADING_LOG).write_text(
                    "".join(f"Task {case} Result:\n  rep 1: pass -- x\n  rep 1 shape: answer_words={words}\n" for case in cases),
                    encoding="utf-8",
                )
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = ab.main([str(a), str(b)])
        self.assertEqual((code, err.getvalue()), (1, ""))
        self.assertEqual(
            out.getvalue(),
            "Front-door A/B: platform (A) vs the default session (B)\n"
            "\n"
            "4 cases: 1 regression, 1 a-only, 1 b-only, 1 same\n"
            "Answer length: 2 cases wordier on B (median answer words over A's by more than 25% and 10 words)\n"
            "\n"
            "| Case | Class | A passes | B passes | A words | B words | |\n"
            "|---|---|---|---|---|---|---|\n"
            "| `lost` | regression | 2/3 | 0/3 | 40 | 90 | wordier |\n"
            "| `gone` | a-only | 3/3 | - | 40 | - |  |\n"
            "| `new` | b-only | - | 1/3 | - | 90 |  |\n"
            "| `held` | same | 3/3 | 3/3 | 40 | 90 | wordier |\n",
        )

    def test_an_option_can_stand_between_the_two_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a, b = Path(tmp, "a"), Path(tmp, "b")
            for directory, record in ((a, PLATFORM_RECORD), (b, SESSION_RECORD)):
                directory.mkdir()
                write_case(directory, "held", 3, 3)
                (directory / ab.FRONT_DOOR_FILE).write_text(record, encoding="utf-8")
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as err:
                code = ab.main([str(a), "--length-gate", str(b)])
        self.assertEqual(code, 2)
        self.assertIn("--length-gate needs answer shapes", err.getvalue())

    def test_it_takes_two_directories(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for argv in ([tmp], [tmp, tmp, tmp]):
                with self.subTest(directories=len(argv)), redirect_stderr(io.StringIO()) as err:
                    with self.assertRaises(SystemExit) as stop:
                        ab.main(argv)
                    self.assertEqual(stop.exception.code, 2)
                    self.assertIn("give run A's artifact directory, then run B's", err.getvalue())


class PairsTest(unittest.TestCase):
    """--pairs: the two cases of a line of the pairs file, in one run or in several."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.pairs_file = self.root / "pairs.txt"
        self.pairs_file.write_text("# why\n\nalpha-a alpha-b  # the first pair\n", encoding="utf-8")

    def run_dir(
        self, name: str, cases: dict[str, tuple[int, int]], *, record: str | None = PLATFORM_RECORD, started: str | None = None, baseline: str | None = None
    ) -> Path:
        """`<name>/artifacts`, as a Prow build lays a run out."""
        directory = self.root / name / "artifacts"
        directory.mkdir(parents=True)
        for case, (passes, scored) in cases.items():
            write_case(directory, case, passes, scored)
        if record is not None:
            (directory / ab.FRONT_DOOR_FILE).write_text(record, encoding="utf-8")
        if started is not None:
            (directory.parent / ab.STARTED_FILE).write_text(json.dumps({ab.STARTED_COMMIT_KEY: started}), encoding="utf-8")
        if baseline is not None:
            (directory / ab.BASELINE_FILE).write_text(json.dumps({"case": "x", ab.BASELINE_COMMIT_KEY: baseline}) + "\n{not json\n", encoding="utf-8")
        return directory

    def pairs(self, *directories: Path) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = ab.main(["--pairs", str(self.pairs_file), *map(str, directories)])
        return code, out.getvalue(), err.getvalue()

    def rows(self, out: str) -> dict[str, list[str]]:
        table = [line.strip("|").split("|") for line in out.splitlines() if line.startswith("| `")]
        return {cells[0].strip().strip("`"): [cell.strip() for cell in cells[1:]] for cells in table}

    def test_each_class_in_one_run(self) -> None:
        sides = {
            "lost": ((2, 3), (0, 3)), "dropped": ((3, 3), (2, 3)), "gained": ((1, 3), (3, 3)), "held": ((3, 3), (3, 3)),
            "weather": ((2, 3), (0, 0)), "platform-side": ((3, 3), None), "profile-side": (None, (1, 3)), "neither": (None, None),
        }
        self.pairs_file.write_text("".join(f"{stem}-a {stem}-b\n" for stem in sides), encoding="utf-8")
        cases = {f"{stem}-{side}": counts for stem, both in sides.items() for side, counts in zip("ab", both) if counts}
        code, out, err = self.pairs(self.run_dir("night", {**cases, "unrelated": (0, 3)}))
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(
            {label: cells[0] for label, cells in self.rows(out).items()},
            {
                "lost-a / lost-b": "regression", "dropped-a / dropped-b": "worse", "weather-a / weather-b": "unscored",
                "platform-side-a / platform-side-b": "a-only", "profile-side-a / profile-side-b": "b-only",
                "gained-a / gained-b": "better", "held-a / held-b": "same",
            },
        )
        self.assertEqual(self.rows(out)["lost-a / lost-b"], ["regression", "2/3", "0/3"])
        self.assertTrue(out.startswith("Permit pairs: the platform agent (A) vs the profile (B)\n\n7 cases: 1 regression, 1 worse, 1 unscored, 1 a-only, 1 b-only, 1 better, 1 same\n"), out)
        self.assertIn("- Note: `neither-a / neither-b` ran in none of these directories.", out)
        self.assertNotIn("Answer length", out)
        self.assertNotIn("unrelated", out)

    def test_no_regression_exits_zero(self) -> None:
        code, out, err = self.pairs(self.run_dir("night", {"alpha-a": (3, 3), "alpha-b": (1, 3)}, started="a" * 40))
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.rows(out), {"alpha-a / alpha-b": ["worse", "3/3", "1/3"]})
        self.assertIn(f"- `{self.root / 'night' / 'artifacts'}`: front door platform, commit aaaaaaaaa", out)
        self.assertNotIn("Note:", out)

    def test_the_sides_can_be_in_two_runs_and_their_commits_are_printed(self) -> None:
        a = self.run_dir("writers-c", {"alpha-a": (3, 3), "shared": (1, 3)}, started="1" * 40, baseline="2" * 40)
        b = self.run_dir("writers-b", {"alpha-b": (3, 3), "shared": (2, 3)}, baseline="2" * 40)
        code, out, _ = self.pairs(a, b)
        self.assertEqual(code, 0)
        self.assertEqual(self.rows(out), {"alpha-a / alpha-b": ["same", "3/3", "3/3"]})
        self.assertIn(f"- `{a}`: front door platform, commit 111111111", out)
        self.assertIn(f"- `{b}`: front door platform, commit 222222222", out)
        self.assertIn("- Note: the sides of `alpha-a / alpha-b` ran at different commits (111111111, 222222222).", out)

    def test_a_side_with_no_recorded_commit_gets_no_commit_note(self) -> None:
        """The tool does not know that the commits differ."""
        a = self.run_dir("recorded", {"alpha-a": (3, 3)}, started="1" * 40)
        b = self.run_dir("copied", {"alpha-b": (3, 3)})
        code, out, _ = self.pairs(a, b)
        self.assertEqual(code, 0)
        self.assertIn(f"- `{b}`: front door platform, commit not recorded", out)
        self.assertNotIn("Note:", out)

    def test_a_commit_that_no_record_holds_is_not_recorded(self) -> None:
        directory = self.run_dir("dev", {"alpha-a": (3, 3), "alpha-b": (3, 3)})
        self.assertEqual(ab.run_commit(directory), ab.NOT_RECORDED)
        (directory.parent / ab.STARTED_FILE).write_text("{", encoding="utf-8")
        (directory / ab.BASELINE_FILE).write_text("", encoding="utf-8")
        self.assertEqual(ab.run_commit(directory), ab.NOT_RECORDED)
        (directory.parent / ab.STARTED_FILE).write_text(json.dumps({"timestamp": 1}), encoding="utf-8")
        code, out, _ = self.pairs(directory)
        self.assertEqual(code, 0)
        self.assertIn("front door platform, commit not recorded", out)

    def test_a_side_a_that_never_passed_gets_a_note(self) -> None:
        """0/3 against 0/3 is `same` and exit 0, and shows nothing about the capability."""
        code, out, _ = self.pairs(self.run_dir("night", {"alpha-a": (0, 3), "alpha-b": (0, 3)}))
        self.assertEqual(code, 0)
        self.assertEqual(self.rows(out)["alpha-a / alpha-b"][0], "same")
        self.assertIn("- Note: side A of `alpha-a / alpha-b` never passed, so the pair shows nothing about the capability.", out)

    def test_a_side_a_off_the_platform_front_door_is_refused(self) -> None:
        """There the default session takes a default-route case, so the run did not measure the
        platform agent. The route is the one `front_door` reads."""
        for label, record, message in (
            ("the session route", SESSION_RECORD, "which deployed session"),
            ("a record of before the resolved route", "session\n", "which deployed session"),
            ("a route that the install did not resolve", "requested=platform\nresolved=session\n", "requested platform but the install resolved session"),
            ("no resolved route", "requested=platform\nresolved=unknown\n", "was not recorded"),
        ):
            with self.subTest(label):
                code, out, err = self.pairs(self.run_dir(label.replace(" ", "-"), {"alpha-a": (3, 3), "alpha-b": (3, 3)}, record=record))
                self.assertEqual((code, out), (2, ""))
                self.assertIn(message, err)

    def test_a_run_with_no_record_is_a_platform_run_as_in_the_two_run_form(self) -> None:
        code, out, _ = self.pairs(self.run_dir("older", {"alpha-a": (3, 3), "alpha-b": (3, 3)}, record=None))
        self.assertEqual(code, 0)
        self.assertIn("front door platform", out)

    def test_a_side_b_from_a_session_run_is_compared(self) -> None:
        """Side B's directive addresses the profile, so its run can be on either front door."""
        a = self.run_dir("platform", {"alpha-a": (3, 3)})
        b = self.run_dir("session", {"alpha-b": (2, 3)}, record=SESSION_RECORD)
        code, out, _ = self.pairs(a, b)
        self.assertEqual(code, 0)
        self.assertIn(f"- `{b}`: front door session", out)
        self.assertEqual(self.rows(out)["alpha-a / alpha-b"][0], "worse")

    def test_a_pairs_case_in_two_runs_is_refused(self) -> None:
        first = self.run_dir("monday", {"alpha-a": (3, 3), "alpha-b": (3, 3)})
        second = self.run_dir("tuesday", {"alpha-b": (0, 3)})
        code, out, err = self.pairs(first, second)
        self.assertEqual((code, out), (2, ""))
        self.assertIn("alpha-b has a hand-off in", err)

    def test_nothing_graded_on_both_sides_is_refused(self) -> None:
        for label, cases in (("one side", {"alpha-a": (3, 3)}), ("a side unscored", {"alpha-a": (3, 3), "alpha-b": (0, 0)}), ("other cases", {"unrelated": (3, 3)})):
            with self.subTest(label):
                code, out, err = self.pairs(self.run_dir(label.replace(" ", "-"), cases))
                self.assertEqual((code, out), (2, ""))
                self.assertIn("no pair was graded on both sides", err)

    def test_a_directory_that_is_no_run_is_refused(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        for label, directory, message in (("no hand-off", empty, "has no case-*.json"), ("not there", self.root / "absent", "is not a directory")):
            with self.subTest(label):
                code, _, err = self.pairs(directory)
                self.assertEqual(code, 2)
                self.assertIn(message, err)

    def test_a_pairs_file_that_cannot_be_read_is_refused(self) -> None:
        run = self.run_dir("night", {"alpha-a": (3, 3), "alpha-b": (3, 3)})
        for label, content in (("one case", b"alpha-a\n"), ("three cases", b"alpha-a alpha-b gamma\n"), ("no pair", b"# nothing yet\n"), ("not text", b"\xff\xfe")):
            with self.subTest(label):
                self.pairs_file.write_bytes(content)
                code, out, err = self.pairs(run)
                self.assertEqual((code, out), (2, ""))
                self.assertIn("pairs.txt", err)
        self.pairs_file.unlink()
        self.assertEqual(self.pairs(run)[0], 2)

    def test_the_length_gate_is_not_for_pairs(self) -> None:
        run = self.run_dir("night", {"alpha-a": (3, 3), "alpha-b": (3, 3)})
        with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as stop:
            ab.main(["--length-gate", "--pairs", str(self.pairs_file), str(run)])
        self.assertEqual(stop.exception.code, 2)
        self.assertIn("--length-gate", err.getvalue())


if __name__ == "__main__":
    unittest.main()
