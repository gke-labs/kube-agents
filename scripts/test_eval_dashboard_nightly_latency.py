"""nightly_latency.py: the digest's latency block -- the night line against
its typical with the spike mark, the slow cases against their own typical
with the floor, the sustained streak and the broad-slowdown cap.

The nights are night documents as ``nightly.night_reports`` returns them,
newest first, reduced to the fields the module reads. The digest wiring is
tested in test_eval_dashboard_nightly_digest.py (LatencyBlockTest).
"""

import unittest

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


if __name__ == "__main__":
    unittest.main()
