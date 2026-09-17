"""Unit tests for feedback_prompt.py, the once-per-install feedback request.

Run: python3 -m unittest agents/platform/scripts/test_feedback_prompt.py

The property that matters is "exactly once": every tick but one prints nothing,
and the one that prints claims a marker first. The clock is injected, so a
week passes in a call.
"""

import contextlib
import io
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import feedback_prompt as fp  # noqa: E402

T0 = 1_700_000_000.0
WEEK = 7 * 86400


class FeedbackPromptCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name)
        self.env = unittest.mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop(fp.ENABLED_ENV, None)
        os.environ.pop(fp.DELAY_ENV, None)

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def tick(self, now: float):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fp.main(self.home, now)
        return code, out.getvalue(), err.getvalue()

    def armed(self) -> Path:
        return self.home / fp.ARMED_MARKER

    def sent(self) -> Path:
        return self.home / fp.SENT_MARKER


class OnceOnlyTest(FeedbackPromptCase):
    def test_the_first_tick_arms_and_prints_nothing(self):
        code, out, err = self.tick(T0)
        self.assertEqual((0, "", ""), (code, out, err))
        self.assertTrue(self.armed().exists())
        self.assertFalse(self.sent().exists())
        self.assertEqual(f"{T0:.0f}\n", self.armed().read_text())

    def test_a_tick_before_the_delay_is_silent(self):
        self.tick(T0)
        code, out, _ = self.tick(T0 + WEEK - 1)
        self.assertEqual((0, ""), (code, out))
        self.assertFalse(self.sent().exists())

    def test_the_first_tick_after_the_delay_prints_once_and_the_next_is_silent(self):
        self.tick(T0)
        code, out, err = self.tick(T0 + WEEK)
        self.assertEqual(0, code)
        self.assertEqual(fp.MESSAGE, out)
        self.assertEqual("", err)
        self.assertTrue(self.sent().exists())
        code, out, _ = self.tick(T0 + WEEK + 86400)
        self.assertEqual((0, ""), (code, out))

    def test_the_anchor_is_the_first_tick_not_the_latest(self):
        # The marker is created once and never rewritten: a second tick must
        # not push the clock back.
        self.tick(T0)
        self.tick(T0 + 3 * 86400)
        self.assertEqual(f"{T0:.0f}\n", self.armed().read_text())
        _, out, _ = self.tick(T0 + WEEK)
        self.assertEqual(fp.MESSAGE, out)

    def test_a_sent_marker_left_by_another_run_keeps_this_one_silent(self):
        # The claim is what makes "once" true: a racing run that lost the
        # create prints nothing, whatever the clock says.
        self.tick(T0)
        self.sent().write_text("claimed elsewhere\n")
        code, out, _ = self.tick(T0 + WEEK)
        self.assertEqual((0, ""), (code, out))
        self.assertEqual("claimed elsewhere\n", self.sent().read_text())

    def test_a_truncated_armed_marker_anchors_on_its_mtime_rather_than_re_arming(self):
        self.armed().write_text("")
        os.utime(self.armed(), (T0, T0))
        _, out, _ = self.tick(T0 + WEEK)
        self.assertEqual(fp.MESSAGE, out)


class KnobsTest(FeedbackPromptCase):
    def test_disabled_neither_arms_nor_claims(self):
        os.environ[fp.ENABLED_ENV] = "false"
        code, out, err = self.tick(T0)
        self.assertEqual((0, "", ""), (code, out, err))
        self.assertFalse(self.armed().exists())
        self.assertFalse(self.sent().exists())

    def test_disabled_when_due_leaves_the_sent_marker_unclaimed(self):
        # Turned off after arming, then on again later: the install still gets
        # exactly one request, because the off ticks claimed nothing.
        self.tick(T0)
        os.environ[fp.ENABLED_ENV] = "false"
        _, out, _ = self.tick(T0 + WEEK)
        self.assertEqual("", out)
        self.assertFalse(self.sent().exists())
        os.environ.pop(fp.ENABLED_ENV)
        _, out, _ = self.tick(T0 + WEEK + 1)
        self.assertEqual(fp.MESSAGE, out)

    def test_only_an_explicit_false_value_disables(self):
        for value in ("false", "False", " FALSE ", "0", "no", "off"):
            self.assertFalse(fp.enabled(value), value)
        for value in (None, "", "true", "yes", "1", "flase"):
            self.assertTrue(fp.enabled(value), repr(value))

    def test_every_delay_unit_parses(self):
        self.assertEqual(3 * 86400, fp.parse_delay("3d"))
        self.assertEqual(12 * 3600, fp.parse_delay("12h"))
        self.assertEqual(2 * 60, fp.parse_delay("2m"))
        self.assertEqual(2 * 60, fp.parse_delay(" 2M "))

    def test_unset_and_malformed_delays_fall_back_to_a_week(self):
        self.assertEqual(WEEK, fp.parse_delay(None))
        self.assertEqual(WEEK, fp.parse_delay(""))
        for value in ("7", "7 days", "1w", "-1d", "1.5d", "abc"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(WEEK, fp.parse_delay(value), value)
            self.assertIn(fp.DELAY_ENV, err.getvalue())
            self.assertIn(value, err.getvalue())

    def test_the_delay_knob_moves_the_due_time(self):
        os.environ[fp.DELAY_ENV] = "2m"
        self.tick(T0)
        _, out, _ = self.tick(T0 + 119)
        self.assertEqual("", out)
        _, out, _ = self.tick(T0 + 120)
        self.assertEqual(fp.MESSAGE, out)

    def test_a_malformed_delay_is_logged_and_the_default_applies(self):
        os.environ[fp.DELAY_ENV] = "soon"
        self.tick(T0)
        _, out, err = self.tick(T0 + WEEK - 1)
        self.assertEqual("", out)
        self.assertIn("using 7d", err)
        _, out, _ = self.tick(T0 + WEEK)
        self.assertEqual(fp.MESSAGE, out)


class FailureTest(FeedbackPromptCase):
    def test_an_unwritable_home_exits_1_with_nothing_on_stdout(self):
        missing = self.home / "absent" / "profile"
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = fp.main(missing, T0)
        self.assertEqual(1, code)
        self.assertEqual("", out.getvalue())
        self.assertIn(fp.ARMED_MARKER, err.getvalue())

    def test_a_failed_claim_exits_1_and_prints_nothing(self):
        self.tick(T0)
        real_open = os.open

        def refuse_the_claim(path, flags, *args, **kwargs):
            if Path(path).name == fp.SENT_MARKER:
                raise PermissionError(13, "read-only file system", str(path))
            return real_open(path, flags, *args, **kwargs)

        with unittest.mock.patch.object(fp.os, "open", refuse_the_claim):
            code, out, err = self.tick(T0 + WEEK)
        self.assertEqual((1, ""), (code, out))
        self.assertIn(fp.SENT_MARKER, err)
        self.assertFalse(self.sent().exists())

    def test_the_home_comes_from_hermes_home(self):
        os.environ[fp.HOME_ENV] = str(self.home)
        with contextlib.redirect_stdout(io.StringIO()):
            fp.main(now=T0)
        self.assertTrue(self.armed().exists())


class MessageTest(unittest.TestCase):
    def test_the_message_opens_with_the_heading(self):
        self.assertTrue(fp.MESSAGE.startswith(f"{fp.HEADING}\n\n"))

    def test_the_message_carries_the_short_link_and_never_a_forms_url(self):
        self.assertIn("https://gke-labs.github.io/kube-agents/feedback", fp.MESSAGE)
        self.assertNotIn("docs.google.com", fp.MESSAGE)
        self.assertNotIn("forms.gle", fp.MESSAGE)

    def test_the_message_carries_the_public_issue_disclosure(self):
        self.assertIn("public issue on gke-labs/kube-agents", fp.MESSAGE)
        self.assertIn("optional follow-up email", fp.MESSAGE)

    def test_the_message_says_a_reply_reaches_the_agent(self):
        self.assertIn("reply in this thread reaches the agent", fp.MESSAGE)

    def test_the_message_does_not_name_a_duration(self):
        # The delay is a knob, so the text must not promise "a week".
        self.assertNotIn("week", fp.MESSAGE)


if __name__ == "__main__":
    unittest.main()
