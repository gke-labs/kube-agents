"""nightly_tone.py: the digest's reply-shape line -- last night's share of
answers opening on the verdict, answer length, message count and apologies,
each against its typical with the ⚠ mark past the threshold.

The nights are night documents as ``nightly.night_reports`` returns them,
newest first, reduced to the fields the module reads. The digest wiring is
tested in test_eval_dashboard_nightly_digest.py (ToneBlockTest).
"""

import unittest

from eval_dashboard import nightly_tone as nt


def night(n=40, verdict=36, apology=0, words=60, messages=2, truncated=False, missing=()):
    shape = None if n is None else {"n": n, "opens_on_verdict": verdict, "apology": apology,
                                    "bot_messages_p50": messages, "words_before_answer_p50": 0, "answer_words_p50": words}
    return {"shape": shape, "truncated": truncated, "missing_parts": list(missing), "running_parts": [], "parts": []}


def steady(count, **fields):
    return [night(**fields) for _ in range(count)]


class NightLineTest(unittest.TestCase):
    def test_no_reply_measured_last_night_is_no_line(self):
        self.assertIsNone(nt.night_line([night(n=None)] + steady(7)))
        self.assertIsNone(nt.night_line([night(n=0)] + steady(7)))
        self.assertIsNone(nt.night_line([]))

    def test_an_ordinary_night_against_its_typical(self):
        line = nt.night_line([night(verdict=35, words=62)] + steady(7))
        self.assertEqual(line, "🗣 *Reply shape*: 88% of 40 reps open on the answer (typical 90%) · answer p50 62 words (typical 60)"
                               " · messages p50 2 (typical 2) · 0% apologize (typical 0%)")
        self.assertEqual(nt.regressions([night(verdict=35, words=62)] + steady(7)), [])

    def test_each_number_past_its_threshold_is_marked(self):
        nights = [night(verdict=30, apology=4, words=90, messages=3)] + steady(7)
        self.assertEqual(nt.regressions(nights), [nt.VERDICT_SHARE, nt.APOLOGY_SHARE, nt.ANSWER_P50, nt.MESSAGES_P50])
        line = nt.night_line(nights)
        self.assertIn("75% of 40 reps open on the answer ⚠ (typical 90%)", line)
        self.assertIn("answer p50 90 words ⚠ (typical 60)", line)
        self.assertIn("messages p50 3 ⚠ (typical 2)", line)
        self.assertIn("10% apologize ⚠ (typical 0%)", line)

    def test_an_exact_rise_from_a_nonzero_typical_is_marked(self):
        # 0.2 + 0.1 is 0.30000000000000004 in floats; the points are whole.
        nights = [night(n=10, verdict=9, apology=3)] + steady(7, n=10, verdict=9, apology=2)
        self.assertEqual(nt.regressions(nights), [nt.APOLOGY_SHARE])

    def test_the_verdict_share_mark_at_and_just_short_of_five_points(self):
        self.assertEqual(nt.regressions([night(verdict=34)] + steady(7)), [nt.VERDICT_SHARE])
        self.assertEqual(nt.regressions([night(verdict=35)] + steady(7)), [])

    def test_a_half_point_rounds_up_as_the_page_does(self):
        self.assertIn("13% of 40 reps", nt.night_line([night(verdict=5)] + steady(2)))

    def test_a_count_is_judged_against_its_typical_as_printed(self):
        # Six baseline nights at 2, 2, 2, 3, 3, 3 messages: the median 2.5
        # prints as 3, so 3 messages tonight is no regression, and 5 is.
        base = steady(3, messages=2) + steady(3, messages=3)
        self.assertIn("messages p50 3 (typical 3)", nt.night_line([night(messages=3)] + base))
        self.assertEqual(nt.regressions([night(messages=4)] + base), [])
        self.assertEqual(nt.regressions([night(messages=5)] + base), [nt.MESSAGES_P50])

    def test_just_short_of_the_threshold_is_not_marked(self):
        nights = [night(verdict=35, apology=1, words=89)] + steady(7)
        self.assertEqual(nt.regressions(nights), [])

    def test_fewer_than_three_earlier_nights_give_no_typical(self):
        nights = [night(verdict=10, words=500)] + steady(2)
        self.assertEqual(nt.regressions(nights), [])
        self.assertEqual(nt.night_line(nights), "🗣 *Reply shape*: 25% of 40 reps open on the answer · answer p50 500 words"
                                                " · messages p50 2 · 0% apologize")

    def test_nights_cut_short_or_partial_are_left_out_of_the_typical_and_labelled(self):
        bad = steady(3, verdict=4, words=900, truncated=True) + steady(2, verdict=4, words=900, missing=["main-b"])
        line = nt.night_line([night()] + bad + steady(3))
        self.assertIn("(typical 90%)", line)
        self.assertIn("(typical 60)", line)
        self.assertTrue(nt.night_line([night(truncated=True)] + steady(3)).endswith("(cut short: over the cases it ran)"))
        self.assertTrue(nt.night_line([night(missing=["writers"])] + steady(3)).endswith("(partial: over the cases on record)"))

    def test_a_night_with_no_shape_does_not_enter_the_typical(self):
        line = nt.night_line([night()] + [night(n=None)] * 5 + steady(2))
        self.assertNotIn("typical", line)

    def test_a_count_the_night_did_not_measure_is_left_out(self):
        last = night()
        last["shape"]["answer_words_p50"] = None
        line = nt.night_line([last] + steady(7))
        self.assertNotIn("answer p50", line)
        self.assertIn("messages p50 2", line)


def focused(chat=(30, 0), other=(400, 0), chat_n=36, other_n=440, **fields):
    """A night whose cases split into the focus domain and the rest, each a
    single case rollup ``(opens_on_verdict, apology)`` out of ``*_n``."""
    def case(domain, n, counts):
        return {"case": domain, "domain": domain, "shape": {"n": n, "opens_on_verdict": counts[0], "apology": counts[1]}}

    out = night(n=chat_n + other_n, verdict=chat[0] + other[0], apology=chat[1] + other[1], **fields)
    out["cases"] = [case(nt.FOCUS_DOMAIN, chat_n, chat), case("capacity", other_n, other)]
    return out


class FocusLineTest(unittest.TestCase):
    def test_shares_only_over_the_focus_domain(self):
        line = nt.focus_line([focused()] + [focused() for _ in range(7)])
        self.assertEqual(line, "🗣 *Reply shape, chat and routing*: 83% of 36 reps open on the answer (typical 83%) · 0% apologize (typical 0%)")

    def test_a_drop_confined_to_the_focus_domain_is_marked_there_and_not_on_the_whole_line(self):
        nights = [focused(chat=(26, 0))] + [focused() for _ in range(7)]
        self.assertEqual(nt.regressions(nights, nt.focus_shape, nt.FOCUS_DROP_POINTS), [nt.VERDICT_SHARE])
        self.assertIn("72% of 36 reps open on the answer ⚠", nt.focus_line(nights))
        self.assertEqual(nt.regressions(nights), [], "four reps of 476 is under a point")

    def test_no_focus_case_measured_is_no_line(self):
        last = night()
        last["cases"] = [{"case": "x", "domain": "capacity", "shape": {"n": 3, "opens_on_verdict": 3, "apology": 0}}]
        self.assertIsNone(nt.focus_line([last] + steady(3)))



class OneRunPerDateTest(unittest.TestCase):
    """A same-date re-run counts once in the reply-shape typicals, as in the
    latency line's (nightly_latency.one_per_date)."""

    def test_a_same_date_re_run_is_one_earlier_night(self):
        # Seven earlier dates: the 27th (run twice), 26th and 25th at 90%
        # opening on the answer, the 24th to the 21st at 50%. Counted per
        # date the typical is the median of three 90s and four 50s, 50%;
        # counted per run, the 27th's second run pushes the 21st out and it
        # reads 90%.
        def at(day, hour, **fields):
            return {**night(**fields), "started": f"2026-10-{day:02d}T{hour:02d}:00:00+00:00"}
        earlier = [at(27, 9, verdict=36), at(27, 2, verdict=36), at(26, 2, verdict=36), at(25, 2, verdict=36)]
        earlier += [at(24 - i, 2, verdict=20) for i in range(4)]
        line = nt.night_line([at(28, 2, verdict=20)] + earlier)
        self.assertIn("(typical 50%)", line)

if __name__ == "__main__":
    unittest.main()
