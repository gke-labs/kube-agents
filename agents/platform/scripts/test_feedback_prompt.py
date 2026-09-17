"""Unit tests for feedback_prompt.py, the once-per-install feedback request.

Run: python3 -m unittest agents/platform/scripts/test_feedback_prompt.py

The property that matters is "once delivered": every tick but one prints nothing,
the one that prints claims a marker first, and a print the scheduler recorded
as undelivered is repeated until one lands. The clock and the scheduler's store
are injected, so a week passes in a call.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timezone
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
        code, out, _ = self.tick(T0 + WEEK - fp.DUE_SLACK_SECONDS - 1)
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

    def test_an_unset_delay_is_a_week_and_a_malformed_one_is_refused(self):
        self.assertEqual(WEEK, fp.parse_delay(None))
        self.assertEqual(WEEK, fp.parse_delay(""))
        for value in ("7", "7 days", "1w", "-1d", "1.5d", "abc"):
            with self.assertRaises(ValueError, msg=value) as raised:
                fp.parse_delay(value)
            self.assertIn(fp.DELAY_ENV, str(raised.exception))
            self.assertIn(value, str(raised.exception))

    def test_the_delay_knob_moves_the_due_time(self):
        os.environ[fp.DELAY_ENV] = "2m"
        self.tick(T0)
        _, out, _ = self.tick(T0 + 119)
        self.assertEqual("", out)
        _, out, _ = self.tick(T0 + 120)
        self.assertEqual(fp.MESSAGE, out)

    def test_a_malformed_delay_is_a_failed_run_that_arms_nothing(self):
        # The scheduler keeps a zero-exit script's stderr nowhere, so a silent
        # fallback would leave no trace of the rejected value. A failed run is
        # reported in chat like any other script failure, and the clock does
        # not start until the value parses.
        os.environ[fp.DELAY_ENV] = "soon"
        code, out, err = self.tick(T0)
        self.assertEqual((1, ""), (code, out))
        self.assertIn("'soon'", err)
        self.assertFalse(self.armed().exists())
        os.environ[fp.DELAY_ENV] = "2m"
        code, out, _ = self.tick(T0)
        self.assertEqual((0, ""), (code, out))
        self.assertTrue(self.armed().exists())

    def test_a_week_is_due_a_few_minutes_early_but_not_a_day_early(self):
        # The daily tick drifts by seconds from one day to the next, so a
        # strict comparison could read seven days as seven days less a few
        # seconds and land on day eight.
        self.tick(T0)
        _, out, _ = self.tick(T0 + WEEK - fp.DUE_SLACK_SECONDS - 1)
        self.assertEqual("", out)
        _, out, _ = self.tick(T0 + WEEK - fp.DUE_SLACK_SECONDS)
        self.assertEqual(fp.MESSAGE, out)

    def test_a_delay_under_a_day_gets_no_slack(self):
        # A hand-marked run with a two-minute delay is how the mechanism is
        # observed; two minutes has to mean two minutes there.
        self.assertFalse(fp.due(T0, T0 + 119, 120))
        self.assertTrue(fp.due(T0, T0 + 120, 120))
        self.assertFalse(fp.due(T0, T0 + 86400 - fp.DUE_SLACK_SECONDS - 1, 86400))
        self.assertTrue(fp.due(T0, T0 + 86400 - fp.DUE_SLACK_SECONDS, 86400))


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

    def test_a_marker_whose_payload_failed_to_write_is_not_left_behind(self):
        # The create and the write are two operations. An empty sent marker
        # left by a failed write would read on every later tick as a claim no
        # run can retry, and the prompt would be lost for good; the failed run
        # exits 1 (reported in chat) and the next tick claims afresh.
        self.tick(T0)
        refuse = unittest.mock.Mock(side_effect=OSError(28, "No space left on device"))
        with unittest.mock.patch.object(fp.os, "write", refuse):
            code, out, err = self.tick(T0 + WEEK)
        self.assertEqual((1, ""), (code, out))
        self.assertIn(fp.SENT_MARKER, err)
        self.assertFalse(self.sent().exists())
        _, out, _ = self.tick(T0 + WEEK + 86400)
        self.assertEqual(fp.MESSAGE, out)

    def test_a_short_write_of_a_marker_is_a_failure_that_leaves_nothing_behind(self):
        # A partial payload ("17896") would parse as a time and anchor or claim
        # wrongly, so it is treated like a refused write.
        short = unittest.mock.Mock(side_effect=lambda fd, data: len(data) - 1)
        with unittest.mock.patch.object(fp.os, "write", short):
            code, out, err = self.tick(T0)
        self.assertEqual((1, ""), (code, out))
        self.assertIn(fp.ARMED_MARKER, err)
        self.assertFalse(self.armed().exists())
        code, out, _ = self.tick(T0)
        self.assertEqual((0, ""), (code, out))
        self.assertTrue(self.armed().exists())

    def test_the_home_comes_from_hermes_home(self):
        os.environ[fp.HOME_ENV] = str(self.home)
        with contextlib.redirect_stdout(io.StringIO()):
            fp.main(now=T0)
        self.assertTrue(self.armed().exists())

    def test_the_default_home_is_the_profile_home_the_ticker_uses(self):
        # `profile_cron_tick.py` runs this roster with HERMES_HOME set to the
        # platform profile's home; a hand run in the container without that
        # environment must find the same markers, not start a second pair
        # under the gateway's `/opt/data`.
        self.assertEqual("/opt/data/profiles/platform", fp.DEFAULT_HOME)


RELAY_502 = (
    "delivery error: chat relay answered HTTP 502: chat relay failed: "
    "composed but not delivered to google_chat (target chat:cron-reports)"
)


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat()


class RetryTest(FeedbackPromptCase):
    """Printing is not delivering: a recorded hard failure is retried, a landing is not."""

    def setUp(self):
        super().setUp()
        self.tick(T0)
        _, out, _ = self.tick(T0 + WEEK)
        self.assertEqual(fp.MESSAGE, out)
        self.assertEqual(fp.claim_record(T0 + WEEK, 1), self.sent().read_text())

    def record(self, run_at: float, error: str | None, job_id: str = fp.JOB_ID, status: str = "ok"):
        """What the scheduler leaves in the profile store after the run at ``run_at``."""
        store = self.home / fp.STORE_PATH
        store.parent.mkdir(parents=True, exist_ok=True)
        entry = {"id": job_id, "last_run_at": iso(run_at), "last_status": status, "last_delivery_error": error}
        store.write_text(json.dumps({"jobs": [{"id": "other-job", "last_delivery_error": RELAY_502}, entry]}))

    def test_a_recorded_hard_failure_is_retried_on_the_next_tick(self):
        # The scheduler stamps last_run_at after delivery, so the record of the
        # run that carried the attempt sits after the attempt time.
        self.record(T0 + WEEK + 77, RELAY_502)
        code, out, err = self.tick(T0 + WEEK + 86400)
        self.assertEqual((0, fp.MESSAGE, ""), (code, out, err))
        self.assertEqual(fp.claim_record(T0 + WEEK + 86400, 2), self.sent().read_text())

    def test_a_delivered_attempt_is_never_repeated(self):
        self.record(T0 + WEEK + 77, None)
        for day in (1, 2, 30):
            _, out, _ = self.tick(T0 + WEEK + day * 86400)
            self.assertEqual("", out, day)
        self.assertEqual(fp.claim_record(T0 + WEEK, 1), self.sent().read_text())

    def test_a_note_a_partial_or_a_degraded_delivery_landed_somewhere_and_is_not_retried(self):
        for error in (
            "delivered without thread_id",
            "chat relay partial: the report did not reach slack.",
            "chat relay degraded: the Chat Agent turn failed after posting",
        ):
            self.record(T0 + WEEK + 77, error)
            _, out, _ = self.tick(T0 + WEEK + 86400)
            self.assertEqual("", out, error)

    def test_a_record_that_predates_the_attempt_is_not_evidence(self):
        # The scheduler never recorded the run that printed (a crash between
        # the run and the stamp): unknown, and unknown means no second post.
        self.record(T0 + WEEK - 5, RELAY_502)
        _, out, _ = self.tick(T0 + WEEK + 86400)
        self.assertEqual("", out)

    def test_no_store_no_job_or_an_unreadable_store_ends_the_retries(self):
        _, out, _ = self.tick(T0 + WEEK + 86400)
        self.assertEqual("", out)
        self.record(T0 + WEEK + 77, RELAY_502, job_id="not-this-job")
        _, out, _ = self.tick(T0 + WEEK + 86400)
        self.assertEqual("", out)
        (self.home / fp.STORE_PATH).write_text("{not json")
        _, out, _ = self.tick(T0 + WEEK + 86400)
        self.assertEqual("", out)

    def test_a_failed_runs_relayed_summary_is_not_the_attempts_record(self):
        # Day 7 lands. Later a run of this job fails (a malformed delay) and
        # its failure summary is relayed on a day the relay is down: same
        # fields, a hard error, a later stamp. That is not the attempt's
        # record, and the message must not go a second time.
        self.record(T0 + WEEK + 77, None)
        self.record(T0 + WEEK + 3 * 86400, RELAY_502, status="error")
        _, out, _ = self.tick(T0 + WEEK + 4 * 86400)
        self.assertEqual("", out)
        self.assertEqual(fp.claim_record(T0 + WEEK, 1), self.sent().read_text())

    def test_retries_have_no_cap(self):
        # A capped job goes silent for good at the cap, and a silent run never
        # clears the streak chat_delivery_watch keeps, so the ledger issue would
        # name this job for the life of the install. Retried until one lands,
        # the landing clears it like any other job's delivery.
        attempted_at = T0 + WEEK
        for attempt in range(2, 31):
            self.record(attempted_at + 77, RELAY_502)
            attempted_at += 86400
            _, out, _ = self.tick(attempted_at)
            self.assertEqual(fp.MESSAGE, out, attempt)
            self.assertEqual(fp.claim_record(attempted_at, attempt), self.sent().read_text())
        self.record(attempted_at + 77, None)
        _, out, _ = self.tick(attempted_at + 86400)
        self.assertEqual("", out)

    def test_a_racing_run_after_a_retry_sees_no_record_for_it_and_stays_silent(self):
        self.record(T0 + WEEK + 77, RELAY_502)
        retry_at = T0 + WEEK + 86400
        _, out, _ = self.tick(retry_at)
        self.assertEqual(fp.MESSAGE, out)
        # Same store, same second: the retry's own run is not recorded yet.
        _, out, _ = self.tick(retry_at)
        self.assertEqual("", out)
        self.assertEqual(fp.claim_record(retry_at, 2), self.sent().read_text())

    def test_a_marker_written_before_retries_existed_counts_as_one_attempt(self):
        self.sent().write_text(f"{T0 + WEEK:.0f}\n")
        self.record(T0 + WEEK + 77, RELAY_502)
        _, out, _ = self.tick(T0 + WEEK + 86400)
        self.assertEqual(fp.MESSAGE, out)
        self.assertEqual(fp.claim_record(T0 + WEEK + 86400, 2), self.sent().read_text())

    def test_the_claim_record_round_trips(self):
        self.assertEqual((T0, 3), fp.parse_claim(fp.claim_record(T0, 3)))
        self.assertEqual((T0, 1), fp.parse_claim(f"{T0:.0f}\n"))
        self.assertIsNone(fp.parse_claim("claimed elsewhere\n"))
        self.assertIsNone(fp.parse_claim(""))


class StampTest(FeedbackPromptCase):
    """The markers hold whole seconds, floored: a rounded-up second would outrun the record."""

    def test_a_stamp_is_the_second_the_time_fell_in(self):
        self.assertEqual("1789615518", fp.stamp(1789615518.630110))
        self.assertEqual("1789615518", fp.stamp(1789615518.0))

    def test_a_stamp_that_follows_the_attempt_within_the_second_is_its_record(self):
        # Seen on the robot install: the script's clock read 18.63, the
        # scheduler's stamp 18.78. With no adapter to run (no chat platform
        # bound) the gap is that small every time, and a marker rounded to 19
        # would read that stamp as an earlier run and never retry.
        self.tick(T0)
        attempted_at = T0 + WEEK + 0.63
        _, out, _ = self.tick(attempted_at)
        self.assertEqual(fp.MESSAGE, out)
        self.assertEqual(f"{T0 + WEEK:.0f}\n1\n", self.sent().read_text())
        store = self.home / fp.STORE_PATH
        store.parent.mkdir(parents=True, exist_ok=True)
        entry = {"id": fp.JOB_ID, "last_run_at": iso(attempted_at + 0.157), "last_status": "ok", "last_delivery_error": RELAY_502}
        store.write_text(json.dumps({"jobs": [entry]}))
        _, out, _ = self.tick(T0 + WEEK + 86400)
        self.assertEqual(fp.MESSAGE, out)


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
