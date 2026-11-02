"""weekly_chat.py posts the weekly health summary once a week: on the first
tick from Monday 11 AM Toronto (15:00Z in October, 16:00Z in winter), never
twice for one week, never while muted, and never for a week the email
refused (no summary written).

The HTTP layer is a recording fake handed in as `opener`, and the gs://
state object a recording `runner`; nothing opens a socket or touches a bucket.
"""

import contextlib
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error

from eval_dashboard import post_health
from eval_dashboard import weekly_chat as wc

SPACE = "spaces/AAAAtestspace"
TOKEN = "ya29.weekly-token-value"
ENV = {post_health.SPACE_ENV: SPACE, post_health.TOKEN_ENV: TOKEN}
WEEK_END = "2026-10-25"
TEXT = "*kube-agents weekly health · Oct 19–25*\n83% of nightly test runs passed (767 of 924) across 103 scenarios."
PLAIN = "kube-agents weekly health · Oct 19–25\n83% of nightly test runs passed (767 of 924) across 103 scenarios."
CARD = {"cardId": "weekly-health", "card": {"header": {"title": "kube-agents · weekly health"}, "sections": []}}
# Monday 2026-10-26, 11:00 AM EDT.
DUE = "2026-10-26T15:00:00Z"
GS_STATE = "gs://bucket/evals/weekly-chat-state.json"


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    def __init__(self, fail=False, refuse_cards=False):
        self.texts = []
        self.bodies = []
        self.fail = fail
        self.refuse_cards = refuse_cards

    def __call__(self, request, timeout=None):
        if self.fail:
            raise urllib.error.HTTPError(request.full_url, 500, "nope", {}, None)
        body = json.loads(request.data.decode("utf-8"))
        if self.refuse_cards and "cardsV2" in body:
            raise urllib.error.HTTPError(request.full_url, 400, "bad card", {}, None)
        self.bodies.append(body)
        self.texts.append(body.get("text", body.get("fallbackText")))
        return FakeResponse()


class Result:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


class FakeGcloudStorage:
    """``gcloud storage cat`` / ``cp`` against one in-memory object; any other
    command fails the test (the job has no working gsutil)."""

    def __init__(self, content=None, cat_error="ERROR: (gcloud.storage.cat) The following URLs matched no objects or files:\n-" + GS_STATE):
        self.content = content
        self.cat_error = cat_error
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2] != ["gcloud", "storage"]:
            raise AssertionError(f"not a gcloud storage call: {argv}")
        if "cat" in argv:
            if self.content is None:
                return Result(rc=1, stderr=self.cat_error)
            return Result(stdout=self.content)
        if "cp" in argv:
            self.content = pathlib.Path(argv[-2]).read_text()
            return Result()
        raise AssertionError(argv)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.message = self.dir / "weekly-chat.json"
        self.message.write_text(json.dumps({"week_end": WEEK_END, "text": TEXT}))
        self.state = self.dir / "weekly-chat-state.json"
        self.opener = FakeOpener()

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, now, env=None, state=None, runner=None, opener=None):
        err = io.StringIO()
        argv = ["--message", str(self.message), "--state", state or str(self.state), "--now", now]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = wc.main(argv, environ=ENV if env is None else env, opener=opener or self.opener,
                         runner=runner or (lambda *a, **k: self.fail("no gcloud storage call for a local state")))
        self.log = err.getvalue()
        return rc


class OncePerWeek(Base):
    def test_before_monday_11_am_toronto_posts_nothing(self):
        for now in ("2026-10-25T23:00:00Z", "2026-10-26T14:45:00Z", "2026-10-26T14:59:59Z"):
            self.assertEqual(self.tick(now), 0)
        self.assertEqual(self.opener.texts, [])
        self.assertFalse(self.state.exists())
        self.assertIn("not due until Mon 11:00 AM ET", self.log)

    def test_the_first_tick_at_the_hour_posts_and_later_ticks_and_reruns_do_not(self):
        self.assertEqual(self.tick(DUE), 0)
        self.assertEqual(self.opener.texts, [TEXT])
        self.assertEqual(json.loads(self.state.read_text()), {"week_end": WEEK_END, "posted_at": "2026-10-26T15:00:00+00:00"})
        # The next tick, a re-run of the same tick, and Thursday's.
        for now in ("2026-10-26T15:15:00Z", DUE, "2026-10-29T18:00:00Z"):
            self.assertEqual(self.tick(now), 0)
        self.assertEqual(self.opener.texts, [TEXT])
        self.assertIn("already posted", self.log)

    def test_a_missed_monday_posts_on_the_next_tick_within_the_week(self):
        self.assertEqual(self.tick("2026-10-27T15:00:00Z"), 0)
        self.assertEqual(self.opener.texts, [TEXT])

    def test_the_next_week_posts_again(self):
        self.state.write_text(json.dumps({"week_end": "2026-10-18", "posted_at": "2026-10-19T15:00:00+00:00"}))
        self.assertEqual(self.tick(DUE), 0)
        self.assertEqual(self.opener.texts, [TEXT])

    def test_the_hour_is_toronto_time_in_winter_too(self):
        self.message.write_text(json.dumps({"week_end": "2027-01-10", "text": TEXT}))
        # 11 AM EST is 16:00Z.
        self.assertEqual(self.tick("2027-01-11T13:30:00Z"), 0)
        self.assertEqual(self.opener.texts, [])
        self.assertEqual(self.tick("2027-01-11T16:00:00Z"), 0)
        self.assertEqual(self.opener.texts, [TEXT])


class WhenNothingIsPosted(Base):
    def test_muted_posts_nothing_and_records_nothing(self):
        self.assertEqual(self.tick(DUE, env={**ENV, wc.MUTE_ENV: "true"}), 0)
        self.assertEqual(self.opener.texts, [])
        self.assertFalse(self.state.exists())
        self.assertIn("muted", self.log)

    def test_a_refused_week_has_no_summary_and_posts_nothing(self):
        self.message.unlink()
        self.assertEqual(self.tick(DUE), 0)
        self.assertEqual(self.opener.texts, [])
        self.assertFalse(self.state.exists())
        self.assertIn("no weekly summary to post", self.log)

    def test_a_post_that_fails_after_the_record_is_not_retried(self):
        self.assertEqual(self.tick(DUE, opener=FakeOpener(fail=True)), 1)
        self.assertEqual(json.loads(self.state.read_text())["week_end"], WEEK_END)
        self.assertIn("NOT retried", self.log)
        self.assertNotIn(TOKEN, self.log)
        self.assertEqual(self.tick("2026-10-26T15:15:00Z"), 0)
        self.assertEqual(self.opener.texts, [])

    def test_unconfigured_posts_nothing(self):
        self.assertEqual(self.tick(DUE, env={}), 0)
        self.assertIn("not configured", self.log)
        self.assertFalse(self.state.exists())

    def test_dry_run_records_nothing(self):
        err = io.StringIO()
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = wc.main(["--message", str(self.message), "--state", str(self.state), "--now", DUE, "--dry-run"],
                         environ=ENV, opener=self.opener)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue().strip(), TEXT)
        self.assertEqual(self.opener.texts, [])
        self.assertFalse(self.state.exists())


class TheCard(Base):
    def setUp(self):
        super().setUp()
        self.message.write_text(json.dumps({"week_end": WEEK_END, "text": TEXT, "card": CARD}))

    def test_the_card_is_posted_with_the_text_as_its_fallback_once(self):
        self.assertEqual(self.tick(DUE), 0)
        self.assertEqual(self.opener.bodies, [{"fallbackText": PLAIN, "cardsV2": [CARD]}])
        self.assertEqual(self.tick("2026-10-26T15:15:00Z"), 0)
        self.assertEqual(len(self.opener.bodies), 1)

    def test_a_refused_card_posts_the_text_instead_still_once(self):
        opener = FakeOpener(refuse_cards=True)
        self.assertEqual(self.tick(DUE, opener=opener), 0)
        self.assertEqual(opener.bodies, [{"text": TEXT}])
        self.assertIn("refused the card", self.log)
        self.assertEqual(self.tick("2026-10-26T15:15:00Z", opener=opener), 0)
        self.assertEqual(len(opener.bodies), 1)

    def test_a_server_error_on_the_card_is_not_retried(self):
        opener = FakeOpener(fail=True)
        self.assertEqual(self.tick(DUE, opener=opener), 1)
        self.assertIn("NOT retried", self.log)
        self.assertNotIn("refused the card", self.log)

    def test_muted_posts_no_card(self):
        self.assertEqual(self.tick(DUE, env={**ENV, wc.MUTE_ENV: "true"}), 0)
        self.assertEqual(self.opener.bodies, [])
        self.assertFalse(self.state.exists())

    def test_a_summary_without_a_card_posts_the_text(self):
        self.message.write_text(json.dumps({"week_end": WEEK_END, "text": TEXT}))
        self.assertEqual(self.tick(DUE), 0)
        self.assertEqual(self.opener.bodies, [{"text": TEXT}])

    def test_dry_run_prints_the_message_body_and_records_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            rc = wc.main(["--message", str(self.message), "--state", str(self.state), "--now", DUE, "--dry-run"],
                         environ=ENV, opener=self.opener)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out.getvalue()), {"fallbackText": PLAIN, "cardsV2": [CARD]})
        self.assertEqual(self.opener.bodies, [])
        self.assertFalse(self.state.exists())


class GsState(Base):
    def test_a_missing_object_is_a_first_post_and_is_then_written(self):
        gs = FakeGcloudStorage()
        self.assertEqual(self.tick(DUE, state=GS_STATE, runner=gs), 0)
        self.assertEqual(self.opener.texts, [TEXT])
        self.assertEqual(json.loads(gs.content)["week_end"], WEEK_END)
        self.assertEqual(self.tick("2026-10-26T15:15:00Z", state=GS_STATE, runner=gs), 0)
        self.assertEqual(self.opener.texts, [TEXT])

    def test_an_unreadable_object_posts_nothing(self):
        gs = FakeGcloudStorage(cat_error="AccessDeniedException: 403")
        self.assertEqual(self.tick(DUE, state=GS_STATE, runner=gs), 1)
        self.assertEqual(self.opener.texts, [])
        self.assertIn("not posting this tick", self.log)

    def test_a_corrupt_object_posts_nothing(self):
        gs = FakeGcloudStorage(content="{not json")
        self.assertEqual(self.tick(DUE, state=GS_STATE, runner=gs), 1)
        self.assertEqual(self.opener.texts, [])

    def test_a_corrupt_local_file_posts_nothing_too(self):
        self.state.write_text("{not json")
        self.assertEqual(self.tick(DUE), 1)
        self.assertEqual(self.opener.texts, [])

    def test_a_record_that_fails_to_write_posts_nothing_and_the_next_tick_posts_once(self):
        gs = FakeGcloudStorage()

        def failing_cp(argv, **kwargs):
            if "cp" in argv:
                raise wc.subprocess.CalledProcessError(1, argv)
            return gs(argv, **kwargs)

        # Every tick whose write fails posts nothing: never a post per tick.
        for now in (DUE, "2026-10-26T15:15:00Z"):
            self.assertEqual(self.tick(now, state=GS_STATE, runner=failing_cp), 1)
        self.assertEqual(self.opener.texts, [])
        self.assertIn("not posting", self.log)
        for now in ("2026-10-26T15:30:00Z", "2026-10-26T15:45:00Z"):
            self.assertEqual(self.tick(now, state=GS_STATE, runner=gs), 0)
        self.assertEqual(self.opener.texts, [TEXT])
        self.assertEqual(json.loads(gs.content)["week_end"], WEEK_END)

if __name__ == "__main__":
    unittest.main()
