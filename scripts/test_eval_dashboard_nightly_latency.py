"""nightly_latency.py: the digest's latency block -- the night line against
its typical with the spike mark, the slow cases against their own typical
with the floor, the sustained streak and the broad-slowdown cap.

The nights are night documents as ``nightly.night_reports`` returns them,
newest first, reduced to the fields the module reads. The digest wiring is
tested in test_eval_dashboard_nightly_digest.py (LatencyBlockTest).
"""

import unittest

from eval_dashboard import nightly
from eval_dashboard import nightly_latency as nl


def case(name, agent=None, domain="capacity"):
    return {"case": name, "domain": domain, "agent_p50_s": agent}


def night(cases=(), p50=None, p95=None, truncated=False, missing=(), running=()):
    return {"cases": list(cases), "turn1_p50_s": p50, "turn1_p95_s": p95, "truncated": truncated,
            "missing_parts": list(missing), "running_parts": list(running)}


def steady(count, agent=100.0, p50=10.0, p95=20.0, names=("a", "b", "c", "d")):
    """``count`` ordinary nights, each case at ``agent`` seconds."""
    return [night([case(n, agent) for n in names], p50, p95) for _ in range(count)]


class NightLineTest(unittest.TestCase):
    def test_no_timing_last_night_is_no_line(self):
        self.assertIsNone(nl.night_line([night([case("a")])] + steady(7)))
        self.assertIsNone(nl.night_line([]))

    def test_an_ordinary_night_against_its_typical(self):
        line = nl.night_line(steady(1, agent=110.0, p50=9.5, p95=24.4) + steady(7))
        self.assertEqual(line, "⏱ *Latency*: turn 1 p50 9.5 s, p95 24.4 s (target p95 < 10 s; typical 10.0 s, 20.0 s) · agent time, median case 110 s (typical 100 s)")

    def test_a_number_at_one_and_a_half_times_its_typical_is_marked(self):
        line = nl.night_line(steady(1, agent=150.0, p50=10.0, p95=60.0) + steady(7))
        self.assertIn("p95 60.0 s 🐢 3.0×", line)
        self.assertIn("median case 150 s 🐢 1.5× (typical 100 s)", line)
        self.assertNotIn("p50 10.0 s 🐢", line)

    def test_fewer_than_three_earlier_nights_give_no_typical(self):
        line = nl.night_line(steady(1, agent=300.0, p50=10.0, p95=60.0) + steady(2))
        self.assertEqual(line, "⏱ *Latency*: turn 1 p50 10.0 s, p95 60.0 s (target p95 < 10 s) · agent time, median case 300 s")

    def test_nights_cut_short_are_left_out_of_the_typical(self):
        slow_cut = [night([case("a", 900.0)], 90.0, 900.0, truncated=True) for _ in range(5)]
        line = nl.night_line(steady(1) + slow_cut + steady(3))
        self.assertIn("typical 10.0 s, 20.0 s", line)
        self.assertIn("(typical 100 s)", line)

    def test_partial_nights_are_left_out_of_the_typical_and_labelled(self):
        half = [night([case("a", 900.0)], 90.0, 900.0, missing=["main-b"]) for _ in range(3)]
        half += [night([case("a", 900.0)], 90.0, 900.0, running=["writers"]) for _ in range(2)]
        line = nl.night_line(steady(1) + half + steady(3))
        self.assertIn("typical 10.0 s, 20.0 s", line)
        self.assertIn("(typical 100 s)", line)
        self.assertFalse(line.endswith("(partial: over the cases on record)"))
        last = night([case("a", 120.0)], 10.0, 20.0, missing=["main-b"])
        self.assertTrue(nl.night_line([last] + steady(7)).endswith("(partial: over the cases on record)"))

    def test_a_part_cut_short_leaves_the_night_partial(self):
        writers_cut = {**night([case("a", 900.0)], 90.0, 900.0), "parts": [{"part": "main", "truncated": False}, {"part": "writers", "truncated": True}]}
        self.assertFalse(nl.whole(writers_cut))
        line = nl.night_line(steady(1) + [writers_cut] * 4 + steady(3))
        self.assertIn("(typical 100 s)", line)
        self.assertTrue(nl.night_line([writers_cut] + steady(7)).endswith("(partial: over the cases on record)"))

    def test_only_the_newest_seven_earlier_nights_count(self):
        line = nl.night_line(steady(1) + steady(7, agent=100.0) + steady(10, agent=1000.0))
        self.assertIn("(typical 100 s)", line)

    def test_a_night_cut_short_says_so(self):
        line = nl.night_line([night([case("a", 120.0)], 10.0, 20.0, truncated=True)] + steady(7))
        self.assertTrue(line.endswith("(typical 100 s) (cut short: over the cases it ran)"))

    def test_turn_one_not_timed_still_gives_the_agent_time(self):
        line = nl.night_line([night([case("a", 120.0)])] + steady(7))
        self.assertEqual(line, "⏱ *Latency*: turn 1 not timed · agent time, median case 120 s (typical 100 s)")


class SlowCasesTest(unittest.TestCase):
    def test_twice_the_typical_and_sixty_seconds_more_is_slow(self):
        last = night([case("a", 200.0), case("b", 199.0), case("c", 100.0), case("d", 100.0)])
        slow, timed = nl.slow_cases([last] + steady(7))
        self.assertEqual([s["case"] for s in slow], ["a"])
        self.assertEqual(timed, 4)
        self.assertAlmostEqual(slow[0]["ratio"], 2.0)

    def test_the_floor_keeps_a_fast_case_quiet(self):
        # 10 s to 60 s is 6x but only 50 s more.
        nights = [night([case("a", 60.0)])] + [night([case("a", 10.0)]) for _ in range(7)]
        self.assertEqual(nl.slow_cases(nights), ([], 1))

    def test_keep_limits_the_cases_judged_and_counted(self):
        last = night([case("a", 400.0), case("b", 400.0), case("c", 100.0), case("d", 100.0)])
        slow, timed = nl.slow_cases([last] + steady(7), keep={"b", "c"})
        self.assertEqual(([s["case"] for s in slow], timed), (["b"], 2))

    def test_a_case_with_fewer_than_three_nights_has_no_typical(self):
        nights = [night([case("new", 900.0)]), night([case("new", 10.0)]), night([case("new", 10.0)])]
        self.assertEqual(nl.slow_cases(nights), ([], 0))

    def test_a_night_cut_short_still_counts_for_a_case(self):
        nights = [night([case("a", 500.0)])] + [night([case("a", 100.0)], truncated=True) for _ in range(3)]
        slow, _ = nl.slow_cases(nights)
        self.assertEqual([s["case"] for s in slow], ["a"])

    def test_largest_ratio_first(self):
        last = night([case("a", 300.0), case("b", 500.0), case("c", 100.0), case("d", 100.0), case("e", 100.0)])
        slow, _ = nl.slow_cases([last] + steady(7, names=("a", "b", "c", "d", "e")))
        self.assertEqual([s["case"] for s in slow], ["b", "a"])

    def test_sustained_counts_the_slow_nights_in_a_row(self):
        names = ("a", "b", "c", "d", "e", "f", "g", "h")
        older = steady(7, names=names)
        # "a" slow on the last three nights, "b" on the last one only.
        def slow_night(slow_names):
            return night([case(n, 400.0 if n in slow_names else 100.0) for n in names])
        nights = [slow_night({"a", "b"}), slow_night({"a"}), slow_night({"a"})] + older
        slow, _ = nl.slow_cases(nights)
        by_case = {s["case"]: s for s in slow}
        self.assertEqual({name: s["sustained"] for name, s in by_case.items()}, {"a": 3, "b": 1})
        self.assertIn(", sustained 3 nights", nl.slow_text(by_case["a"], "Capacity", "@x"))
        self.assertNotIn("sustained", nl.slow_text(by_case["b"], "Capacity", "@x"))

    def test_broad_above_a_quarter_of_the_timed_cases_and_more_than_the_list_holds(self):
        self.assertTrue(nl.is_broad([{}] * 6, 20, 5))
        self.assertFalse(nl.is_broad([{}] * 6, 24, 5))
        # A quarter of a small night is still named, case by case.
        self.assertFalse(nl.is_broad([{}] * 5, 6, 5))
        self.assertFalse(nl.is_broad([], 0, 5))

    def test_slow_text(self):
        entry = {"case": "a", "value": 428.0, "typical": 18.0, "ratio": 428.0 / 18.0, "sustained": 3}
        self.assertEqual(nl.slow_text(entry, "Observability", "@owner"), "Observability: a 18 s → 428 s (23.8×), sustained 3 nights · @owner")


def dated(nights, first_day=28, hour=2):
    """``nights`` (newest first) with a ``started`` stamp a date apart, the
    newest on 2026-10-``first_day``."""
    return [{**n, "started": f"2026-10-{first_day - i:02d}T{hour:02d}:00:00+00:00"} for i, n in enumerate(nights)]


def rerun(n, day, hour):
    """``n`` as a run started on 2026-10-``day`` at ``hour``."""
    return {**n, "started": f"2026-10-{day:02d}T{hour:02d}:00:00+00:00"}


class OneRunPerDateTest(unittest.TestCase):
    """A same-date re-run of the main nightly job is a night of its own in
    ``night_reports``; the latency block counts its date once, by the newest
    run, as ``nightly_bugs.graded_nights`` does."""

    def test_the_newest_run_of_a_date_is_kept(self):
        older, newer = rerun(night(p50=1.0), 27, 2), rerun(night(p50=2.0), 27, 9)
        undated = night(p50=3.0)
        self.assertEqual(nl.one_per_date([newer, older, undated]), [newer, undated])

    def test_a_same_date_re_run_is_not_a_second_sustained_night(self):
        names = ("a", "b", "c", "d")
        slow = night([case(n, 400.0 if n == "a" else 100.0) for n in names])
        nights = [rerun(slow, 28, 9), rerun(slow, 28, 2)] + dated(steady(7, names=names), first_day=27)
        slow_entries, _ = nl.slow_cases(nights)
        self.assertEqual([(s["case"], s["sustained"]) for s in slow_entries], [("a", 1)])

    def test_a_case_typical_spans_seven_dates(self):
        # Seven earlier dates: 27 run twice at 100 s, 26 and 25 at 100 s,
        # 24 to 21 at 1000 s. Counted per date the typical is 1000 s; counted
        # per run, 27's re-run pushes 21 out and the typical reads 100 s.
        earlier = [rerun(night([case("a", 100.0)]), 27, 9), rerun(night([case("a", 100.0)]), 27, 2)]
        earlier += dated([night([case("a", 100.0)])] * 2, first_day=26)
        earlier += dated([night([case("a", 1000.0)])] * 4, first_day=24)
        slow, _ = nl.slow_cases([rerun(night([case("a", 2500.0)]), 28, 2)] + earlier)
        self.assertEqual([(s["case"], s["typical"]) for s in slow], [("a", 1000.0)])

    def test_the_night_typical_counts_a_date_once(self):
        # The same shape for the night line: counted per run, 27's two whole
        # runs give a typical turn 1 p50 of 10 s; per date, 40 s.
        earlier = [rerun(night([case("a", 100.0)], 10.0, 20.0), 27, 9), rerun(night([case("a", 100.0)], 10.0, 20.0), 27, 2)]
        earlier += dated([night([case("a", 100.0)], 10.0, 20.0)] * 2, first_day=26)
        earlier += dated([night([case("a", 100.0)], 40.0, 20.0)] * 4, first_day=24)
        line = nl.night_line([rerun(night([case("a", 100.0)], 10.0, 20.0), 28, 2)] + earlier)
        self.assertIn("typical 40.0 s, 20.0 s", line)

    def test_a_re_run_through_night_reports(self):
        """The real path: runs, then ``night_reports``, then the block. A cron
        run and a 09:00 re-run of the main job on 10-05 both time case-a slow."""
        def run(build, started, agent):
            reps = [{"n": i, "result": "pass", "agent_s": agent, "turn1_s": 10.0} for i in (1, 2, 3)]
            return {"build_id": build, "tier": "nightly", "job": "ci-kube-agents-eval-nightly", "pr": None,
                    "head_sha": build[-7:], "started": started, "finished": started.replace("T00", "T06").replace("T09", "T15"),
                    "result": "SUCCESS", "duration_s": 21600, "tasks": [{"name": "case-a", "result": "pass", "reps": reps}]}
        runs = [run(f"300000000000000000{d}", f"2026-10-0{d}T00:00:00+00:00", 100.0) for d in (1, 2, 3, 4)]
        runs += [run("3000000000000000005", "2026-10-05T00:00:00+00:00", 400.0),
                 run("3000000000000000006", "2026-10-05T09:00:00+00:00", 400.0)]
        data = {"schema_version": 1, "generated_at": "2026-10-05T16:00:00+00:00",
                "cases": [{"name": "case-a", "domain": "cost", "active": True, "nightly_active": True}], "runs": runs}
        nights = nightly.night_reports(data)
        self.assertEqual([n["build"] for n in nights[:2]], ["3000000000000000006", "3000000000000000005"], "the re-run is a night of its own")
        slow, _ = nl.slow_cases(nights)
        self.assertEqual([(s["case"], s["sustained"], s["typical"]) for s in slow], [("case-a", 1, 100.0)])


if __name__ == "__main__":
    unittest.main()
