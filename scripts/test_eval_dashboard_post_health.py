"""post_health.py posts on a transition, stays silent otherwise, digests once a
day, and never lets the token, the space or the webhook URL reach a log.

The HTTP layer is a recording fake handed in as `opener`; nothing here opens
a socket or touches a bucket (the gs:// state path is exercised through a
recording `runner`).
"""

import contextlib
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone

from eval_dashboard import post_health

SPACE = "spaces/AAAAtestspace"
TOKEN = "ya29.super-secret-token-value"
WEBHOOK = "https://chat.googleapis.com/v1/spaces/AAAA/messages?key=SECRETKEY&token=SECRETTOKEN"

T0 = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def health(state="GREEN", cause="", cases=(), since="2026-09-04T03:30:00+00:00", advice="", evidence=()):
    return {
        "schema_version": 1,
        "state": state,
        "condition": None if state == "GREEN" else "shared_break",
        "since": since,
        "cause": cause,
        "failing_cases": list(cases),
        "evidence": list(evidence),
        "advice": advice,
        "recovering": False,
        "metrics": {
            "window_hours": 24,
            "full_runs": 31,
            "prs": 19,
            "green_runs": 26,
            "red_runs": 5,
            "green_rate": 0.839,
            "aborted_runs": 40,
            "setup_deaths": 2,
            "infra_rep_rate": 0.062,
            "infra_reps": 110,
            "wall_clock_p50_s": 7500,
            "wall_clock_p90_s": 16200,
            "fixtures": {"healed": 28, "broken": 2, "projects": 30},
        },
        "generated_at": "2026-09-04T12:00:00+00:00",
    }


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records every request; answers with the queued statuses (200 by default)."""

    def __init__(self, statuses=None):
        self.requests = []
        self.statuses = list(statuses or [])

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        status = self.statuses.pop(0) if self.statuses else 200
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "nope", {}, None)
        return FakeResponse(status)

    @property
    def bodies(self):
        return [json.loads(req.data.decode("utf-8")) for req in self.requests]


class RunHarness(unittest.TestCase):
    """Drives `main` end to end against a temp state file and a fake opener."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.state = self.dir / "state.json"
        self.opener = FakeOpener()

    def tick(self, health_doc, now, environ=None, opener=None, dry_run=False, digest_hour=8):
        path = self.dir / "health.json"
        path.write_text(json.dumps(health_doc))
        argv = ["--health", str(path), "--state", str(self.state), "--now", now.isoformat(), "--digest-hour", str(digest_hour)]
        if dry_run:
            argv.append("--dry-run")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = post_health.main(
                argv,
                environ={post_health.SPACE_ENV: SPACE, post_health.TOKEN_ENV: TOKEN} if environ is None else environ,
                opener=opener or self.opener,
            )
        return rc, err.getvalue()


class TransitionPosting(RunHarness):
    def test_first_green_tick_posts_nothing_but_records_state(self):
        rc, err = self.tick(health(), T0)
        self.assertEqual(rc, 0)
        self.assertEqual(self.opener.requests, [])
        self.assertEqual(json.loads(self.state.read_text())["state"], "GREEN")
        self.assertIn("posted nothing", err)

    def test_first_tick_in_trouble_posts_the_state(self):
        rc, _ = self.tick(health("DEGRADED", "quota storm window 18:23–19:58 UTC", advice="Retest after 20:28 UTC."), T0)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opener.requests), 1)
        text = self.opener.bodies[0]["text"]
        self.assertTrue(text.startswith("*CI health: DEGRADED* — quota storm"))
        self.assertIn("Advice: Retest after 20:28 UTC.", text)
        self.assertIn(post_health.DASHBOARD_URL, text)

    def test_posts_on_transition_and_stays_silent_without_one(self):
        self.tick(health(), T0)
        self.tick(health(), T0.replace(minute=15))
        self.assertEqual(self.opener.requests, [], "no change, no post")
        outage = health(
            "OUTAGE",
            "shared fixture/environment break: cluster-agent-crashloop-debug",
            ["cluster-agent-crashloop-debug"],
            since="2026-09-04T12:30:00+00:00",
            advice="Don't retest yet; the failing cases share a cause. Tracking: #1278",
            evidence=["cluster-agent-crashloop-debug failed all graded reps on 6 runs from 6 PRs (#1, #2, #3, #4, #5, #6)"],
        )
        self.tick(outage, T0.replace(minute=30))
        self.assertEqual(len(self.opener.requests), 1)
        text = self.opener.bodies[0]["text"]
        self.assertIn("*CI health: OUTAGE* (was GREEN)", text)
        self.assertIn("• cluster-agent-crashloop-debug failed all graded reps", text)
        self.assertIn("Tracking: #1278", text)
        self.tick(outage, T0.replace(minute=45))
        self.assertEqual(len(self.opener.requests), 1, "same outage, same cause: silent")

    def test_outage_reposts_only_when_a_new_case_joins_and_not_within_the_interval(self):
        one = health("OUTAGE", "break: a", ["a"], since="2026-09-04T12:00:00+00:00")
        two = health("OUTAGE", "break: a, b", ["a", "b"], since="2026-09-04T12:00:00+00:00")
        self.tick(one, T0)
        self.tick(two, T0.replace(minute=30))
        self.assertEqual(len(self.opener.requests), 1, "a new case inside the interval waits")
        self.tick(two, T0.replace(hour=14, minute=15))
        self.assertEqual(len(self.opener.requests), 2, "past the interval the grown list goes out")
        self.tick(one, T0.replace(hour=17))
        self.assertEqual(len(self.opener.requests), 2, "a case dropping off is not news")

    def test_recovery_names_the_duration_and_the_cause(self):
        self.tick(health("DEGRADED", "quota storm window 18:23–19:58 UTC", since="2026-09-04T09:47:00+00:00"), T0)
        self.tick(health(), T0.replace(hour=15, minute=30))
        self.assertEqual(len(self.opener.requests), 2)
        text = self.opener.bodies[1]["text"]
        self.assertIn("*CI health: GREEN* — recovered after 5h 43m of DEGRADED (quota storm window 18:23–19:58 UTC)", text)
        self.assertIn(post_health.DASHBOARD_URL, text)

    def test_a_condition_change_inside_degraded_is_posted(self):
        storm = health("DEGRADED", "quota storm window 18:23–19:58 UTC")
        storm["condition"] = "storm"
        deaths = health("DEGRADED", "setup/clone failures on 3 runs (#1, #2)")
        deaths["condition"] = "setup_deaths"
        self.tick(storm, T0)
        self.tick(deaths, T0.replace(minute=15))
        self.assertEqual(len(self.opener.requests), 2)
        self.assertIn("*CI health: DEGRADED* — setup/clone failures", self.opener.bodies[1]["text"])

    def test_staleness_is_posted_once_each_way(self):
        doc = health()
        doc["stale"] = True
        doc["metrics"]["data_age_s"] = 4 * 24 * 3600
        doc["generated_at"] = "2026-09-04T05:55:40+00:00"
        self.tick(doc, T0)
        self.tick(doc, T0.replace(minute=15))
        self.assertEqual(len(self.opener.requests), 1)
        text = self.opener.bodies[0]["text"]
        self.assertTrue(text.startswith("*CI health: data is stale* — data.json last refreshed 2026-09-04T05:55:40+00:00 (96h ago)"), text)
        self.tick(health(), T0.replace(minute=30))
        self.assertEqual(len(self.opener.requests), 2)
        self.assertTrue(self.opener.bodies[1]["text"].startswith("*CI health: data is fresh again*"))

    def test_a_failed_digest_beside_a_posted_change_does_not_repeat_the_change(self):
        self.tick(health(), T0)
        partial = FakeOpener(statuses=[200, 500])
        rc, err = self.tick(health("DEGRADED", "quota storm window 07:10–07:40 UTC"), T0.replace(hour=7, minute=50), opener=partial)
        self.assertEqual(rc, 1)
        self.assertIn("failed to post: digest", err)
        self.assertEqual([body["text"].split("\n")[0][:24] for body in partial.bodies], ["*CI health: DEGRADED* (w", "*CI health daily digest "])
        rc, _ = self.tick(health("DEGRADED", "quota storm window 07:10–07:40 UTC"), T0.replace(hour=8, minute=5))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opener.requests), 1, "only the digest is retried")
        self.assertTrue(self.opener.bodies[0]["text"].startswith("*CI health daily digest"))

    def test_a_failed_post_leaves_the_state_untouched_so_the_next_tick_retries(self):
        self.tick(health(), T0)
        failing = FakeOpener(statuses=[500])
        rc, err = self.tick(health("DEGRADED", "setup/clone failures on 3 runs (#1, #2)"), T0.replace(minute=15), opener=failing)
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 500", err)
        self.assertEqual(json.loads(self.state.read_text())["state"], "GREEN")
        rc, _ = self.tick(health("DEGRADED", "setup/clone failures on 3 runs (#1, #2)"), T0.replace(minute=30))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opener.requests), 1, "retried on the next tick")


class Digest(RunHarness):
    def test_digest_goes_out_once_in_the_window_and_once_per_day(self):
        self.tick(health(), T0.replace(hour=7, minute=30))
        self.assertEqual(self.opener.requests, [], "outside the window")
        self.tick(health(), T0.replace(hour=7, minute=45))
        self.assertEqual(len(self.opener.requests), 1)
        text = self.opener.bodies[0]["text"]
        self.assertTrue(text.startswith("*CI health daily digest (2026-09-04)* — GREEN since 03:30 UTC"))
        self.assertIn("31 full runs on 19 PRs, 26 green (84%)", text)
        self.assertIn("wall clock p50 2h 05m / p90 4h 30m", text)
        self.assertIn("infra reps 6%", text)
        self.assertIn("setup failures 2", text)
        self.assertIn("Fixtures: 28 healed, 2 broken, across 30 projects", text)
        self.assertIn(post_health.DASHBOARD_URL, text)
        self.tick(health(), T0.replace(hour=8, minute=0))
        self.tick(health(), T0.replace(hour=8, minute=15))
        self.assertEqual(len(self.opener.requests), 1, "one digest per day")
        self.tick(health(), T0.replace(day=5, hour=8, minute=5))
        self.assertEqual(len(self.opener.requests), 2, "the next day gets its own")

    def test_digest_hour_is_configurable_and_carries_the_cause_when_not_green(self):
        doc = health("OUTAGE", "shared fixture/environment break: x", ["x"], advice="Don't retest yet; the failing cases share a cause. Tracking: #1278")
        self.tick(doc, T0.replace(hour=13, minute=50), digest_hour=14)
        kinds = [body["text"].split("\n")[0] for body in self.opener.bodies]
        self.assertEqual(len(kinds), 2, "the first tick posts the state and the digest")
        self.assertTrue(any(line.startswith("*CI health daily digest (2026-09-04)* — OUTAGE since 03:30 UTC: shared fixture") for line in kinds))
        self.assertIn("Advice: Don't retest yet", self.opener.bodies[1]["text"])


class Secrecy(RunHarness):
    def test_the_chat_api_request_shape(self):
        self.tick(health("DEGRADED", "quota storm window 18:23–19:58 UTC"), T0)
        request = self.opener.requests[0]
        self.assertEqual(request.full_url, "https://chat.googleapis.com/v1/spaces/AAAAtestspace/messages")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(request.get_header("Content-type"), "application/json; charset=UTF-8")
        self.assertEqual(set(self.opener.bodies[0]), {"text"})

    def test_a_bare_space_id_is_normalized(self):
        environ = {post_health.SPACE_ENV: "AAAAtestspace", post_health.TOKEN_ENV: TOKEN}
        self.tick(health("DEGRADED", "x"), T0, environ=environ)
        self.assertTrue(self.opener.requests[0].full_url.endswith("/spaces/AAAAtestspace/messages"))

    def test_webhook_is_the_alternative_when_no_space_is_set(self):
        environ = {post_health.WEBHOOK_ENV: WEBHOOK}
        self.tick(health("DEGRADED", "x"), T0, environ=environ)
        request = self.opener.requests[0]
        self.assertEqual(request.full_url, WEBHOOK)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(set(self.opener.bodies[0]), {"text"})

    def test_nothing_configured_exits_zero_and_posts_nothing(self):
        rc, err = self.tick(health("OUTAGE", "x", ["x"]), T0, environ={})
        self.assertEqual(rc, 0)
        self.assertEqual(self.opener.requests, [])
        self.assertIn("webhook not configured", err)
        self.assertFalse(self.state.exists(), "no state is recorded for a post that never happened")

    def test_no_secret_reaches_the_log_on_success_or_failure(self):
        _, ok_err = self.tick(health("DEGRADED", "x"), T0)
        failing = FakeOpener(statuses=[403])
        _, fail_err = self.tick(health("OUTAGE", "y", ["y"]), T0.replace(minute=15), opener=failing)
        environ = {post_health.WEBHOOK_ENV: WEBHOOK}
        _, hook_err = self.tick(health("GREEN"), T0.replace(minute=30), environ=environ, opener=FakeOpener(statuses=[404]))
        for text in (ok_err, fail_err, hook_err):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("SECRETKEY", text)
            self.assertNotIn("SECRETTOKEN", text)
            self.assertNotIn(SPACE, text)
        self.assertIn("HTTP 403", fail_err)
        self.assertIn("HTTP 404", hook_err)

    def test_dry_run_prints_the_message_and_still_records_state(self):
        rc, err = self.tick(health("DEGRADED", "quota storm window 18:23–19:58 UTC"), T0, environ={}, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("--dry-run: would post [change]", err)
        self.assertIn("*CI health: DEGRADED*", err)
        self.assertEqual(self.opener.requests, [])
        self.assertEqual(json.loads(self.state.read_text())["state"], "DEGRADED")


class BucketState(unittest.TestCase):
    def test_gs_state_is_read_and_written_through_gsutil_only(self):
        calls = []

        class Result:
            def __init__(self, rc, out=""):
                self.returncode = rc
                self.stdout = out

        def runner(argv, **kwargs):
            calls.append(argv)
            if argv[:3] == ["gsutil", "-q", "cat"]:
                return Result(1)
            return Result(0)

        self.assertIsNone(post_health.read_state("gs://bucket/evals/health-state.json", runner))
        post_health.write_state("gs://bucket/evals/health-state.json", {"state": "GREEN"}, runner)
        self.assertEqual(calls[0], ["gsutil", "-q", "cat", "gs://bucket/evals/health-state.json"])
        self.assertEqual(calls[1][:5], ["gsutil", "-q", "-h", "Cache-Control: no-cache", "cp"])
        self.assertEqual(calls[1][-1], "gs://bucket/evals/health-state.json")


if __name__ == "__main__":
    unittest.main()
