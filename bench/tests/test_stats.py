"""The exact small-sample statistics behind ``bench-run``."""

from __future__ import annotations

import math
import unittest

from kube_agents_bench import stats


class WilsonTest(unittest.TestCase):
    def test_three_of_three_is_not_a_point(self):
        interval = stats.wilson_interval(3, 3)
        self.assertEqual(interval.rate, 1.0)
        self.assertAlmostEqual(interval.low, 0.4385, places=3)
        self.assertEqual(interval.high, 1.0)

    def test_zero_of_three_mirrors_it(self):
        interval = stats.wilson_interval(0, 3)
        self.assertEqual(interval.low, 0.0)
        self.assertAlmostEqual(interval.high, 0.5615, places=3)

    def test_no_data_is_the_unit_interval(self):
        interval = stats.wilson_interval(0, 0)
        self.assertIsNone(interval.rate)
        self.assertEqual((interval.low, interval.high), (0.0, 1.0))

    def test_impossible_counts_are_refused(self):
        with self.assertRaises(ValueError):
            stats.wilson_interval(4, 3)


class FisherTest(unittest.TestCase):
    def test_the_tea_tasting_table(self):
        # Fisher's own example: 3/4 vs 1/4, two-sided p = 0.4857.
        self.assertAlmostEqual(stats.fisher_exact(3, 1, 1, 3), 0.4857, places=4)

    def test_agrees_with_the_hypergeometric_on_a_large_baseline(self):
        # 0/3 against 783/900. The observed table is the least likely one
        # (three failures drawn from 903 with 120 failures among them), so
        # the two-sided p is exactly that hypergeometric term.
        expected = math.comb(120, 3) / math.comb(903, 3)
        self.assertAlmostEqual(stats.fisher_exact(0, 3, 783, 117), expected, places=9)

    def test_three_passes_against_a_high_baseline_prove_nothing(self):
        # The presubmit's happy path: 3/3 on a case that passes 87% of the
        # time on main is exactly what main would produce.
        self.assertGreater(stats.fisher_exact(3, 0, 783, 117), 0.5)

    def test_symmetric_in_the_groups(self):
        self.assertAlmostEqual(stats.fisher_exact(2, 5, 9, 1), stats.fisher_exact(9, 1, 2, 5))

    def test_an_empty_group_is_no_evidence(self):
        self.assertEqual(stats.fisher_exact(0, 0, 5, 5), 1.0)

    def test_negative_counts_are_refused(self):
        with self.assertRaises(ValueError):
            stats.fisher_exact(-1, 1, 1, 1)


class PlanningTest(unittest.TestCase):
    def test_more_effect_needs_fewer_reps(self):
        small = stats.reps_to_detect(0.7, 400, 800)  # 0.5 -> 0.7
        large = stats.reps_to_detect(0.9, 400, 800)  # 0.5 -> 0.9
        self.assertIsNotNone(small)
        self.assertIsNotNone(large)
        self.assertLess(large, small)

    def test_a_silent_case_shows_any_pass_quickly(self):
        # A case at 0/692 on main: three passes are already significant.
        self.assertLessEqual(stats.reps_to_detect(1.0, 0, 692), 3)

    def test_no_baseline_means_no_plan(self):
        self.assertIsNone(stats.reps_to_detect(0.9, 0, 0))

    def test_an_effect_too_small_to_see_returns_none(self):
        self.assertIsNone(stats.reps_to_detect(0.51, 400, 800, max_reps=10))

    def test_power_is_a_probability(self):
        for n in (1, 3, 10):
            value = stats.power_at(n, 0.9, 400, 800)
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)
        self.assertEqual(stats.power_at(0, 0.9, 400, 800), 0.0)


class FutilityTest(unittest.TestCase):
    def test_a_decided_case_can_still_decide(self):
        self.assertTrue(stats.can_still_decide(3, 3, 3, 0, 692))

    def test_three_more_runs_against_a_close_baseline_cannot(self):
        # 1/3 so far on a case at 0.55: nothing in three more runs reaches 0.05.
        self.assertFalse(stats.can_still_decide(1, 3, 6, 459, 834))

    def test_enough_remaining_runs_can(self):
        self.assertTrue(stats.can_still_decide(1, 3, 40, 459, 834))

    def test_finite_everywhere(self):
        for k in range(0, 4):
            self.assertTrue(math.isfinite(stats.fisher_exact(k, 3 - k, 783, 117)))


if __name__ == "__main__":
    unittest.main()
