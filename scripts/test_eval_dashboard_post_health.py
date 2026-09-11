"""post_health.py posts on a transition, stays silent otherwise, digests once a
day, renders the five approved message shapes, and never lets the token, the
space or the webhook URL reach a log.

Times in messages are Toronto time ("7:30 AM ET"); T0 is 12:00Z, which is
8:00 AM EDT on 2026-09-04, and the digest hour is 9 AM Toronto (13:00Z that
day), so the window is 12:40Z-13:20Z.

The HTTP layer is a recording fake handed in as `opener`; nothing here opens
a socket or touches a bucket (the gs:// state path is exercised through a
recording `runner`, the GitHub calls through a recording `gh_runner`).
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
URL = post_health.DASHBOARD_URL
GH_ENV = {post_health.ghcli.TOKEN_ENV: "ghs_workflow_token"}

T0 = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
DIGEST_HOUR = 9  # Toronto; 13:00Z in September
DIGEST_UTC = 13

TRIO = ["cluster-agent-crashloop-debug", "cluster-agent-crashloop-evidence-chain", "cluster-agent-crashloop-misleading-symptom"]
CONDITION = {"GREEN": None, "DEGRADED": "storm", "OUTAGE": "shared_break"}


def health(state="GREEN", cause="", cases=(), since="2026-09-04T03:30:00+00:00", condition=None, prs=(), runs=0, window=(None, None), issues=()):
    return {
        "schema_version": 1,
        "state": state,
        "condition": condition or CONDITION[state],
        "since": since,
        "cause": cause,
        "failing_cases": list(cases),
        "tracking_issues": list(issues),
        "incident": None
        if state == "GREEN"
        else {"prs": list(prs), "runs": runs, "window_start": window[0], "window_end": window[1]},
        "evidence": [],
        "advice": "",
        "recovering": False,
        "stale": False,
        "metrics": {
            "window_hours": 24,
            "full_runs": 31,
            "prs": 19,
            "green_runs": 26,
            "red_runs": 5,
            "pr_caused_reds": 2,
            "infra_reds": 5,
            "green_rate": 0.839,
            "aborted_runs": 40,
            "setup_deaths": 2,
            "infra_rep_rate": 0.062,
            "infra_reps": 110,
            "wall_clock_p50_s": 7500,
            "wall_clock_p90_s": 16200,
        },
        "generated_at": "2026-09-04T12:00:00+00:00",
    }


def outage(cases=TRIO, since="2026-09-08T09:00:00+00:00", prs=(1246, 1238, 608, 1150, 1226, 1275), issues=()):
    return health("OUTAGE", "shared fixture/environment break: " + ", ".join(cases), cases, since=since, prs=prs, runs=len(prs), issues=issues)


def storm(since="2026-09-03T18:30:00+00:00", window=("2026-09-03T17:15:00+00:00", "2026-09-03T18:25:00+00:00"), prs=(1182, 1167, 1188)):
    return health("DEGRADED", "quota storm window 17:15–18:25 UTC", since=since, condition="storm", prs=prs, runs=len(prs), window=window)


def deaths(since="2026-09-05T13:00:00+00:00", prs=(965, 1121, 1186, 1199)):
    return health("DEGRADED", "setup/clone failures on 4 runs", since=since, condition="setup_deaths", prs=prs, runs=4)


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

    @property
    def texts(self):
        return [body["text"] for body in self.bodies]


class GhResult:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


class FakeGh:
    """A `gh api` runner: records (method, path, body); answers from a script
    keyed by method, in order, defaulting to an empty list for reads and a
    freshly numbered issue or comment for writes."""

    def __init__(self, open_issues=(), fail_writes=False):
        self.calls = []
        self.open_issues = list(open_issues)
        self.fail_writes = fail_writes
        self.next_number = 1300

    def __call__(self, argv, input=None, **kwargs):
        method, path = argv[argv.index("-X") + 1], argv[argv.index("-X") + 2]
        body = json.loads(input) if input else None
        self.calls.append((method, path, body))
        if method == "GET":
            return GhResult(stdout="\n".join(json.dumps(issue) for issue in self.open_issues))
        if self.fail_writes:
            return GhResult(rc=1, stderr="gh: Resource not accessible by integration (HTTP 403)")
        number = self.next_number
        self.next_number += 1
        return GhResult(stdout=json.dumps({"number": number, "html_url": f"https://github.com/gke-labs/kube-agents/issues/{number}", "id": number}))

    def writes(self):
        return [(method, path) for method, path, _ in self.calls if method != "GET"]


class RunHarness(unittest.TestCase):
    """Drives `main` end to end against a temp state file and a fake opener."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.state = self.dir / "state.json"
        self.opener = FakeOpener()
        self.gh = FakeGh()

    def tick(self, health_doc, now, environ=None, opener=None, dry_run=False, digest_hour=DIGEST_HOUR, digest_tz=None, gh=None, data=None):
        path = self.dir / "health.json"
        path.write_text(json.dumps(health_doc))
        argv = ["--health", str(path), "--state", str(self.state), "--now", now.isoformat(), "--digest-hour", str(digest_hour)]
        if data is not None:
            data_path = self.dir / "data.json"
            data_path.write_text(data if isinstance(data, str) else json.dumps(data))
            argv += ["--data", str(data_path)]
        if digest_tz:
            argv += ["--digest-tz", digest_tz]
        if dry_run:
            argv.append("--dry-run")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = post_health.main(
                argv,
                environ={post_health.SPACE_ENV: SPACE, post_health.TOKEN_ENV: TOKEN} if environ is None else environ,
                opener=opener or self.opener,
                gh_runner=gh or self.gh,
            )
        return rc, err.getvalue()

    def at(self, hour, minute=0, day=4):
        return T0.replace(day=day, hour=hour, minute=minute)

    def recorded(self):
        return json.loads(self.state.read_text())


# --------------------------------------------------------------------------- #
# The five shapes
# --------------------------------------------------------------------------- #


class Shapes(RunHarness):
    def test_outage(self):
        self.tick(outage(issues=["#1278"]), T0)
        self.assertEqual(
            self.opener.texts[0],
            "🔴 *Smoke gate: broken* — the 3 crashloop tests fail on every PR since 5:00 AM ET (6 PRs so far). Shared test fixture, not your code.\n"
            "Don't retest yet. Tracking #1278.\n"
            f"{URL}?cases=cluster-agent-crashloop-debug,cluster-agent-crashloop-evidence-chain,cluster-agent-crashloop-misleading-symptom&since=2026-09-08T09:00:00Z#gate",
        )

    def test_outage_without_an_issue_says_so(self):
        self.tick(outage(cases=["cost-idle-pool-probe"], prs=(1, 2, 3)), T0)
        lines = self.opener.texts[0].split("\n")
        self.assertEqual(lines[0], "🔴 *Smoke gate: broken* — cost-idle-pool-probe fail on every PR since 5:00 AM ET (3 PRs so far). Shared test fixture, not your code.")
        self.assertEqual(lines[1], "Don't retest yet. Tracking no issue yet — file one with the presubmit-gate label.")

    def test_storm(self):
        self.tick(storm(), T0)
        self.assertEqual(
            self.opener.texts[0],
            "🟡 *Smoke gate: flaky* — quota storm 1:15 PM–2:25 PM ET hit 3 PRs.  Passing runs still count; if yours went red, retest after 2:55 PM ET.\n"
            f"{URL}?since=2026-09-03T18:30:00Z#gate",
        )

    def test_setup_deaths(self):
        self.tick(deaths(), T0)
        self.assertEqual(
            self.opener.texts[0],
            "🟡 *Smoke gate: flaky* — 4 runs on 4 PRs died during setup since 9:00 AM ET.  Passing runs still count; if yours died before any test ran, retest.\n"
            f"{URL}?since=2026-09-05T13:00:00Z#gate",
        )

    def test_recovery(self):
        self.tick(outage(cases=TRIO, since="2026-09-07T14:00:00+00:00", issues=["#1269"]), T0.replace(day=7, hour=14))
        self.tick(health(since="2026-09-08T01:00:00+00:00"), T0.replace(day=8, hour=1))
        self.assertEqual(
            self.opener.texts[1],
            "🟢 *Smoke gate: healthy again* — fixed after 11h (the 3 crashloop tests were failing, #1269).\n"
            f"{URL}?cases=cluster-agent-crashloop-debug,cluster-agent-crashloop-evidence-chain,cluster-agent-crashloop-misleading-symptom&since=2026-09-07T14:00:00Z&until=2026-09-08T01:00:00Z#gate",
        )

    def test_recovery_from_a_storm(self):
        self.tick(storm(since="2026-09-04T09:47:00+00:00"), T0)
        self.tick(health(), T0.replace(hour=15, minute=30))
        self.assertEqual(self.opener.texts[1].split("\n")[0], "🟢 *Smoke gate: healthy again* — fixed after 5h 43m (quota storm).")

    def test_digest(self):
        self.tick(health(), self.at(DIGEST_UTC, 5))
        self.assertEqual(
            self.opener.texts[0],
            f"📊 *Smoke gate, last 24h:* 31 runs · 26 green · 2 PR-caused red · 5 infra · typical run 125 min\n{URL}?since=2026-09-04T03:30:00Z#agent",
        )

    def test_stale_and_fresh_again(self):
        doc = health()
        doc["stale"] = True
        doc["generated_at"] = "2026-09-04T05:55:40+00:00"
        self.tick(doc, T0)
        self.assertEqual(self.opener.texts[0], "⚪ *Smoke gate: no fresh data since 1:55 AM ET* — the health bot can't see recent runs. Someone check the refresh job.")
        self.tick(health(), T0.replace(minute=15))
        self.assertEqual(self.opener.texts[1], "⚪ *Smoke gate: fresh data again* — refreshed 8:00 AM ET; the gate reads GREEN.")

    def test_times_are_toronto_and_dst_correct(self):
        clock = post_health.clock
        self.assertEqual(clock(datetime(2026, 9, 8, 11, 30, tzinfo=timezone.utc)), "7:30 AM ET")
        self.assertEqual(clock(datetime(2026, 9, 8, 11, 30, tzinfo=timezone.utc), weekday=True), "Tue 7:30 AM ET")
        self.assertEqual(clock(datetime(2026, 1, 8, 12, 30, tzinfo=timezone.utc)), "7:30 AM ET", "EST in January")
        self.assertEqual(clock(datetime(2026, 9, 8, 4, 0, tzinfo=timezone.utc)), "12:00 AM ET")
        self.assertEqual(clock(datetime(2026, 9, 8, 16, 0, tzinfo=timezone.utc)), "12:00 PM ET")
        self.assertEqual(post_health.clock_range(datetime(2026, 9, 8, 17, 15, tzinfo=timezone.utc), datetime(2026, 9, 8, 18, 25, tzinfo=timezone.utc)), "1:15 PM–2:25 PM ET")
        self.assertEqual(clock(None), "?")
        # The links stay ISO UTC.
        self.tick(outage(since="2026-09-08T11:30:00+00:00"), T0)
        self.assertIn("since 7:30 AM ET", self.opener.texts[0])
        self.assertTrue(self.opener.texts[0].endswith("&since=2026-09-08T11:30:00Z#gate"))

    def test_case_descriptions(self):
        d = post_health.describe_cases
        self.assertEqual(d(TRIO), "the 3 crashloop tests")
        self.assertEqual(d(["cost-idle-pool-probe"]), "cost-idle-pool-probe")
        self.assertEqual(d(["cost-idle-pool-probe", "security-overgrant-probe"]), "cost-idle-pool-probe and security-overgrant-probe")
        self.assertEqual(d(["a-probe", "b-probe", "c-probe", "d-probe"]), "4 tests (a-probe, b-probe, c-probe and 1 more)")
        self.assertEqual(d(["obtainability-remediation-proposal", "obtainability-fleet-exposure-sweep"]), "the 2 obtainability tests")
        self.assertEqual(d([]), "tests")


# --------------------------------------------------------------------------- #
# When a message goes out
# --------------------------------------------------------------------------- #


class TransitionPosting(RunHarness):
    def test_first_green_tick_posts_nothing_but_records_state(self):
        rc, err = self.tick(health(), T0)
        self.assertEqual(rc, 0)
        self.assertEqual(self.opener.requests, [])
        self.assertEqual(self.recorded()["state"], "GREEN")
        self.assertIn("posted nothing", err)

    def test_first_tick_in_trouble_posts_the_state(self):
        rc, _ = self.tick(storm(), T0)
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opener.requests), 1)
        self.assertTrue(self.opener.texts[0].startswith("🟡 *Smoke gate: flaky*"))

    def test_posts_on_transition_and_stays_silent_without_one(self):
        self.tick(health(), T0)
        self.tick(health(), T0.replace(minute=15))
        self.assertEqual(self.opener.requests, [], "no change, no post")
        doc = outage(since="2026-09-04T12:30:00+00:00")
        self.tick(doc, T0.replace(minute=30))
        self.assertEqual(len(self.opener.requests), 1)
        self.assertTrue(self.opener.texts[0].startswith("🔴 *Smoke gate: broken*"))
        self.tick(doc, T0.replace(hour=14))
        self.assertEqual(len(self.opener.requests), 1, "same outage, same cases: silent")

    def test_outage_reposts_only_when_a_new_case_joins_and_not_within_the_interval(self):
        one = outage(cases=["a"], since="2026-09-04T12:00:00+00:00", prs=(1, 2, 3))
        two = outage(cases=["a", "b"], since="2026-09-04T12:00:00+00:00", prs=(1, 2, 3, 4))
        self.tick(one, T0)
        self.tick(two, T0.replace(minute=30))
        self.assertEqual(len(self.opener.requests), 1, "a new case inside the interval waits")
        self.tick(two, T0.replace(hour=14, minute=15))
        self.assertEqual(len(self.opener.requests), 2, "past the interval the grown list goes out")
        self.assertIn("a and b fail on every PR since 8:00 AM ET (4 PRs so far)", self.opener.texts[1])
        self.tick(one, T0.replace(hour=17))
        self.assertEqual(len(self.opener.requests), 2, "a case dropping off is not news")

    def test_a_condition_change_inside_degraded_is_posted(self):
        self.tick(storm(), T0)
        self.tick(deaths(), T0.replace(minute=15))
        self.assertEqual(len(self.opener.requests), 2)
        self.assertIn("died during setup", self.opener.texts[1])

    def test_staleness_is_posted_once_each_way(self):
        doc = health()
        doc["stale"] = True
        doc["generated_at"] = "2026-09-04T05:55:40+00:00"
        self.tick(doc, T0)
        self.tick(doc, T0.replace(minute=15))
        self.assertEqual(len(self.opener.requests), 1)
        self.tick(health(), T0.replace(minute=30))
        self.assertEqual(len(self.opener.requests), 2)
        self.assertTrue(self.opener.texts[1].startswith("⚪ *Smoke gate: fresh data again*"))

    def test_a_failed_digest_beside_a_posted_change_does_not_repeat_the_change(self):
        self.tick(health(), T0)
        partial = FakeOpener(statuses=[200, 500])
        rc, err = self.tick(storm(), self.at(DIGEST_UTC - 1, 50), opener=partial)
        self.assertEqual(rc, 1)
        self.assertIn("failed to post: digest", err)
        self.assertEqual([text.split(" ")[0] for text in partial.texts], ["🟡", "📊"])
        rc, _ = self.tick(storm(), self.at(DIGEST_UTC, 5))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opener.requests), 1, "only the digest is retried")
        self.assertTrue(self.opener.texts[0].startswith("📊"))

    def test_a_change_that_fails_beside_a_stale_notice_that_succeeds_is_posted_next_tick(self):
        # The dashboard refresh resumes after a stall and the fresh data
        # shows a break: decide emits change + stale in one tick. If the
        # change's POST fails, the state file must not record the OUTAGE as
        # told on the strength of the stale notice.
        self.tick(health(), T0)
        doc = outage(cases=["a"], since="2026-09-04T12:15:00+00:00", prs=(1, 2, 3))
        doc["stale"] = True
        partial = FakeOpener(statuses=[500, 200])
        rc, err = self.tick(doc, T0.replace(minute=15), opener=partial)
        self.assertEqual(rc, 1)
        self.assertIn("failed to post: change", err)
        recorded = self.recorded()
        self.assertEqual((recorded["state"], recorded["failing_cases"], recorded["stale"]), ("GREEN", [], True))
        rc, _ = self.tick(doc, T0.replace(minute=30))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opener.requests), 1, "the change alone is retried")
        self.assertTrue(self.opener.texts[0].startswith("🔴 *Smoke gate: broken*"))
        # The mirror: change succeeds, stale fails -> stale retried alone.
        fresh = outage(cases=["a"], since="2026-09-04T12:15:00+00:00", prs=(1, 2, 3))
        partial = FakeOpener(statuses=[500])
        self.tick(fresh, T0.replace(minute=45), opener=partial)  # stale flips back to False; the post fails
        self.assertTrue(self.recorded()["stale"], "still told as stale")
        self.tick(fresh, T0.replace(hour=14))
        self.assertTrue(self.opener.texts[-1].startswith("⚪ *Smoke gate: fresh data again*"))

    def test_a_stale_notice_mid_outage_does_not_swallow_a_case_that_joined_inside_the_interval(self):
        one = outage(cases=["a"], since="2026-09-04T12:00:00+00:00", prs=(1, 2, 3))
        two = outage(cases=["a", "b"], since="2026-09-04T12:00:00+00:00", prs=(1, 2, 3))
        two["stale"] = True
        self.tick(one, T0)
        self.tick(two, T0.replace(minute=30))
        self.assertEqual(len(self.opener.requests), 2, "the stale notice went out; b is inside the interval")
        self.assertEqual(self.recorded()["failing_cases"], ["a"], "b is not recorded as told")
        self.tick(two, T0.replace(hour=14, minute=15))
        self.assertEqual(len(self.opener.requests), 3)
        self.assertIn("a and b fail on every PR", self.opener.texts[-1])

    def test_a_failed_post_leaves_the_state_untouched_so_the_next_tick_retries(self):
        self.tick(health(), T0)
        failing = FakeOpener(statuses=[500])
        rc, err = self.tick(deaths(), T0.replace(minute=15), opener=failing)
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 500", err)
        self.assertEqual(self.recorded()["state"], "GREEN")
        rc, _ = self.tick(deaths(), T0.replace(minute=30))
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.opener.requests), 1, "retried on the next tick")

    def test_the_recovery_cites_what_the_space_was_told(self):
        # The tracking issue and cases recorded at the change are what the
        # recovery names, even if health.json has since dropped them.
        self.tick(outage(cases=["a"], prs=(1, 2, 3), issues=["#1"]), T0)
        self.assertEqual(self.recorded()["tracking_issues"], ["#1"])
        self.tick(health(), T0.replace(hour=15))
        self.assertIn("(a were failing, #1)", self.opener.texts[1])


class Digest(RunHarness):
    def test_digest_goes_out_once_in_the_window_and_once_per_day(self):
        self.tick(health(), self.at(DIGEST_UTC - 1, 30))
        self.assertEqual(self.opener.requests, [], "outside the window")
        self.tick(health(), self.at(DIGEST_UTC - 1, 45))
        self.assertEqual(len(self.opener.requests), 1)
        self.assertTrue(self.opener.texts[0].startswith("📊 *Smoke gate, last 24h:* 31 runs · 26 green"))
        self.assertEqual(self.recorded()["last_digest_date"], "2026-09-04", "the marker is the Toronto date")
        self.tick(health(), self.at(DIGEST_UTC, 0))
        self.tick(health(), self.at(DIGEST_UTC, 15))
        self.assertEqual(len(self.opener.requests), 1, "one digest per day")
        self.tick(health(), self.at(DIGEST_UTC, 5, day=5))
        self.assertEqual(len(self.opener.requests), 2, "the next day gets its own")

    def test_digest_hour_is_a_toronto_hour_and_the_zone_is_configurable(self):
        # 9 AM Toronto is 13:00Z in September and 14:00Z in January (DST).
        self.tick(health(), datetime(2026, 1, 15, 13, 5, tzinfo=timezone.utc))
        self.assertEqual(self.opener.requests, [], "13:05Z is 8:05 AM EST: not yet")
        self.tick(health(), datetime(2026, 1, 15, 14, 5, tzinfo=timezone.utc))
        self.assertEqual(len(self.opener.requests), 1)
        self.assertEqual(self.recorded()["last_digest_date"], "2026-01-15")
        # --digest-tz UTC reads the hour on the UTC clock.
        self.tick(health(), datetime(2026, 1, 16, 9, 5, tzinfo=timezone.utc), digest_tz="UTC")
        self.assertEqual(len(self.opener.requests), 2)
        # A day boundary on the local clock: 03:05Z on the 17th is still the
        # 16th in Toronto, so a digest at 9 AM local that day is a new one.
        self.tick(health(), datetime(2026, 1, 17, 14, 5, tzinfo=timezone.utc))
        self.assertEqual(len(self.opener.requests), 3)

    def test_digest_carries_one_line_on_last_night_when_data_json_is_given(self):
        # Two nights of the nightly tier; the digest goes out Fri 9 AM ET
        # (13:00Z on 2026-09-04), so the night that started 00:00Z Friday
        # (Thu 8 PM ET) is last night.
        def night(build, day, tasks, **extra):
            run = {"build_id": build, "tier": "nightly", "job": "ci-kube-agents-eval-nightly", "pr": None, "head_sha": "abc1234",
                   "started": f"2026-09-{day:02d}T00:00:00+00:00", "finished": f"2026-09-{day:02d}T06:40:00+00:00",
                   "result": "FAILURE", "duration_s": 24000, "tasks": tasks}
            run.update(extra)
            return run

        def task(name, *results):
            return {"name": name, "result": "fail" if "fail" in results else "pass",
                    "reps": [{"n": i + 1, "result": r, "reason": None if r == "pass" else "check absent"} for i, r in enumerate(results)]}

        cases = [{"name": n, "domain": "cost", "active": True, "nightly_active": True} for n in ("case-a", "case-b", "case-c")]
        data = {"schema_version": 1, "generated_at": T0.isoformat(), "cases": cases, "runs": [
            night("3000000000000000001", 3, [task("case-a", "pass", "pass", "pass"), task("case-b", "pass", "pass", "pass"), task("case-c", "pass", "pass", "pass")]),
            night("3000000000000000002", 4, [task("case-a", "pass", "pass", "pass"), task("case-b", "pass", "fail", "pass"), task("case-c", "fail", "fail", "fail")]),
        ]}
        self.tick(health(), self.at(DIGEST_UTC, 5), data=data)
        self.assertEqual(
            self.opener.texts[0],
            "📊 *Smoke gate, last 24h:* 31 runs · 26 green · 2 PR-caused red · 5 infra · typical run 125 min\n"
            "🌙 Nightly: 3 cases · 1 passed all reps · 1 partial · 1 failed · newly failing: case-c · 6h 40m\n"
            f"{post_health.NIGHTLY_URL}\n{URL}?since=2026-09-04T03:30:00Z#agent",
        )
        self.assertEqual(post_health.NIGHTLY_URL, "https://storage.cloud.google.com/kube-agents-dashboards/evals/nightly.html")
        # A truncated night says so instead of numbers; a missing night too.
        data["runs"].append(night("3000000000000000003", 5, data["runs"][-1]["tasks"][:2], result="ABORTED", duration_s=None))
        self.tick(health(), self.at(DIGEST_UTC, 5, day=5), data=data)
        self.assertIn("\n🌙 Nightly: truncated after 6h 40m · 2 of 3 cases recorded · the night's numbers are not comparable\n", self.opener.texts[1])
        self.tick(health(), self.at(DIGEST_UTC, 5, day=7), data=data)
        self.assertIn("\n🌙 Nightly: no run last night (the newest on record started Fri 8:00 PM ET)\n", self.opener.texts[2])
        # No nightly run collected yet, and an unreadable data.json: the
        # digest still goes out and the line says what happened.
        self.tick(health(), self.at(DIGEST_UTC, 5, day=8), data={"schema_version": 1, "runs": [], "cases": []})
        self.assertIn("\n🌙 Nightly: no run on record yet\n", self.opener.texts[3])
        rc, err = self.tick(health(), self.at(DIGEST_UTC, 5, day=9), data="{not json")
        self.assertEqual(rc, 0)
        self.assertIn("warning:", err)
        self.assertIn("\n🌙 Nightly: no data.json to read a night from\n", self.opener.texts[4])

    def test_the_digest_without_data_json_is_unchanged(self):
        self.tick(health(), self.at(DIGEST_UTC, 5))
        self.assertNotIn("Nightly", self.opener.texts[0])

    def test_digest_hour_is_configurable_and_goes_out_beside_a_change(self):
        self.tick(outage(), T0.replace(hour=17, minute=50), digest_hour=14)  # 1:50 PM ET
        self.assertEqual([text.split(" ")[0] for text in self.opener.texts], ["🔴", "📊"])

    def test_digest_carries_the_stale_note_every_day_while_the_stall_lasts(self):
        doc = health()
        doc["stale"] = True
        doc["generated_at"] = "2026-09-04T05:55:40+00:00"
        self.tick(doc, T0.replace(hour=7, minute=0))  # the flip: the stale notice alone
        self.assertEqual(len(self.opener.requests), 1)
        for day in (4, 5):
            self.tick(doc, self.at(DIGEST_UTC, 5, day=day))
        digests = [text for text in self.opener.texts if text.startswith("📊")]
        self.assertEqual(len(digests), 2)
        for text in digests:
            self.assertEqual(text.split("\n")[1], "⚪ No fresh data since 1:55 AM ET — these numbers stop there. Someone check the refresh job.")
        self.tick(health(), self.at(DIGEST_UTC, 5, day=6))
        self.assertNotIn("No fresh data", self.opener.texts[-1])

    def test_digest_without_a_p50_says_so(self):
        doc = health()
        doc["metrics"]["wall_clock_p50_s"] = None
        self.tick(doc, self.at(DIGEST_UTC, 5))
        self.assertIn("typical run n/a", self.opener.texts[0])


# --------------------------------------------------------------------------- #
# The tracking issue
# --------------------------------------------------------------------------- #


class TrackingIssue(RunHarness):
    """A new OUTAGE with no issue files one (once), adopts a human's when one
    names the same cases, comments on recovery, never closes, and a GitHub
    failure costs only the "Tracking #NNN" wording."""

    def environ(self):
        return {post_health.SPACE_ENV: SPACE, post_health.TOKEN_ENV: TOKEN, **GH_ENV}

    def test_a_new_outage_files_the_issue_once_and_the_message_cites_it(self):
        self.tick(health(), T0, environ=self.environ())
        self.assertEqual(self.gh.calls, [], "a green tick asks GitHub nothing")
        doc = outage(cases=["cost-idle-pool-probe", "reliability-pdb-probe"], since="2026-09-08T11:30:00+00:00", prs=(1, 2, 3))
        rc, _ = self.tick(doc, T0.replace(minute=15), environ=self.environ())
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh.writes(), [("POST", "repos/gke-labs/kube-agents/issues")])
        _, _, body = self.gh.calls[-1]
        self.assertEqual(body["title"], "Smoke gate outage: 2 cases failing on every PR since Tue 7:30 AM ET")
        self.assertEqual(body["labels"], ["presubmit-gate"])
        self.assertIn("- `cost-idle-pool-probe`\n- `reliability-pdb-probe`", body["body"])
        self.assertIn("**Window:** since Tue 7:30 AM ET (2026-09-08T11:30:00+00:00), 3 PRs red so far.", body["body"])
        self.assertIn("**Class:** shared fixture/environment break: cost-idle-pool-probe, reliability-pdb-probe (`shared_break`).", body["body"])
        self.assertIn(f"Incident brief: {URL}?cases=cost-idle-pool-probe,reliability-pdb-probe&since=2026-09-08T11:30:00Z#gate", body["body"])
        self.assertTrue(body["body"].rstrip().endswith("Filed automatically by the smoke health bot; edit freely. Fix PRs: reference this issue."))
        self.assertEqual(self.opener.texts[0].split("\n")[1], "Don't retest yet. Tracking #1300.")
        recorded = self.recorded()
        self.assertEqual(recorded["issue"], {"number": 1300, "url": "https://github.com/gke-labs/kube-agents/issues/1300"})
        # The same outage growing a case re-posts but does not re-file.
        grown = outage(cases=["cost-idle-pool-probe", "reliability-pdb-probe", "agent-kanban-smoke"], since="2026-09-08T11:30:00+00:00", prs=(1, 2, 3, 4))
        self.tick(grown, T0.replace(hour=15), environ=self.environ())
        self.assertEqual(len(self.gh.writes()), 1, "filed once")
        self.assertIn("Tracking #1300.", self.opener.texts[-1])

    def test_a_human_filed_issue_naming_the_cases_is_adopted(self):
        human = {"number": 1278, "html_url": "https://github.com/gke-labs/kube-agents/issues/1278", "title": "Seeded fleet outage", "body": "cluster-agent-crashloop-debug, cluster-agent-crashloop-evidence-chain and cluster-agent-crashloop-misleading-symptom red every PR"}
        other = {"number": 1254, "html_url": "x", "title": "upgrades-lagging-master-probe: rung-4 collapse", "body": "unrelated"}
        gh = FakeGh(open_issues=[other, human])
        self.tick(outage(cases=TRIO), T0, environ=self.environ(), gh=gh)
        self.assertEqual(gh.writes(), [], "nothing filed")
        self.assertIn("Tracking #1278.", self.opener.texts[0])
        self.assertEqual(self.recorded()["issue"]["number"], 1278)

    def test_a_case_notes_issue_means_nothing_is_filed(self):
        self.tick(outage(issues=["#1269"]), T0, environ=self.environ())
        self.assertEqual(self.gh.calls, [])
        self.assertIn("Tracking #1269.", self.opener.texts[0])

    def test_recovery_comments_on_the_issue_and_drops_it(self):
        self.tick(outage(cases=["a-probe"], since="2026-09-04T09:00:00+00:00", prs=(1, 2, 3)), T0, environ=self.environ())
        self.tick(health(), T0.replace(hour=15, minute=30), environ=self.environ())
        self.assertEqual(self.gh.writes()[-1], ("POST", "repos/gke-labs/kube-agents/issues/1300/comments"))
        self.assertEqual(self.gh.calls[-1][2], {"body": "Healthy again after 6h 30m; bot will not close it."})
        self.assertNotIn("PATCH", [method for method, _ in self.gh.writes()], "never closed")
        self.assertIn("(a-probe were failing, #1300)", self.opener.texts[1])
        self.assertIsNone(self.recorded()["issue"])

    def test_a_github_failure_is_a_warning_and_the_message_says_no_issue_yet(self):
        gh = FakeGh(fail_writes=True)
        rc, err = self.tick(outage(cases=["a-probe"], prs=(1, 2, 3)), T0, environ=self.environ(), gh=gh)
        self.assertEqual(rc, 0)
        self.assertIn("warning: gh api POST", err)
        self.assertIn("Tracking no issue yet — file one with the presubmit-gate label.", self.opener.texts[0])
        self.assertIsNone(self.recorded()["issue"])

    def test_without_a_github_token_nothing_is_asked(self):
        self.tick(outage(cases=["a-probe"], prs=(1, 2, 3)), T0)
        self.assertEqual(self.gh.calls, [])

    def test_dry_run_prints_the_issue_instead_of_filing_it(self):
        rc, err = self.tick(outage(cases=["a-probe"], prs=(1, 2, 3)), T0, environ={}, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh.writes(), [])
        self.assertIn("--dry-run: would POST repos/gke-labs/kube-agents/issues", err)
        self.assertIn("Smoke gate outage: 1 case failing on every PR since", err)


# --------------------------------------------------------------------------- #
# Deep links
# --------------------------------------------------------------------------- #


class DeepLinks(RunHarness):
    """Every message but the stale notice ends with the dashboard deep link the
    dashboard understands: query before fragment, literal commas and colons,
    #gate for an incident, #agent for the digest."""

    def last_line(self, index=-1):
        return self.opener.texts[index].split("\n")[-1]

    def test_a_state_change_links_the_cases_and_the_start(self):
        self.tick(outage(cases=["cluster-agent-crashloop-debug", "cluster-agent-crashloop-evidence-chain"], since="2026-09-08T03:08:00+00:00"), T0)
        self.assertEqual(self.last_line(), f"{URL}?cases=cluster-agent-crashloop-debug,cluster-agent-crashloop-evidence-chain&since=2026-09-08T03:08:00Z#gate")

    def test_a_storm_links_the_start_without_cases(self):
        self.tick(storm(since="2026-09-04T18:30:00+00:00"), T0)
        self.assertEqual(self.last_line(), f"{URL}?since=2026-09-04T18:30:00Z#gate")

    def test_a_recovery_closes_the_incident_with_until(self):
        self.tick(outage(cases=["x-probe"], since="2026-09-04T03:08:00+00:00", prs=(1, 2, 3)), T0)
        self.tick(health(since="2026-09-04T14:00:00+00:00"), T0.replace(hour=14))
        self.assertEqual(self.last_line(), f"{URL}?cases=x-probe&since=2026-09-04T03:08:00Z&until=2026-09-04T14:00:00Z#gate")

    def test_the_digest_links_the_agent_section(self):
        self.tick(health(since="2026-09-04T03:30:00+00:00"), self.at(DIGEST_UTC, 5))
        self.assertEqual(self.last_line(), f"{URL}?since=2026-09-04T03:30:00Z#agent")

    def test_the_link_is_the_whole_last_line(self):
        self.tick(storm(since="2026-09-04T18:30:00+00:00"), T0)
        text = self.opener.texts[0]
        self.assertTrue(text.split("\n")[-1].startswith("https://"))
        self.assertNotIn("Dashboard:", text)


# --------------------------------------------------------------------------- #
# Secrets and transport
# --------------------------------------------------------------------------- #


class Secrecy(RunHarness):
    def test_the_chat_api_request_shape(self):
        self.tick(storm(), T0)
        request = self.opener.requests[0]
        self.assertEqual(request.full_url, "https://chat.googleapis.com/v1/spaces/AAAAtestspace/messages")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(request.get_header("Content-type"), "application/json; charset=UTF-8")
        self.assertEqual(set(self.opener.bodies[0]), {"text"})

    def test_a_bare_space_id_is_normalized(self):
        environ = {post_health.SPACE_ENV: "AAAAtestspace", post_health.TOKEN_ENV: TOKEN}
        self.tick(storm(), T0, environ=environ)
        self.assertTrue(self.opener.requests[0].full_url.endswith("/spaces/AAAAtestspace/messages"))

    def test_webhook_is_the_alternative_when_no_space_is_set(self):
        environ = {post_health.WEBHOOK_ENV: WEBHOOK}
        self.tick(storm(), T0, environ=environ)
        request = self.opener.requests[0]
        self.assertEqual(request.full_url, WEBHOOK)
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(set(self.opener.bodies[0]), {"text"})

    def test_nothing_configured_exits_zero_and_posts_nothing(self):
        rc, err = self.tick(outage(), T0, environ={})
        self.assertEqual(rc, 0)
        self.assertEqual(self.opener.requests, [])
        self.assertIn("webhook not configured", err)
        self.assertFalse(self.state.exists(), "no state is recorded for a post that never happened")

    def test_no_secret_reaches_the_log_on_success_or_failure(self):
        _, ok_err = self.tick(storm(), T0)
        failing = FakeOpener(statuses=[403])
        _, fail_err = self.tick(outage(), T0.replace(minute=15), opener=failing)
        environ = {post_health.WEBHOOK_ENV: WEBHOOK}
        _, hook_err = self.tick(health(), T0.replace(minute=30), environ=environ, opener=FakeOpener(statuses=[404]))
        for text in (ok_err, fail_err, hook_err):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("SECRETKEY", text)
            self.assertNotIn("SECRETTOKEN", text)
            self.assertNotIn(SPACE, text)
        self.assertIn("HTTP 403", fail_err)
        self.assertIn("HTTP 404", hook_err)

    def test_dry_run_prints_the_message_and_still_records_state(self):
        rc, err = self.tick(storm(), T0, environ={}, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("--dry-run: would post [change]", err)
        self.assertIn("🟡 *Smoke gate: flaky*", err)
        self.assertEqual(self.opener.requests, [])
        self.assertEqual(self.recorded()["state"], "DEGRADED")


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
