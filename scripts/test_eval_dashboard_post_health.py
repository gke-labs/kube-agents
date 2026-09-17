"""post_health.py posts on a transition, stays silent otherwise, digests once a
day, renders the six approved message shapes, and never lets the token, the
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
import urllib.parse
from datetime import datetime, timedelta, timezone

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
        "slow": None,
        "pool": None,
        "metrics": {
            "queue_wait_p50_s": None,
            "queue_wait_read": False,
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


def slow_note(since="2026-09-14T18:00:00+00:00", infra_reps=2):
    """health.py's rule-7 note as it read at 18:00Z on 2026-09-14 (#1586)."""
    return {"since": since, "runs": 5, "min_s": 9161, "median_s": 10984, "max_s": 12836, "baseline_days": 7, "baseline_runs": 264, "baseline_p50_s": 9085, "baseline_p90_s": 11919, "infra_reps": infra_reps}


def slow(**note):
    doc = health()
    doc["slow"] = slow_note(**note)
    return doc


def pool_note(verdict="BREACH", cause="CAPACITY", since="2026-09-04T09:00:00+00:00", **over):
    """health.py's rule-8 note. The default is the #1069 incident's shape: a
    full pool, two runs queued past the limit, and a 22-minute median over the
    last three hours against the runbook's 15. Never the seven-day window --
    that is not what the periodic breached on, and a verdict lasting a week
    outlives the day that earned it. A live queue is in the default because
    decide() posts no breach without one; `day` with `window_hours=None` is
    the fallback, for a stretch too thin for the periodic to judge."""
    note = {
        "since": since,
        "verdict": verdict,
        "measured_at": "2026-09-04T11:23:00+00:00",
        "day": None,
        "window_hours": 3,
        "p50_s": 22 * 60,
        "p95_s": 61 * 60,
        "waiting": 6,
        "over_threshold": 2,
        "threshold_p50_s": 15 * 60,
        "threshold_p95_s": 45 * 60,
        "free": 0,
        "total": 30,
        "cause": cause,
        "max_concurrency": 30,
    }
    if verdict == "STALE":
        # health.py strips every number from a stale note.
        note = {"since": since, "verdict": verdict, "measured_at": note["measured_at"]}
    return note | over


def pooled(wait_s=None, **note):
    doc = health()
    doc["pool"] = pool_note(**note)
    doc["metrics"]["queue_wait_p50_s"] = wait_s
    doc["metrics"]["queue_wait_read"] = True
    return doc


def cleared(wait_s=24):
    """The tick after an episode: the artifact was read, and the reading is
    fine. Distinct from a tick with no artifact at all, where the note also
    disappears but nothing has been learned."""
    doc = health()
    doc["metrics"]["queue_wait_p50_s"] = wait_s
    doc["metrics"]["queue_wait_read"] = True
    return doc


def outage(cases=TRIO, since="2026-09-08T09:00:00+00:00", prs=(1246, 1238, 608, 1150, 1226, 1275), issues=()):
    return health("OUTAGE", "shared fixture/environment break: " + ", ".join(cases), cases, since=since, prs=prs, runs=len(prs), issues=issues)


def storm(since="2026-09-03T18:30:00+00:00", window=("2026-09-03T17:15:00+00:00", "2026-09-03T18:25:00+00:00"), prs=(1182, 1167, 1188)):
    return health("DEGRADED", "quota storm window 17:15–18:25 UTC", since=since, condition="storm", prs=prs, runs=len(prs), window=window)


def deaths(since="2026-09-05T13:00:00+00:00", prs=(965, 1121, 1186, 1199)):
    return health("DEGRADED", "setup/clone failures on 4 runs", since=since, condition="setup_deaths", prs=prs, runs=4)


# The 2026-09-11 build-cluster event (#1478), as health.py reports it: the
# losses spanned 14:05:52-14:19:16Z (10:05-10:19 AM EDT) on five nodes.
NODES_0911 = {
    "gke-kube-agents-prow-default-pool-eb220b2a-6uhg": 1,
    "gke-kube-agents-prow-default-pool-eb220b2a-93sl": 2,
    "gke-kube-agents-prow-default-pool-eb220b2a-er33": 3,
    "gke-kube-agents-prow-default-pool-eb220b2a-pe72": 3,
    "gke-kube-agents-prow-default-pool-eb220b2a-sgnk": 3,
}
PRS_0911 = (926, 1118, 1246, 1258, 1319, 1351, 1362, 1439, 1451, 1456, 1460, 1471)


def lost_pods(since="2026-09-11T14:05:52+00:00", prs=PRS_0911, nodes=None, window=("2026-09-11T14:05:52+00:00", "2026-09-11T14:19:16+00:00"), evidence=()):
    nodes = NODES_0911 if nodes is None else nodes
    doc = health("DEGRADED", f"lost pods: {len(prs)} runs on {len(prs)} PRs died with their build node 14:05–14:19 UTC", since=since, condition="lost_pods", prs=prs, runs=len(prs), window=window)
    doc["incident"]["nodes"] = dict(nodes)
    doc["incident"]["event"] = len(prs) >= 8
    doc["evidence"] = list(evidence)
    return doc


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
            f"{URL}#since=2026-09-08T09:00:00Z&cases=cluster-agent-crashloop-debug,cluster-agent-crashloop-evidence-chain,cluster-agent-crashloop-misleading-symptom&view=gate",
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
            f"{URL}#since=2026-09-03T18:30:00Z&view=gate",
        )

    def test_setup_deaths(self):
        self.tick(deaths(), T0)
        self.assertEqual(
            self.opener.texts[0],
            "🟡 *Smoke gate: flaky* — 4 runs on 4 PRs died during setup since 9:00 AM ET.  Passing runs still count; if yours died before any test ran, retest.\n"
            f"{URL}#since=2026-09-05T13:00:00Z&view=gate",
        )

    def test_recovery(self):
        self.tick(outage(cases=TRIO, since="2026-09-07T14:00:00+00:00", issues=["#1269"]), T0.replace(day=7, hour=14))
        self.tick(health(since="2026-09-08T01:00:00+00:00"), T0.replace(day=8, hour=1))
        self.assertEqual(
            self.opener.texts[1],
            "🟢 *Smoke gate: healthy again* — fixed after 11h (the 3 crashloop tests were failing, #1269).\n"
            f"{URL}#since=2026-09-07T14:00:00Z&until=2026-09-08T01:00:00Z&cases=cluster-agent-crashloop-debug,cluster-agent-crashloop-evidence-chain,cluster-agent-crashloop-misleading-symptom&view=gate",
        )

    def test_recovery_from_a_storm(self):
        self.tick(storm(since="2026-09-04T09:47:00+00:00"), T0)
        self.tick(health(), T0.replace(hour=15, minute=30))
        self.assertEqual(self.opener.texts[1].split("\n")[0], "🟢 *Smoke gate: healthy again* — fixed after 5h 43m (quota storm).")

    def test_digest(self):
        self.tick(health(), self.at(DIGEST_UTC, 5))
        self.assertEqual(
            self.opener.texts[0],
            f"📊 *Smoke gate, last 24h:* 31 runs · 26 green · 2 PR-caused red · 5 infra · typical run 125 min · typical wait n/a\n{URL}#since=2026-09-04T03:30:00Z&view=agent",
        )

    def test_stale_and_fresh_again(self):
        doc = health()
        doc["stale"] = True
        doc["generated_at"] = "2026-09-04T05:55:40+00:00"
        self.tick(doc, T0)
        self.assertEqual(self.opener.texts[0], "⚪ *Smoke gate: no fresh data since 1:55 AM ET* — the health bot can't see recent runs. Someone check the refresh job.")
        self.tick(health(), T0.replace(minute=15))
        self.assertEqual(self.opener.texts[1], "⚪ *Smoke gate: fresh data again* — refreshed 8:00 AM ET; the gate reads GREEN.")

    def test_slow(self):
        self.tick(slow(), T0)
        self.assertEqual(
            self.opener.texts[0],
            "🐢 *Smoke gate: slow* — the last 5 full runs took 152–213 min (median 183) against a 7-day typical of 151 min (p90 198); 2 reps lost to 429s."
            " Not a break, and /retest won't make yours faster.\n"
            f"{URL}#view=agent",
        )
        self.assertEqual(self.recorded()["state"], "GREEN", "the note moves no state")

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
        self.assertIn("#since=2026-09-08T11:30:00Z&", self.opener.texts[0])
        self.assertTrue(self.opener.texts[0].endswith("&view=gate"))

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
            "📊 *Smoke gate, last 24h:* 31 runs · 26 green · 2 PR-caused red · 5 infra · typical run 125 min · typical wait n/a\n"
            "🌙 Nightly: 3 cases · 1 passed all reps · 1 partial · 1 failed · newly failing: case-c · 6h 40m\n"
            f"{post_health.NIGHTLY_URL}\n{URL}#since=2026-09-04T03:30:00Z&view=agent",
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

    def test_digest_prints_the_typical_wait_on_an_ordinary_day(self):
        # The point of the field: the wait is normally seconds, and a number
        # nobody sees on a quiet day is one nobody can read on a bad one.
        doc = health()
        doc["metrics"]["queue_wait_p50_s"] = 24
        self.tick(doc, self.at(DIGEST_UTC, 5))
        self.assertIn("typical run 125 min · typical wait 24s", self.opener.texts[0])

    def test_digest_prints_n_a_when_there_is_no_pool_artifact(self):
        self.tick(health(), self.at(DIGEST_UTC, 5))
        self.assertIn("typical wait n/a", self.opener.texts[0])

    def test_digest_carries_a_cause_free_pool_line_while_the_episode_lasts(self):
        self.tick(pooled(wait_s=22 * 60), T0.replace(hour=7))  # the note itself
        self.tick(pooled(wait_s=22 * 60), self.at(DIGEST_UTC, 5))
        self.assertEqual(
            self.opener.texts[1].split("\n")[1],
            "⏳ Queue backed up — last 3h: median wait 22 min against a 15 min limit;"
            " p95 61 min against 45.",
        )

    def test_the_digest_falls_back_to_the_worst_day_when_the_recent_stretch_is_too_thin(self):
        thin = {"day": "2026-09-03", "window_hours": None}
        self.tick(pooled(wait_s=22 * 60, **thin), T0.replace(hour=7))
        self.tick(pooled(wait_s=22 * 60, **thin), self.at(DIGEST_UTC, 5))
        self.assertEqual(
            self.opener.texts[1].split("\n")[1],
            "⏳ Queue backed up — worst day 2026-09-03: median wait 22 min against a 15 min limit;"
            " p95 61 min against 45.",
        )

    def test_the_digest_line_carries_the_half_that_breached(self):
        # A day breaches on p50 or p95, so the median alone can be a passing
        # number standing in as the reason the line is there at all.
        self.tick(pooled(wait_s=24, p50_s=24), T0.replace(hour=7))
        self.tick(pooled(wait_s=24, p50_s=24), self.at(DIGEST_UTC, 5))
        self.assertEqual(
            self.opener.texts[1].split("\n")[1],
            "⏳ Queue backed up — last 3h: median wait 24s against a 15 min limit;"
            " p95 61 min against 45.",
        )

    def test_digest_counts_the_queue_when_no_day_breached(self):
        live = {"day": None, "p50_s": None, "p95_s": None, "over_threshold": 2}
        self.tick(pooled(wait_s=24, **live), T0.replace(hour=7))
        self.tick(pooled(wait_s=24, **live), self.at(DIGEST_UTC, 5))
        self.assertEqual(
            self.opener.texts[1].split("\n")[1],
            "⏳ Queue backed up — 2 runs waiting past the 45 min p95 limit.",
        )

    def test_digest_says_the_numbers_stopped_rather_than_quoting_them(self):
        self.tick(pooled(verdict="STALE"), T0.replace(hour=7))
        self.tick(pooled(verdict="STALE"), self.at(DIGEST_UTC, 5))
        line = self.opener.texts[1].split("\n")[1]
        self.assertEqual(
            line,
            "⚪ No pool numbers since 7:23 AM ET — ci-kube-agents-pool-pressure has stopped reporting.",
        )
        self.assertIn("typical wait n/a", self.opener.texts[1])

    def test_digest_says_the_wait_is_unknown_without_reaching_for_a_number(self):
        # UNMEASURED is the window sweep failing, not the periodic dying, so it
        # is neither the stale line nor a breach with figures in it.
        self.tick(pooled(verdict="UNMEASURED"), T0.replace(hour=7))
        self.tick(pooled(verdict="UNMEASURED"), self.at(DIGEST_UTC, 5))
        self.assertEqual(
            self.opener.texts[1].split("\n")[1],
            "⚪ Queue wait unknown — the hourly pool check couldn't read how long recent runs waited.",
        )

    def test_digest_carries_the_slow_line_while_it_lasts(self):
        self.tick(slow(), T0.replace(hour=7))  # the note itself
        self.tick(slow(), self.at(DIGEST_UTC, 5))
        self.assertEqual(
            self.opener.texts[1].split("\n")[1],
            "🐢 Slow since 2:00 PM ET: the last 5 full runs took 152–213 min (median 183) against a 7-day typical of 151 min (p90 198); 2 reps lost to 429s.",
        )
        self.tick(health(), self.at(DIGEST_UTC, 5, day=5))
        self.assertEqual(len(self.opener.texts), 3, "clearing is not a message")
        self.assertNotIn("Slow since", self.opener.texts[2])


# --------------------------------------------------------------------------- #
# The slow note
# --------------------------------------------------------------------------- #


class SlowNote(RunHarness):
    def test_posted_once_per_episode_and_again_after_it_clears(self):
        # Morning ticks, clear of the 12:40-13:20Z digest window.
        self.tick(slow(), self.at(10))
        self.tick(slow(), self.at(10, 15))
        self.tick(slow(), self.at(10, 30))
        self.assertEqual(len(self.opener.requests), 1, "one note for the episode")
        self.assertTrue(self.recorded()["slow"])
        self.tick(health(), self.at(10, 45))
        self.assertEqual(len(self.opener.requests), 1, "clearing is not news")
        self.assertFalse(self.recorded()["slow"])
        self.tick(slow(infra_reps=0), self.at(15))
        self.assertEqual(len(self.opener.requests), 2, "a new episode is")
        self.assertIn("; no reps lost.", self.opener.texts[1])

    def test_a_failed_note_is_retried_next_tick(self):
        self.tick(health(), T0)
        rc, err = self.tick(slow(), T0.replace(minute=15), opener=FakeOpener(statuses=[500]))
        self.assertEqual(rc, 1)
        self.assertIn("failed to post: slow", err)
        self.assertFalse(self.recorded()["slow"], "not told yet")
        self.tick(slow(), T0.replace(minute=30))
        self.assertEqual(len(self.opener.requests), 1)
        self.assertTrue(self.recorded()["slow"])

    def test_a_note_that_vanishes_with_the_state_is_told_again_when_green_returns(self):
        # health.py computes the note for GREEN ticks only, so an incident
        # takes it away; the state file follows it down silently and the
        # note after the recovery is a new episode, told once more.
        self.tick(slow(), self.at(10))
        self.tick(outage(), self.at(10, 15))
        self.assertEqual([text.split(" ")[0] for text in self.opener.texts], ["🐢", "🔴"])
        self.assertEqual((self.recorded()["state"], self.recorded()["slow"]), ("OUTAGE", False))
        self.tick(slow(), self.at(10, 30))
        self.assertEqual([text.split(" ")[0] for text in self.opener.texts][2:], ["🟢", "🐢"])
        self.assertTrue(self.recorded()["slow"])


class PoolNote(RunHarness):
    """Rule 8 (#1607): one message per episode, one more when the verdict
    changes inside it, and a header that names the cause."""

    def first(self):
        return self.opener.texts[0].split("\n")

    def test_the_full_pool_message_asks_for_a_project_and_pairs_each_number_with_its_limit(self):
        self.tick(pooled(), self.at(10))
        lines = self.first()
        self.assertEqual(
            lines[0],
            "⏳ *Smoke gate: pool full* — all 30 projects are leased and runs are queuing. Consider onboarding a project.",
        )
        self.assertEqual(
            lines[1],
            "Last 3h: median wait 22 min against a 15 min limit; p95 61 min against 45.",
        )
        self.assertEqual(lines[2], "2 runs waiting right now, past the 45 min p95 limit.")
        self.assertEqual(lines[3], "Runs still pass; /retest makes the queue longer.")
        self.assertEqual(lines[4], f"{URL}#view=agent")

    def test_the_worst_breached_day_stands_in_when_the_recent_stretch_is_too_thin(self):
        # Under MIN_SAMPLES_FOR_RECENT_VERDICT the periodic withholds the
        # recent percentiles, and health.py sends the worst breached day
        # instead. The label has to change with them.
        self.tick(pooled(day="2026-09-03", window_hours=None), self.at(10))
        self.assertEqual(
            self.first()[1],
            "Worst day 2026-09-03: median wait 22 min against a 15 min limit; p95 61 min against 45.",
        )

    def test_a_breach_with_no_bad_day_counts_the_runs_queued_right_now(self):
        # One run stuck past p95 breaches a week with no bad day in it, and
        # then there is no day's median to print -- only the live queue.
        self.tick(pooled(day=None, p50_s=None, p95_s=None, over_threshold=3), self.at(10))
        lines = self.first()
        self.assertEqual(lines[1], "3 runs waiting right now, past the 45 min p95 limit.")
        self.assertEqual(lines[2], "Runs still pass; /retest makes the queue longer.")

    def test_a_breach_on_a_day_and_on_the_live_queue_reports_both(self):
        self.tick(pooled(over_threshold=1), self.at(10))
        lines = self.first()
        self.assertEqual(
            lines[1],
            "Last 3h: median wait 22 min against a 15 min limit; p95 61 min against 45.",
        )
        self.assertEqual(lines[2], "1 run waiting right now, past the 45 min p95 limit.")

    def test_the_cap_message_names_the_cap_and_the_pool_it_is_below(self):
        self.tick(pooled(cause="CONCURRENCY_CAP", free=4, max_concurrency=26), self.at(10))
        self.assertEqual(
            self.first()[0],
            "⏳ *Smoke gate: concurrency cap* — the pool has 30 projects but the concurrency cap is only 26. Raise the cap.",
        )

    def test_the_control_plane_message_states_the_cause_and_names_the_build_cluster(self):
        # No hedge: decide() posts this only while runs are waiting, and the
        # queue and the occupancy are read in one pass, so a free pool and a
        # backed-up queue describe one moment rather than two.
        self.tick(pooled(cause="CONTROL_PLANE", free=4), self.at(10))
        lines = self.first()
        self.assertEqual(
            lines[0],
            "⏳ *Smoke gate: runs not starting* — 4 of 30 projects"
            " were free while runs waited, so this is Prow rather than the pool.",
        )
        self.assertEqual(lines[1], "Check the build cluster: kube-agents-prow, project kube-agents-prow.")
        self.assertIn("median wait 22 min", lines[2])

    def test_an_unreadable_pool_asks_for_nothing(self):
        self.tick(pooled(cause="UNKNOWN"), self.at(10))
        self.assertEqual(
            self.first()[0],
            "⏳ *Smoke gate: queue backed up* — cause unclear: the job couldn't read how many projects were in use.",
        )

    def test_an_unrecognised_cause_falls_through_to_the_message_that_asks_for_nothing(self):
        # A cause pool_pressure.py adds later must not read as "onboard".
        self.tick(pooled(cause="SOMETHING_NEW"), self.at(10))
        self.assertIn("cause unclear", self.first()[0])

    def test_a_check_that_could_not_measure_is_white_and_carries_no_numbers(self):
        self.tick(pooled(verdict="UNMEASURED"), self.at(10))
        self.assertEqual(
            self.opener.texts[0],
            "⚪ *Smoke gate: wait unknown* — the hourly pool check ran but couldn't read how long"
            " recent runs waited."
            # Its own job's history: there is no number for the dashboard to
            # show, so every message in this family ends somewhere useful.
            f"\n{post_health.POOL_JOB_HISTORY_URL}",
        )

    def test_a_stopped_check_names_the_job_and_quotes_no_numbers(self):
        self.tick(pooled(verdict="STALE"), self.at(10))
        lines = self.first()
        self.assertEqual(
            lines[0],
            "⚪ *Smoke gate: pool check stopped* — last reading 7:23 AM ET;"
            " ci-kube-agents-pool-pressure runs hourly and has missed the last few."
            " If the next one doesn't land, it needs checking.",
        )
        self.assertEqual(lines[1], post_health.POOL_JOB_HISTORY_URL)
        # A reading hours old is not evidence about now, and a number in the
        # message gets read as current whatever the caveat says.
        self.assertNotIn("22 min", self.opener.texts[0])

    def test_a_crashed_check_has_no_last_reading_to_quote(self):
        # The workflow's sentinel: the copy worked and the body is not the
        # artifact, so health.py has no window_end. The job stopping and the
        # job publishing nothing read alike until the message says which.
        self.tick(pooled(verdict="STALE", measured_at=None), self.at(10))
        self.assertEqual(
            self.first()[0],
            "⚪ *Smoke gate: pool check stopped* — ci-kube-agents-pool-pressure ran but published"
            " no numbers. If the next one doesn't land, it needs checking.",
        )

    def test_posted_once_per_episode(self):
        self.tick(pooled(), self.at(10))
        self.tick(pooled(), self.at(10, 15))
        self.tick(pooled(), self.at(10, 30))
        self.assertEqual(len(self.opener.requests), 1)
        self.assertEqual(self.recorded()["pool_verdict"], "BREACH")

    def test_a_verdict_that_changes_inside_an_episode_is_told_again(self):
        # The episode's `since` carries across, so without this the periodic
        # dying mid-breach would go unsaid.
        self.tick(pooled(), self.at(10))
        self.tick(pooled(verdict="STALE"), self.at(10, 15))
        self.assertEqual([text.split(" ")[0] for text in self.opener.texts], ["⏳", "⚪"])
        self.assertEqual(self.recorded()["pool_verdict"], "STALE")

    def test_a_cause_that_changes_inside_an_episode_is_told_again(self):
        # The cause picks the remedy and is recomputed from live occupancy every
        # hour, so a week-long breach switches from "onboard a project" to
        # "raise the cap" with the verdict unchanged. Only this message carries
        # a remedy; the digest line and the Brief sentence are cause-free.
        self.tick(pooled(), self.at(10))
        self.tick(pooled(cause="CONCURRENCY_CAP"), self.at(11))
        self.assertEqual(len(self.opener.requests), 2)
        self.assertIn("pool full", self.opener.texts[0])
        self.assertIn("concurrency cap", self.opener.texts[1])
        self.assertEqual(self.recorded()["pool_causes"], ["CAPACITY", "CONCURRENCY_CAP"])

    def test_a_cause_the_episode_already_named_is_not_repeated(self):
        # Free projects cross zero repeatedly inside one episode, so CAPACITY
        # comes back within the hour. The second one says nothing the first did
        # not, and rule 8 is a once-per-episode note.
        self.tick(pooled(), self.at(10))
        self.tick(pooled(cause="CONCURRENCY_CAP"), self.at(11))
        self.tick(pooled(), self.at(12))
        self.assertEqual(len(self.opener.requests), 2)

    def test_a_cause_is_recorded_only_once_the_message_is_sent(self):
        # A failed send must be retried, so the remedy is not marked told.
        self.opener.statuses = [500]
        self.tick(pooled(), self.at(10))
        self.tick(pooled(), self.at(10, 15))
        self.assertEqual(len(self.opener.requests), 2, "retried")
        self.assertEqual(self.recorded()["pool_causes"], ["CAPACITY"])

    def test_a_new_episode_names_its_remedy_again(self):
        # The list is the episode's, not the channel's memory: the same cause a
        # month later is news.
        self.tick(pooled(), self.at(10))
        self.tick(cleared(), self.at(10, 15))
        self.assertEqual(self.recorded()["pool_causes"], [])
        self.tick(pooled(), self.at(10, 30))
        self.assertEqual([text.split(" ")[0] for text in self.opener.texts], ["⏳", "✅", "⏳"])

    def test_a_breach_that_goes_blind_before_it_drains_still_gets_its_clear(self):
        # The commonest way a long episode ends: the periodic dies, the note
        # goes ⚪, and the queue drains while nobody is measuring. Reading the
        # last verdict would owe this episode no ✅ at all.
        self.tick(pooled(), self.at(10))
        self.tick(pooled(verdict="STALE"), self.at(10, 15))
        self.tick(cleared(), self.at(10, 30))
        self.assertEqual([text.split(" ")[0] for text in self.opener.texts], ["⏳", "⚪", "✅"])
        self.assertFalse(self.recorded()["pool_breached"])

    def test_a_cleared_note_says_so_once_and_a_new_episode_is_its_own_message(self):
        # Rule 8 clears loudly where rule 7 stays quiet. The periodic judges a
        # rolling seven-day window, so an episode outlives the bad day by up
        # to a week and "it is over" is the news, not noise.
        self.tick(pooled(), self.at(10))
        self.tick(cleared(), self.at(10, 15))
        self.assertEqual(
            self.opener.texts[1],
            "✅ *Smoke gate: queue clear* — runs are starting on time again, typical wait 24s.",
        )
        self.assertIsNone(self.recorded()["pool_verdict"])
        self.tick(cleared(), self.at(10, 30))
        self.assertEqual(len(self.opener.requests), 2, "said once, like the note itself")
        self.tick(pooled(since="2026-09-04T14:30:00+00:00"), self.at(15))
        self.assertEqual(len(self.opener.requests), 3)

    def test_a_clear_on_a_day_with_no_runs_drops_the_wait_rather_than_printing_one(self):
        # queue_wait_p50_s is null on a day nothing concluded. "typical wait ?"
        # reads as a broken number; the news is that the episode is over.
        self.tick(pooled(), self.at(10))
        self.tick(cleared(wait_s=None), self.at(10, 15))
        self.assertEqual(
            self.opener.texts[1],
            "✅ *Smoke gate: queue clear* — runs are starting on time again.",
        )

    def test_the_poster_does_not_hold_the_episode_start(self):
        # It used to. A mute returns before write_state, so anything the state
        # file holds stands still while health.py keeps adjudicating -- and an
        # old episode's start would then be handed to a new one. health.json
        # is written every tick, muted or not, and carries it in
        # metrics.pool_since instead.
        self.tick(pooled(), self.at(10))
        self.assertNotIn("pool_since", self.recorded())

    def test_a_note_that_goes_with_the_artifact_clears_silently(self):
        # The note also disappears when the artifact does, and the bot losing
        # sight of the queue is not the queue recovering.
        self.tick(pooled(), self.at(10))
        self.tick(health(), self.at(10, 15))
        self.assertEqual(len(self.opener.requests), 1)
        # The episode survives the gap: forgetting it here would re-post the
        # same breach the tick the fetch recovers.
        self.assertEqual(self.recorded()["pool_verdict"], "BREACH")
        self.tick(pooled(), self.at(10, 30))
        self.assertEqual(len(self.opener.requests), 1, "still the same episode")

    def test_a_monitoring_episode_ends_without_claiming_a_recovery(self):
        # ⚪ never said the queue was bad, so "starting on time again" would
        # assert what nothing measured.
        self.tick(pooled(verdict="STALE"), self.at(10))
        self.tick(cleared(), self.at(10, 15))
        self.assertEqual(len(self.opener.requests), 1)
        self.assertIsNone(self.recorded()["pool_verdict"])

    def test_a_failed_clear_is_retried_next_tick(self):
        self.tick(pooled(), self.at(10))
        rc, err = self.tick(cleared(), self.at(10, 15), opener=FakeOpener(statuses=[500]))
        self.assertEqual(rc, 1)
        self.assertIn("failed to post: pool_clear", err)
        self.assertEqual(self.recorded()["pool_verdict"], "BREACH", "not told yet")
        self.tick(cleared(), self.at(10, 30))
        self.assertEqual(self.opener.texts[-1].split(" ")[0], "✅")
        self.assertIsNone(self.recorded()["pool_verdict"])

    def test_a_failed_note_is_retried_next_tick(self):
        self.tick(health(), self.at(9))
        rc, err = self.tick(pooled(), self.at(10), opener=FakeOpener(statuses=[500]))
        self.assertEqual(rc, 1)
        self.assertIn("failed to post: pool", err)
        self.assertIsNone(self.recorded()["pool_verdict"], "not told yet")
        self.tick(pooled(), self.at(10, 15))
        self.assertEqual(len(self.opener.requests), 1)
        self.assertEqual(self.recorded()["pool_verdict"], "BREACH")

    def test_the_note_rides_beside_an_incident_unlike_the_slow_one(self):
        # The one deliberate difference from rule 7: a different job reading
        # different data cannot be this incident's own symptom.
        doc = outage()
        doc["pool"] = pool_note()
        self.tick(doc, self.at(10))
        self.assertEqual([text.split(" ")[0] for text in self.opener.texts], ["🔴", "⏳"])

    def test_a_sub_minute_median_prints_seconds_not_zero_minutes(self):
        # The gate breaches on p50 or p95, so a p95-only breach carries an
        # ordinary median; whole minutes would render it "0 min".
        self.tick(pooled(p50_s=24), self.at(10))
        self.assertIn("median wait 24s against a 15 min limit", self.opener.texts[0])

    def test_a_breach_with_an_empty_queue_says_nothing(self):
        # The verdict spans seven days and the remedy is read live, so one bad
        # Monday keeps the verdict all week while the remedy tracks a pool that
        # has since drained. With nothing waiting there is no queue to explain.
        self.tick(pooled(waiting=0, over_threshold=0), self.at(10))
        self.assertEqual(len(self.opener.requests), 0)
        self.assertIsNone(self.recorded()["pool_verdict"])

    def test_the_same_breach_is_told_once_runs_are_waiting(self):
        self.tick(pooled(waiting=0, over_threshold=0), self.at(10))
        self.tick(pooled(over_threshold=2), self.at(11))
        self.assertEqual([text.split(" ")[0] for text in self.opener.texts], ["⏳"])
        self.assertEqual(self.recorded()["pool_verdict"], "BREACH")

    def test_a_cause_that_flips_over_an_empty_queue_says_nothing(self):
        # The Tuesday-3am case: the pool drained overnight, so cause() reads
        # CONTROL_PLANE off a free pool while Monday still holds the verdict.
        self.tick(pooled(), self.at(10))
        self.tick(pooled(cause="CONTROL_PLANE", free=25, waiting=0, over_threshold=0), self.at(11))
        self.assertEqual(len(self.opener.requests), 1)
        self.assertEqual(self.recorded()["pool_causes"], ["CAPACITY"])

    def test_a_backlog_under_the_p95_limit_is_still_told(self):
        # The incident this message is for: the pool full all afternoon, every
        # run waiting half an hour, the day's row breached on p50 and nothing
        # yet past 45 minutes. Gating on the p95 subset would keep it quiet.
        self.tick(pooled(waiting=9, over_threshold=0), self.at(10))
        self.assertEqual([text.split(" ")[0] for text in self.opener.texts], ["⏳"])

    def test_a_breach_is_told_when_the_queue_could_not_be_read(self):
        # Deck unread is not "nothing is waiting". Withholding on it would
        # silence a breach for as long as the read keeps failing.
        self.tick(pooled(waiting=None, over_threshold=0), self.at(10))
        self.assertEqual([text.split(" ")[0] for text in self.opener.texts], ["⏳"])

    def test_the_monitoring_verdicts_are_told_with_no_queue_behind_them(self):
        # Neither advises anything, so neither depends on runs waiting: the
        # news is that the periodic stopped answering.
        for verdict in ("STALE", "UNMEASURED"):
            with self.subTest(verdict=verdict):
                self.setUp()
                self.tick(pooled(verdict=verdict, waiting=0, over_threshold=0), self.at(10))
                self.assertEqual([text.split(" ")[0] for text in self.opener.texts], ["⚪"])

    def test_a_breach_that_returns_after_a_withheld_stretch_is_told(self):
        # Recording a withheld breach as told closes both triggers at once:
        # the verdict matches what the state says was said, and the cause was
        # named earlier in the episode. The breach would come back silently.
        self.tick(pooled(), self.at(10))
        self.tick(pooled(verdict="STALE"), self.at(10, 15))
        self.tick(pooled(waiting=0, over_threshold=0), self.at(10, 30))
        self.assertEqual(self.recorded()["pool_verdict"], "STALE", "nothing was said at 10:30")
        self.tick(pooled(), self.at(10, 45))
        self.assertEqual([text.split(" ")[0] for text in self.opener.texts], ["⏳", "⚪", "⏳"])

    def test_a_breach_nobody_was_told_about_is_not_owed_a_clear(self):
        # pool_breached is set on the send, not on the reading. Set it on the
        # reading and a week of withheld breaches would close with a ✅ ending
        # an episode the space never heard begin.
        self.tick(pooled(waiting=0, over_threshold=0), self.at(10))
        self.tick(cleared(), self.at(10, 15))
        self.assertEqual(len(self.opener.requests), 0)
        self.assertFalse(self.recorded()["pool_breached"])


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
        self.assertIn(f"Incident brief: {URL}#since=2026-09-08T11:30:00Z&cases=cost-idle-pool-probe,reliability-pdb-probe&view=gate", body["body"])
        self.assertTrue(body["body"].rstrip().endswith("Filed automatically by the smoke health bot; edit freely. Fix PRs: reference this issue."))
        self.assertEqual(self.opener.texts[0].split("\n")[1], "Don't retest yet. Tracking #1300.")
        recorded = self.recorded()
        self.assertEqual(recorded["issue"], {"number": 1300, "url": "https://github.com/gke-labs/kube-agents/issues/1300", "condition": "shared_break"})
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


class LostPods(RunHarness):
    """The build-cluster event of 2026-09-11 (#1478): the message names the
    nodes' count and the loss on the reader's clock, the cluster owner gets
    an issue once per event, a human's issue naming the nodes is adopted,
    and an outage's issue is never cited for it."""

    def environ(self):
        return {post_health.SPACE_ENV: SPACE, post_health.TOKEN_ENV: TOKEN, **GH_ENV}

    def test_a_build_cluster_event_message(self):
        self.tick(lost_pods(), T0.replace(day=11, hour=14, minute=30))
        self.assertEqual(
            self.opener.texts[0],
            "🟡 *Smoke gate: flaky* — the build cluster lost 5 nodes at 10:05 AM ET; 12 runs on 12 PRs died mid-run. Not your code; retest once new jobs are running.\n"
            f"{URL}#since=2026-09-11T14:05:52Z&view=gate",
        )

    def test_a_few_lost_pods_say_how_many_without_calling_it_an_event_and_still_file(self):
        doc = lost_pods(prs=(1, 2, 3), nodes={"gke-kube-agents-prow-default-pool-eb220b2a-er33": 3})
        self.tick(doc, T0.replace(day=11, hour=14, minute=30), environ=self.environ())
        self.assertEqual(self.opener.texts[0].split("\n")[0], "🟡 *Smoke gate: flaky* — 3 runs on 3 PRs died with their build node at 10:05 AM ET. Not your code; retest once new jobs are running. Tracking #1300.")
        # The cluster owner's issue is filed on any new lost_pods condition;
        # the 8-run bar changes only the wording.
        self.assertEqual(self.gh.writes(), [("POST", "repos/gke-labs/kube-agents/issues")])
        self.assertEqual(self.gh.calls[-1][2]["title"], "Build cluster lost node(s) gke-kube-agents-prow-default-pool-eb220b2a-er33 at Fri 10:05 AM ET: 3 smoke runs on 3 PRs died mid-run")

    def test_the_cluster_owner_gets_an_issue_once_and_the_message_cites_it(self):
        now = T0.replace(day=11, hour=14, minute=30)
        doc = lost_pods(evidence=["lost pods: 12 runs on 12 PRs died with their build node 14:05–14:19 UTC"])
        rc, _ = self.tick(doc, now, environ=self.environ())
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh.writes(), [("POST", "repos/gke-labs/kube-agents/issues")])
        _, _, body = self.gh.calls[-1]
        self.assertEqual(body["title"], "Build cluster lost node(s) gke-kube-agents-prow-default-pool-eb220b2a-{6uhg,93sl,er33,pe72,sgnk} at Fri 10:05 AM ET: 12 smoke runs on 12 PRs died mid-run")
        self.assertEqual(body["labels"], ["presubmit-gate"])
        text = body["body"]
        self.assertIn("lost the node(s) below at Fri 10:05 AM ET; 12 `pull-kube-agents-smoke-test` runs on 12 pull requests died mid-run. Each pod's last event is `NodeNotReady`, or the pod never uploaded a build log.", text)
        self.assertIn("- `gke-kube-agents-prow-default-pool-eb220b2a-6uhg` (1 run)\n- `gke-kube-agents-prow-default-pool-eb220b2a-93sl` (2 runs)\n- `gke-kube-agents-prow-default-pool-eb220b2a-er33` (3 runs)", text)
        self.assertIn("**Window:** 10:05 AM–10:19 AM ET (2026-09-11T14:05:52+00:00 – 2026-09-11T14:19:16+00:00).", text)
        self.assertIn("**Affected PRs:** #926, #1118, #1246, #1258, #1319, #1351, #1362, #1439, #1451, #1456, #1460, #1471.", text)
        self.assertIn("- lost pods: 12 runs on 12 PRs died with their build node 14:05–14:19 UTC", text)
        self.assertIn("**Advice for authors:** nothing about your change; `/retest` once new jobs are progressing.", text)
        self.assertIn(f"Incident brief: {URL}#since=2026-09-11T14:05:52Z&view=gate", text)
        self.assertTrue(text.rstrip().endswith("Filed automatically by the smoke health bot; the cluster owner should check the node events and autorepair; the bot will not close it."))
        self.assertTrue(self.opener.texts[0].split("\n")[0].endswith("retest once new jobs are running. Tracking #1300."))
        self.assertEqual(self.recorded()["issue"], {"number": 1300, "url": "https://github.com/gke-labs/kube-agents/issues/1300", "condition": "lost_pods"})
        # The same event on the next tick: nothing posted, nothing filed.
        self.tick(doc, now.replace(minute=45), environ=self.environ())
        self.assertEqual(len(self.gh.writes()), 1)
        self.assertEqual(len(self.opener.requests), 1)

    def test_a_human_issue_naming_the_nodes_is_adopted(self):
        human = {"number": 1478, "html_url": "https://github.com/gke-labs/kube-agents/issues/1478", "title": "Build-cluster nodes went NotReady", "body": "nodes gke-kube-agents-prow-default-pool-eb220b2a-pe72 and gke-kube-agents-prow-default-pool-eb220b2a-sgnk"}
        gh = FakeGh(open_issues=[human])
        doc = lost_pods(prs=(1, 2, 3, 4, 5, 6, 7, 8), nodes={"gke-kube-agents-prow-default-pool-eb220b2a-pe72": 5, "gke-kube-agents-prow-default-pool-eb220b2a-sgnk": 3})
        self.tick(doc, T0.replace(day=11, hour=14, minute=30), environ=self.environ(), gh=gh)
        self.assertEqual(gh.writes(), [], "nothing filed")
        self.assertIn("Tracking #1478.", self.opener.texts[0])
        self.assertEqual(self.recorded()["issue"]["condition"], "lost_pods")
        # An issue naming only one of the two nodes is not this event's.
        partial = FakeGh(open_issues=[dict(human, body="gke-kube-agents-prow-default-pool-eb220b2a-pe72 only")])
        self.state.unlink()
        self.tick(doc, T0.replace(day=11, hour=15), environ=self.environ(), gh=partial)
        self.assertEqual(partial.writes(), [("POST", "repos/gke-labs/kube-agents/issues")])

    def test_an_outage_issue_is_not_the_lost_pods_tracking_nor_the_reverse(self):
        self.tick(outage(cases=["a-probe"], since="2026-09-11T12:00:00+00:00", prs=(1, 2, 3)), T0.replace(day=11, hour=12), environ=self.environ())
        self.assertEqual(self.recorded()["issue"]["number"], 1300)
        # The break clears and the nodes go: DEGRADED lost_pods is a change,
        # and the outage's #1300 is not its tracking issue.
        self.tick(lost_pods(), T0.replace(day=11, hour=14, minute=30), environ=self.environ())
        self.assertEqual(self.gh.writes()[-1], ("POST", "repos/gke-labs/kube-agents/issues"))
        self.assertIn("Tracking #1301.", self.opener.texts[-1])
        recorded = self.recorded()
        self.assertEqual(recorded["issue"], {"number": 1301, "url": "https://github.com/gke-labs/kube-agents/issues/1301", "condition": "lost_pods"})
        self.assertEqual([issue["number"] for issue in recorded["issues"]], [1300, 1301], "the outage's issue still rides along")
        # And back up to an OUTAGE: #1301 is the cluster owner's and is not
        # cited; the episode's own outage issue #1300 still rides along and
        # is, so nothing new is filed.
        self.tick(outage(cases=["b-probe"], since="2026-09-11T15:00:00+00:00", prs=(4, 5, 6)), T0.replace(day=11, hour=15), environ=self.environ())
        self.assertIn("Tracking #1300.", self.opener.texts[-1])
        self.assertEqual(len([1 for method, path in self.gh.writes() if path == "repos/gke-labs/kube-agents/issues"]), 2, "two issues in the episode, no third")
        # GREEN: every issue of the episode gets the recovery comment, and
        # the message names them all.
        self.tick(health(), T0.replace(day=11, hour=17), environ=self.environ())
        recovered = [path for method, path in self.gh.writes() if method == "POST" and path.endswith("/comments")]
        self.assertEqual(recovered, [f"repos/gke-labs/kube-agents/issues/{n}/comments" for n in (1300, 1301)])
        self.assertIn("#1300, #1301)", self.opener.texts[-1].split("\n")[0])
        self.assertEqual((self.recorded()["issue"], self.recorded()["issues"]), (None, []))

    def test_an_outage_that_decays_into_a_storm_still_gets_its_recovery_comment(self):
        # The 2026-09-02 shape: the break clears, the storm is what is left,
        # then GREEN. The outage's issue is not the storm's tracking, but it
        # rides in the state until the recovery comments on it.
        self.tick(outage(cases=["a-probe"], since="2026-09-04T09:00:00+00:00", prs=(1, 2, 3)), T0, environ=self.environ())
        self.tick(storm(since="2026-09-04T13:00:00+00:00"), T0.replace(hour=13), environ=self.environ())
        self.assertNotIn("Tracking", self.opener.texts[-1])
        self.assertEqual((self.recorded()["issue"], [i["number"] for i in self.recorded()["issues"]]), (None, [1300]))
        self.tick(health(), T0.replace(hour=15, minute=30), environ=self.environ())
        self.assertEqual(self.gh.writes()[-1], ("POST", "repos/gke-labs/kube-agents/issues/1300/comments"))
        self.assertEqual(self.gh.calls[-1][2], {"body": "Healthy again after 2h 30m; bot will not close it."})
        self.assertEqual(self.opener.texts[-1].split("\n")[0], "🟢 *Smoke gate: healthy again* — fixed after 2h 30m (quota storm, #1300).")
        # A state file from before `issues` existed: its lone `issue` is
        # the episode's, and gets the comment too.
        self.state.write_text(json.dumps(dict(self.recorded(), state="DEGRADED", condition="storm", since="2026-09-04T13:00:00+00:00", issue={"number": 1200, "url": "x"}, issues=None)))
        self.tick(health(), T0.replace(hour=18), environ=self.environ())
        self.assertEqual(self.gh.writes()[-1], ("POST", "repos/gke-labs/kube-agents/issues/1200/comments"))

    def test_the_recovery_names_the_lost_nodes_and_the_issue(self):
        self.tick(lost_pods(), T0.replace(day=11, hour=14, minute=30), environ=self.environ())
        self.tick(health(), T0.replace(day=11, hour=16), environ=self.environ())
        # `since` is the first loss, 14:05:52Z; the recovery tick is 16:00Z.
        self.assertEqual(self.opener.texts[1].split("\n")[0], "🟢 *Smoke gate: healthy again* — fixed after 1h 54m (the build cluster lost nodes, #1300).")
        self.assertEqual(self.gh.calls[-1][2], {"body": "Healthy again after 1h 54m; bot will not close it."})
        self.assertIsNone(self.recorded()["issue"])

    def test_dry_run_prints_the_cluster_owner_issue_instead_of_filing_it(self):
        rc, err = self.tick(lost_pods(), T0.replace(day=11, hour=14, minute=30), environ={}, dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh.writes(), [])
        self.assertIn("--dry-run: would POST repos/gke-labs/kube-agents/issues", err)
        self.assertIn("Build cluster lost node(s) gke-kube-agents-prow-default-pool-eb220b2a-{6uhg,93sl,er33,pe72,sgnk} at Fri 10:05 AM ET", err)

    def test_the_title_compacts_the_node_names_and_stays_under_githubs_limit(self):
        compact = post_health.gate_issue.compact_nodes
        self.assertEqual(compact([]), "(node name not recorded)")
        self.assertEqual(compact(["gke-a-b-1"]), "gke-a-b-1")
        self.assertEqual(compact(["gke-pool-1-aaaa", "gke-pool-1-bbbb"]), "gke-pool-1-{aaaa,bbbb}")
        self.assertEqual(compact(["gke-pool-1-aaaa", "gke-pool-2-bbbb"]), "gke-pool-{1-aaaa,2-bbbb}")
        self.assertEqual(compact(["alpha", "beta"]), "alpha, beta")
        many = {f"gke-kube-agents-prow-pool-{i:02d}-node-{i:04d}": 1 for i in range(12)}
        title = post_health.gate_issue.render_lost_pods_title(lost_pods(nodes=many), "Fri 10:05 AM ET")
        self.assertLessEqual(len(title), post_health.gate_issue.TITLE_MAX_CHARS)
        self.assertTrue(title.startswith("Build cluster lost 12 nodes at Fri 10:05 AM ET: 12 smoke runs"), title)


# --------------------------------------------------------------------------- #
# Deep links
# --------------------------------------------------------------------------- #


class LinkBuilders(unittest.TestCase):
    """post_health.dashboard_link and run_link are the one Python writer of
    the dashboard's URL contract (SCHEMA.md, "URL contract"): every
    parameter in the fragment, none in the query, so the scope survives
    the published host's login redirect."""

    SINCE = datetime(2026, 9, 11, 14, 42, 37, tzinfo=timezone.utc)
    UNTIL = datetime(2026, 9, 11, 16, 50, 9, tzinfo=timezone.utc)

    def test_every_parameter_is_in_the_fragment_and_the_query_is_empty(self):
        link = post_health.dashboard_link(post_health.DASHBOARD_VIEW_GATE, ["a-probe", "b-probe"], self.SINCE, self.UNTIL)
        self.assertEqual(link, f"{URL}#since=2026-09-11T14:42:37Z&until=2026-09-11T16:50:09Z&cases=a-probe,b-probe&view=gate")
        parts = urllib.parse.urlsplit(link)
        self.assertEqual(parts.query, "", "nothing rides in the query string")
        self.assertEqual(
            urllib.parse.parse_qs(parts.fragment),
            {"since": ["2026-09-11T14:42:37Z"], "until": ["2026-09-11T16:50:09Z"], "cases": ["a-probe,b-probe"], "view": ["gate"]},
            "the fragment parses as key=value pairs, the way URLSearchParams reads it",
        )

    def test_empty_parameters_are_omitted_and_view_is_always_last(self):
        self.assertEqual(post_health.dashboard_link(post_health.DASHBOARD_VIEW_AGENT), f"{URL}#view=agent")
        self.assertEqual(post_health.dashboard_link(post_health.DASHBOARD_VIEW_GATE, [], self.SINCE), f"{URL}#since=2026-09-11T14:42:37Z&view=gate")
        self.assertEqual(post_health.dashboard_link(post_health.DASHBOARD_VIEW_GATE, ["a-probe"]), f"{URL}#cases=a-probe&view=gate")

    def test_incident_link_reads_the_health_document(self):
        doc = outage(cases=["a-probe"], since="2026-09-11T14:42:37+00:00")
        self.assertEqual(post_health.incident_link(doc), f"{URL}#since=2026-09-11T14:42:37Z&cases=a-probe&view=gate")
        self.assertEqual(post_health.incident_link(doc, self.UNTIL), f"{URL}#since=2026-09-11T14:42:37Z&until=2026-09-11T16:50:09Z&cases=a-probe&view=gate")

    def test_run_link_is_the_pr_view_beside_the_brief(self):
        self.assertEqual(post_health.run_link("2097282860221206528"), "https://storage.cloud.google.com/kube-agents-dashboards/evals/run.html#build=2097282860221206528")
        self.assertEqual(post_health.DASHBOARD_SITE + "/index.html", URL)


class DeepLinks(RunHarness):
    """Every message but the stale notice ends with the dashboard deep link the
    dashboard understands: the whole scope in the fragment, literal commas
    and colons, view=gate for an incident, view=agent for the digest."""

    def last_line(self, index=-1):
        return self.opener.texts[index].split("\n")[-1]

    def test_a_state_change_links_the_cases_and_the_start(self):
        self.tick(outage(cases=["cluster-agent-crashloop-debug", "cluster-agent-crashloop-evidence-chain"], since="2026-09-08T03:08:00+00:00"), T0)
        self.assertEqual(self.last_line(), f"{URL}#since=2026-09-08T03:08:00Z&cases=cluster-agent-crashloop-debug,cluster-agent-crashloop-evidence-chain&view=gate")

    def test_a_storm_links_the_start_without_cases(self):
        self.tick(storm(since="2026-09-04T18:30:00+00:00"), T0)
        self.assertEqual(self.last_line(), f"{URL}#since=2026-09-04T18:30:00Z&view=gate")

    def test_a_recovery_closes_the_incident_with_until(self):
        self.tick(outage(cases=["x-probe"], since="2026-09-04T03:08:00+00:00", prs=(1, 2, 3)), T0)
        self.tick(health(since="2026-09-04T14:00:00+00:00"), T0.replace(hour=14))
        self.assertEqual(self.last_line(), f"{URL}#since=2026-09-04T03:08:00Z&until=2026-09-04T14:00:00Z&cases=x-probe&view=gate")

    def test_the_digest_links_the_agent_view(self):
        self.tick(health(since="2026-09-04T03:30:00+00:00"), self.at(DIGEST_UTC, 5))
        self.assertEqual(self.last_line(), f"{URL}#since=2026-09-04T03:30:00Z&view=agent")

    def test_no_message_carries_a_query_string(self):
        self.tick(outage(cases=["x-probe"], since="2026-09-04T03:08:00+00:00", prs=(1, 2, 3)), T0)
        self.tick(health(since="2026-09-04T14:00:00+00:00"), self.at(DIGEST_UTC, 5))
        self.assertEqual(len(self.opener.texts), 3, "the change, the recovery and the digest")
        for text in self.opener.texts:
            self.assertEqual(urllib.parse.urlsplit(text.split("\n")[-1]).query, "", text)

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


# --------------------------------------------------------------------------- #
# Fixture drift (#1550): the hourly seeded-fleet scan's condition
# --------------------------------------------------------------------------- #

# 2026-09-14 (a Monday) 13:00Z is 9:00 AM EDT: the scan that fired.
SCAN_AT = "2026-09-14T13:00:00+00:00"
DRIFT_ROLE = "crashloop-workload"
DRIFT_PROJECTS = ("kube-agents-evals-1", "kube-agents-evals-2", "kube-agents-evals-3")
DRIFT_DETAIL = "pod?app=payments-api status.containerStatuses[*].restartCount any_ge 1: observed 0"
BLIND_REASON = "cannot mint a token as seeded-fleet-reader@kube-agents-evals-1.iam.gserviceaccount.com (is roles/iam.serviceAccountTokenCreator granted to the bot?): ERROR: PERMISSION_DENIED"
T14 = datetime(2026, 9, 14, 13, 30, tzinfo=timezone.utc)  # 9:30 AM EDT, outside the digest window


def fixture_block(drifted=None, scanned=SCAN_AT, projects=30, checked=30, unknown=False, stale=False, reason=None):
    return {"scanned_at": scanned, "projects": projects, "checked": checked, "drifted": drifted or {}, "unknown": unknown, "stale": stale, "reason": reason}


def fixture_drift(since=SCAN_AT, roles=(DRIFT_ROLE,), projects=DRIFT_PROJECTS, evidence=()):
    doc = health("DEGRADED", f"seeded fixture drift: {', '.join(roles)} out of designed state on {len(projects)} pool project(s)", since=since, condition="fixture_drift", window=(since, None))
    doc["incident"].update({"roles": list(roles), "projects": list(projects), "drift": {p: {r: [DRIFT_DETAIL] for r in roles} for p in projects}})
    doc["evidence"] = list(evidence)
    doc["fixture_state"] = fixture_block(drifted={p: list(roles) for p in projects})
    return doc


class FixtureDrift(RunHarness):
    """The seeded-fleet scan's condition (#1550): one message naming the roles
    and the projects, the fleet owner's issue once, a human's issue naming the
    roles adopted, the digest's line on the latest scan, and a blind scan said
    once each way and never filed."""

    def environ(self):
        return {post_health.SPACE_ENV: SPACE, post_health.TOKEN_ENV: TOKEN, **GH_ENV}

    def test_a_new_drift_condition_posts_once_and_files_the_fleet_owner_issue(self):
        rc, err = self.tick(fixture_drift(evidence=[f"{DRIFT_ROLE} drifted on 3 pool project(s) (kube-agents-evals-1, kube-agents-evals-2, kube-agents-evals-3) at the 13:00 UTC scan"]), T14, environ=self.environ())
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            self.opener.texts,
            [
                (
                    "🟡 *Smoke gate: flaky* — seeded fixture crashloop-workload out of designed state on 3 pool projects since 9:00 AM ET;"
                    " a red on a case that depends on it from a run that leased one of those projects is the fixture, not the code. Retest once the fleet is re-applied."
                    " Fleet owner: re-apply bench/tf/fleet in the projects named. Tracking #1300.\n"
                    f"{post_health.DASHBOARD_URL}#since=2026-09-14T13:00:00Z&view=gate"
                )
            ],
        )
        method, path, body = self.gh.calls[-1]
        self.assertEqual((method, path), ("POST", "repos/gke-labs/kube-agents/issues"))
        self.assertEqual(body["title"], "Seeded fleet drift: crashloop-workload out of designed state on 3 pool projects since Mon 9:00 AM ET")
        self.assertEqual(body["labels"], ["presubmit-gate"])
        for expected in ("- `crashloop-workload`", "- `kube-agents-evals-2`", f"    - {DRIFT_DETAIL}", "re-apply `bench/tf/fleet`", "the fleet owner should re-apply the stack", "latest scan 2026-09-14T13:00:00+00:00", "at the 13:00 UTC scan"):
            self.assertIn(expected, body["body"])
        self.assertEqual(self.recorded()["issue"], {"number": 1300, "url": "https://github.com/gke-labs/kube-agents/issues/1300", "condition": "fixture_drift"})
        # The same condition next tick is silence.
        self.tick(fixture_drift(), T14 + timedelta(minutes=15), environ=self.environ())
        self.assertEqual(len(self.opener.texts), 1)
        self.assertEqual(len(self.gh.writes()), 1)

    def test_a_human_issue_naming_the_roles_is_adopted(self):
        gh = FakeGh(open_issues=[{"number": 1290, "html_url": "https://github.com/gke-labs/kube-agents/issues/1290", "title": "crashloop-workload sits Pending on the fleet again", "body": ""}])
        self.tick(fixture_drift(), T14, environ=self.environ(), gh=gh)
        self.assertTrue(self.opener.texts[0].endswith(f"Tracking #1290.\n{post_health.DASHBOARD_URL}#since=2026-09-14T13:00:00Z&view=gate"))
        self.assertEqual(gh.writes(), [])
        self.assertEqual(self.recorded()["issue"]["condition"], "fixture_drift")

    def test_the_recovery_says_the_fixtures_had_drifted_and_comments(self):
        self.tick(fixture_drift(), T14, environ=self.environ())
        later = T14 + timedelta(hours=2)
        green = health("GREEN")
        green["fixture_state"] = fixture_block()
        self.tick(green, later, environ=self.environ())
        self.assertEqual(self.opener.texts[-1].splitlines()[0], "🟢 *Smoke gate: healthy again* — fixed after 2h 30m (seeded fixtures had drifted, #1300).")
        self.assertEqual(self.gh.writes()[-1], ("POST", "repos/gke-labs/kube-agents/issues/1300/comments"))
        self.assertIsNone(self.recorded()["issue"])

    def test_the_cause_sentence_the_gate_comment_box_carries(self):
        self.assertEqual(
            post_health.cause_sentence(fixture_drift()),
            "seeded fixture crashloop-workload out of designed state on 3 pool projects since 9:00 AM ET; a red on a case that depends on it from a run that leased one of those projects is the fixture, not the code.",
        )
        two = fixture_drift(roles=(DRIFT_ROLE, "no-pdb-workload"), projects=("kube-agents-evals-1",))
        self.assertEqual(
            post_health.cause_sentence(two),
            "seeded fixtures crashloop-workload, no-pdb-workload out of designed state on 1 pool project since 9:00 AM ET; a red on a case that depends on them from a run that leased one of those projects is the fixture, not the code.",
        )

    def test_the_digest_carries_one_line_on_the_latest_scan(self):
        def line(block):
            doc = health("GREEN")
            if block is not None:
                doc["fixture_state"] = block
            return [text for text in post_health.render_digest(doc, T14).splitlines() if text.startswith("🧭")]

        self.assertEqual(line(None), [], "before the scan has ever published, no line")
        self.assertEqual(line(fixture_block()), ["🧭 *Seeded fleet:* 30 of 30 pool projects checked at 9:00 AM ET, every fixture in its designed state."])
        self.assertEqual(line(fixture_block(checked=28)), ["🧭 *Seeded fleet:* 28 of 30 pool projects checked at 9:00 AM ET, every fixture in its designed state, 2 not checked."])
        self.assertEqual(
            line(fixture_block(drifted={"kube-agents-evals-1": [DRIFT_ROLE], "kube-agents-evals-4": [DRIFT_ROLE, "no-pdb-workload"]})),
            ["🧭 *Seeded fleet:* 2 of 30 checked pool projects drifted at 9:00 AM ET (crashloop-workload, no-pdb-workload); a red on a case that depends on them there is the fixture, not the code."],
        )
        self.assertEqual(line(fixture_block(checked=0, unknown=True, reason=BLIND_REASON)), [f"🧭 *Seeded fleet:* the 9:00 AM ET scan could check none of 30 pool projects ({BLIND_REASON})."])
        self.assertEqual(line(fixture_block(stale=True)), ["🧭 *Seeded fleet:* the last scan (9:00 AM ET) is stale; someone check the scan job."])

    def test_a_blind_scan_is_said_once_each_way_and_is_never_a_change(self):
        blind = health("GREEN")
        blind["fixture_state"] = fixture_block(checked=0, unknown=True, reason=BLIND_REASON)
        self.tick(blind, T14, environ=self.environ())
        self.assertEqual(
            self.opener.texts,
            [f"⚪ *Seeded-fleet scan can't see the fleet* — the 9:00 AM ET scan checked none of 30 pool projects ({BLIND_REASON}). Fixture drift goes unseen until that is fixed; the bot's grant is in docs/ci-health.md."],
        )
        self.assertEqual((self.recorded()["state"], self.recorded()["fixture_unknown"]), ("GREEN", True))
        self.tick(blind, T14 + timedelta(minutes=15))
        self.assertEqual(len(self.opener.texts), 1, "said once")
        seeing = health("GREEN")
        seeing["fixture_state"] = fixture_block(scanned="2026-09-14T14:00:00+00:00")
        self.tick(seeing, T14 + timedelta(hours=1))
        self.assertEqual(self.opener.texts[-1], "⚪ *Seeded-fleet scan sees the fleet again* — the 10:00 AM ET scan checked 30 of 30 pool projects.")
        self.assertFalse(self.recorded()["fixture_unknown"])
        self.assertEqual(self.gh.writes(), [], "nothing is filed for a blind scan")

    def test_a_health_json_without_the_block_changes_nothing(self):
        self.tick(health("GREEN"), T14)
        self.assertEqual(self.opener.texts, [])
        self.assertFalse(self.recorded()["fixture_unknown"])


if __name__ == "__main__":
    unittest.main()
