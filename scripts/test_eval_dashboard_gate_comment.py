"""gate_comment.py comments once per red run, in the words the mock approved
(dash-mocks/mock-n-comment.html): an outage the run matches, a failure only
this PR has, a mix, a hard failure; nothing on a green, aborted or setup-dead
run; an edit rather than a second comment; and a post failure that is a
warning and a retry, never a failed job.

GitHub is a recording fake `gh` runner; the state file is a temp path. The
per-case classes come from the real classify.py.
"""

import contextlib
import io
import json
import pathlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from eval_dashboard import gate_comment, health

UTC = timezone.utc
NOW = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)  # 11:00 AM EDT, a Tuesday
TRIO = ["cluster-agent-crashloop-debug", "cluster-agent-crashloop-evidence-chain", "cluster-agent-crashloop-misleading-symptom"]
ADMITTED = frozenset(TRIO + ["security-overgrant-probe", "reliability-pdb-probe", "agent-kanban-smoke"])
HELD_OUT = "autoops-warning-event-triage"
ROSTER = health.Roster.fixed(ADMITTED)
GRADED_FAIL = "VerificationCorrectness=0.0 (floor 1.0) -- sandbox pod never reached Running (ImagePullBackOff: agent-sandbox:pr-913)"
EMPTY = "the record shows no agent ever ran: the trajectory is empty"

REP = {
    "p": {"result": "pass", "reason": None},
    "f": {"result": "fail", "reason": GRADED_FAIL},
    "e": {"result": "fail", "reason": EMPTY},
    "i": {"result": "infra", "reason": "KUBE_AGENTS_INFRA_FAILURE"},
}


def task(name, letters):
    return {"name": name, "result": "pass" if set(letters) == {"p"} else "fail", "reps": [dict(REP[c], n=i + 1) for i, c in enumerate(letters)]}


def run(build, pr, finished, failing=(), result=None, minutes=132, tasks=None, project="kube-agents-evals-23"):
    if tasks is None:
        tasks = [task(name, "fff" if name in failing else "ppp") for name in sorted(ADMITTED)] + [task(HELD_OUT, "ppp")]
    if result is None:
        result = "FAILURE" if any(t["result"] == "fail" and t["name"] in ADMITTED for t in tasks) else "SUCCESS"
    return {
        "build_id": str(build),
        "pr": pr,
        "project": project,
        "started": (finished - timedelta(minutes=minutes)).isoformat(),
        "finished": finished.isoformat(),
        "result": result,
        "duration_s": minutes * 60,
        "tasks": tasks,
    }


def data(*runs, generated_at=NOW):
    return {"schema_version": 1, "generated_at": generated_at.isoformat(), "runs": list(runs)}


def green_health():
    return {"state": "GREEN", "condition": None, "since": "2026-09-07T20:00:00+00:00", "failing_cases": [], "tracking_issues": [], "incident": None}


def outage_health(cases=TRIO, prs=(1246, 1238, 608, 1150, 1226, 1275, 1290)):
    return {
        "state": "OUTAGE",
        "condition": "shared_break",
        "since": "2026-09-06T11:30:00+00:00",  # Sun 7:30 AM EDT
        "failing_cases": list(cases),
        "tracking_issues": ["#1278"],
        "incident": {"prs": list(prs), "runs": len(prs), "window_start": None, "window_end": None},
    }


def other_runs(failing=TRIO, count=7, start_pr=2000):
    """`count` other PRs' runs over the last few hours, each collapsing
    `failing`; all older than the first tick's one-hour lookback plus the
    30-minute scan overlap, so they are context, not runs to comment on."""
    return [run(9000 + i, start_pr + i, NOW - timedelta(minutes=100 + 20 * i), failing=failing) for i in range(count)]


def green_others(count=9, start_pr=3000):
    return [run(8000 + i, start_pr + i, NOW - timedelta(minutes=30 * (i + 1))) for i in range(count)]


class Result:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


class FakeGh:
    def __init__(self, existing=None, fail_writes=False):
        self.calls = []
        self.existing = dict(existing or {})  # pr -> [comment dicts]
        self.fail_writes = fail_writes
        self.next_id = 500

    def __call__(self, argv, input=None, **kwargs):
        method, path = argv[argv.index("-X") + 1], argv[argv.index("-X") + 2]
        body = json.loads(input) if input else None
        self.calls.append((method, path, body))
        if method == "GET":
            pr = int(path.split("/issues/")[1].split("/")[0])
            return Result(stdout="\n".join(json.dumps(c) for c in self.existing.get(pr, [])))
        if self.fail_writes:
            return Result(rc=1, stderr="gh: HTTP 403 Resource not accessible by integration")
        if method == "PATCH":
            return Result(stdout=json.dumps({"id": int(path.rsplit("/", 1)[1])}))
        self.next_id += 1
        return Result(stdout=json.dumps({"id": self.next_id}))

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != "GET"]

    def bodies(self):
        return [b["body"] for m, _, b in self.calls if m != "GET"]


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = pathlib.Path(self.tmp.name)
        self.state = self.dir / "gate-comment-state.json"
        self.gh = FakeGh()

    def tick(self, doc, health_doc, now=NOW, gh=None, dry_run=False):
        """`now=None` leaves --now off, so data.json's generated_at is the clock."""
        (self.dir / "data.json").write_text(json.dumps(doc))
        (self.dir / "health.json").write_text(json.dumps(health_doc))
        argv = ["--data", str(self.dir / "data.json"), "--health", str(self.dir / "health.json"), "--state", str(self.state), "--admitted", ",".join(sorted(ADMITTED))]
        if now is not None:
            argv += ["--now", now.isoformat()]
        if dry_run:
            argv.append("--dry-run")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = gate_comment.main(argv, gh_runner=gh or self.gh)
        return rc, err.getvalue()

    def recorded(self):
        return json.loads(self.state.read_text())


class Shapes(Harness):
    def test_outage_match(self):
        mine = run(100, 1300, NOW - timedelta(minutes=5), failing=TRIO)
        rc, _ = self.tick(data(mine, *other_runs()), outage_health())
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh.writes(), [("POST", "repos/gke-labs/kube-agents/issues/1300/comments")])
        body = self.gh.bodies()[0]
        lines = body.split("\n")
        self.assertEqual(lines[0], gate_comment.MARKER)
        self.assertEqual(lines[1], "### ❌ Smoke gate: failed · 3 of 7 cases")
        self.assertEqual(
            lines[3],
            "> 🔴 **Gate outage in progress** since Sun 7:30 AM ET. The 3 crashloop tests fail on every PR (7 PRs so far)."
            " **Your 3 failures are exactly those 3, so this red is not your code.** Don't retest yet; run `/retest` once #kube-agents-ci-health says the gate is healthy again."
            " [Why this run failed →](https://storage.cloud.google.com/kube-agents-dashboards/evals/run.html?build=100)"
            " · [Incident brief →](https://storage.cloud.google.com/kube-agents-dashboards/evals/index.html?cases=cluster-agent-crashloop-debug,cluster-agent-crashloop-evidence-chain,cluster-agent-crashloop-misleading-symptom&since=2026-09-06T11:30:00Z#gate)",
        )
        self.assertIn("| Case | Result | Also failing on |", body)
        self.assertIn("| `cluster-agent-crashloop-debug` | 0 / 3 reps | 7 other PRs |", body)
        self.assertNotIn("Reason:", body, "no check reason for the gate's failures")
        self.assertTrue(
            body.rstrip().endswith(
                "4 cases passed. Run 132 min on evals-23 · [build log](https://oss.gprow.dev/view/gs/kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/1300/pull-kube-agents-smoke-test/100)"
            ),
            body,
        )
        for word in ("rung", "threshold", "UTC"):
            self.assertNotIn(word, body)
        self.assertEqual(self.recorded()["comments"]["1300"], {"comment_id": 501, "build_id": "100", "at": NOW.isoformat()})

    def test_pr_caused(self):
        mine = [run(100 + i, 913, NOW - timedelta(hours=3 - i), failing=["security-overgrant-probe"]) for i in range(4)]
        rc, _ = self.tick(data(*mine, *green_others()), green_health())
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.gh.writes()), 1, "the newest red per PR")
        body = self.gh.bodies()[0]
        lines = body.split("\n")
        self.assertEqual(lines[1], "### ❌ Smoke gate: failed · 1 of 7 cases")
        self.assertEqual(
            lines[3],
            "> 🟢 **Gate healthy.** `security-overgrant-probe` passed on the last 9 runs from other PRs and failed on your last 4. **This looks specific to your PR.**"
            " [Why this run failed →](https://storage.cloud.google.com/kube-agents-dashboards/evals/run.html?build=103)",
        )
        self.assertIn("| `security-overgrant-probe` | 0 / 3 reps | no other PR |", body)
        self.assertIn("Reason: `", body)
        self.assertIn("sandbox pod never reached Running (ImagePullBackOff: agent-sandbox:pr-913)", body)
        self.assertNotIn("Incident brief", body)
        self.assertIn("6 cases passed.", body)

    def test_mixed(self):
        mine = run(100, 1300, NOW - timedelta(minutes=5), failing=TRIO + ["agent-kanban-smoke"])
        self.tick(data(mine, *other_runs()), outage_health())
        box = self.gh.bodies()[0].split("\n")[3]
        self.assertIn("3 of your 4 failures are the gate's (`cluster-agent-crashloop-debug`, `cluster-agent-crashloop-evidence-chain` and `cluster-agent-crashloop-misleading-symptom`)", box)
        self.assertIn("1 (`agent-kanban-smoke`) looks specific to your PR", box)
        self.assertIn("Fix that one; don't retest for the rest until the gate is healthy.", box)
        self.assertIn("· [Incident brief →]", box)
        self.assertIn("Reason: `", self.gh.bodies()[0])

    def test_hard_failure(self):
        # FAILURE with every gate case passing: an absolute check tripped.
        tasks = [task(name, "ppp") for name in sorted(ADMITTED)] + [task(HELD_OUT, "fff")]
        mine = run(100, 1300, NOW - timedelta(minutes=5), tasks=tasks, result="FAILURE")
        self.tick(data(mine, *green_others()), green_health())
        body = self.gh.bodies()[0]
        lines = body.split("\n")
        self.assertEqual(lines[1], "### ❌ Smoke gate: failed · hard failure")
        self.assertTrue(lines[3].startswith("> 🔴 **The run failed without a gate case failing all of its repetitions.**"), lines[3])
        self.assertIn("1 held-out case also failed; held-out cases do not block.", lines[3])
        self.assertIn(f"| `{HELD_OUT}` (held out) | 0 / 3 reps |", body)
        self.assertIn("6 cases passed.", body)

    def test_degraded_storm_match(self):
        names = sorted(ADMITTED)
        stormy = [task(names[0], "ffe")] + [task(name, "eee") for name in names[1:3]] + [task(name, "ppp") for name in names[3:]]
        mine = run(100, 1300, NOW - timedelta(minutes=5), tasks=stormy, result="FAILURE")
        storm = {"state": "DEGRADED", "condition": "storm", "since": "2026-09-08T13:00:00+00:00", "failing_cases": [], "tracking_issues": [], "incident": {"prs": [1, 2, 3], "runs": 3, "window_start": "2026-09-08T13:15:00+00:00", "window_end": "2026-09-08T14:25:00+00:00"}}
        self.tick(data(mine, *green_others()), storm)
        box = self.gh.bodies()[0].split("\n")[3]
        self.assertTrue(box.startswith("> 🟡 **Gate degraded** since Tue 9:00 AM ET. quota storm 9:15 AM–10:25 AM ET hit 3 PRs."), box)
        self.assertIn("Your 1 failure is exactly that one, so this red is not your code.", box)
        self.assertIn("| 0 / 3 reps (1 infra) |", self.gh.bodies()[0])


def lost(build, pr, finished, minutes=128, node="gke-kube-agents-prow-default-pool-eb220b2a-sgnk", **fields):
    """A run whose build node went away: zero tasks, FAILURE, no build log,
    a NodeNotReady pod event -- what the collector records for one."""
    raw = run(build, pr, finished, tasks=[], result="failure", minutes=minutes, project=None)
    raw.update({"has_build_log": False, "pod_phase": "Failed", "pod_node": node, "pod_last_event": "NodeNotReady", **fields})
    return raw


def lost_pods_health(prs=(926, 1118, 1246, 1258, 1319, 1351, 1362, 1439, 1451, 1456, 1460, 1471)):
    return {
        "state": "DEGRADED",
        "condition": "lost_pods",
        "since": "2026-09-08T14:05:52+00:00",  # Tue 10:05 AM EDT
        "failing_cases": [],
        "tracking_issues": [],
        "incident": {"prs": list(prs), "runs": len(prs), "window_start": "2026-09-08T14:05:52+00:00", "window_end": "2026-09-08T14:19:16+00:00", "nodes": {"gke-kube-agents-prow-default-pool-eb220b2a-sgnk": 3}, "event": len(prs) >= 8},
    }


class LostPodComment(Harness):
    """A run whose build node went away gets one short comment (#1478): the
    time and node, that nothing was graded, and /retest -- with the
    build-cluster event named while health.json's condition is lost_pods."""

    def test_the_shape_without_an_incident(self):
        mine = lost(100, 1300, NOW - timedelta(minutes=5))
        rc, _ = self.tick(data(mine, *green_others()), green_health())
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh.writes(), [("POST", "repos/gke-labs/kube-agents/issues/1300/comments")])
        lines = self.gh.bodies()[0].split("\n")
        self.assertEqual(lines[0], gate_comment.MARKER)
        self.assertEqual(lines[1], "### ⚪ Smoke gate: run lost")
        self.assertEqual(
            lines[3],
            "> The Prow build node running this job went away at 10:55 AM ET (gke-kube-agents-prow-default-pool-eb220b2a-sgnk)."
            " Nothing was graded and nothing about your change is implied. `/retest` once new jobs are progressing."
            " [Details →](https://storage.cloud.google.com/kube-agents-dashboards/evals/run.html?build=100)",
        )
        self.assertEqual(lines[5], "Ran 128 min before the node went away · [build log](https://oss.gprow.dev/view/gs/kube-agents-prow/pr-logs/pull/gke-labs_kube-agents/1300/pull-kube-agents-smoke-test/100)")
        self.assertNotIn("Incident brief", self.gh.bodies()[0])
        self.assertNotIn("cases", self.gh.bodies()[0])
        self.assertEqual(self.recorded()["comments"]["1300"], {"comment_id": 501, "build_id": "100", "at": NOW.isoformat()})

    def test_inside_a_build_cluster_event_the_box_says_so(self):
        mine = lost(100, 1300, NOW - timedelta(minutes=5))
        self.tick(data(mine, *green_others()), lost_pods_health())
        box = self.gh.bodies()[0].split("\n")[3]
        self.assertTrue(box.startswith("> The Prow build node running this job went away at 10:55 AM ET (gke-kube-agents-prow-default-pool-eb220b2a-sgnk) — part of a build-cluster event: 12 runs on 12 PRs. Nothing was graded"), box)
        self.assertTrue(box.endswith("[Details →](https://storage.cloud.google.com/kube-agents-dashboards/evals/run.html?build=100) · [Incident brief →](https://storage.cloud.google.com/kube-agents-dashboards/evals/index.html?since=2026-09-08T14:05:52Z#gate)"), box)

    def test_below_the_event_bar_the_box_gives_the_count_without_calling_it_an_event(self):
        mine = lost(100, 1300, NOW - timedelta(minutes=5))
        self.tick(data(mine, *green_others()), lost_pods_health(prs=(1300, 1301, 1302)))
        box = self.gh.bodies()[0].split("\n")[3]
        self.assertIn("(gke-kube-agents-prow-default-pool-eb220b2a-sgnk) — one of 3 runs on 3 PRs that lost their build node. Nothing was graded", box)
        self.assertNotIn("event", box)
        self.assertIn("[Incident brief →]", box)

    def test_a_lost_pod_known_only_by_its_missing_log_has_no_node_to_name(self):
        mine = lost(100, 1300, NOW - timedelta(minutes=5), pod_node=None, pod_last_event=None, pod_phase=None)
        self.tick(data(mine, *green_others()), green_health())
        self.assertIn("> The Prow build node running this job went away at 10:55 AM ET. Nothing was graded", self.gh.bodies()[0])

    def test_short_lost_pods_are_commented_on_and_setup_deaths_still_are_not(self):
        # 297 s in with a NodeNotReady pod: a lost pod, not a setup death.
        short = lost(100, 1300, NOW - timedelta(minutes=5), minutes=5)
        clone_failed = run(101, 1301, NOW - timedelta(minutes=4), tasks=[], result="failure", minutes=0)
        clone_failed.update({"has_build_log": True, "pod_phase": "Failed", "pod_node": "n", "pod_last_event": "Started"})
        self.tick(data(short, clone_failed, *green_others()), green_health())
        self.assertEqual(self.gh.writes(), [("POST", "repos/gke-labs/kube-agents/issues/1300/comments")])

    def test_the_same_lost_build_is_never_commented_on_twice_and_a_later_one_edits(self):
        first = lost(100, 1300, NOW - timedelta(minutes=30))
        self.tick(data(first, *green_others()), green_health())
        self.tick(data(first, *green_others()), green_health(), now=NOW + timedelta(minutes=15))
        self.assertEqual(len(self.gh.writes()), 1)
        later = NOW + timedelta(hours=1)
        second = lost(101, 1300, later - timedelta(minutes=5))
        self.tick(data(first, second, *green_others()), green_health(), now=later)
        self.assertEqual(self.gh.writes()[-1], ("PATCH", "repos/gke-labs/kube-agents/issues/comments/501"))
        self.assertEqual(self.recorded()["comments"]["1300"]["build_id"], "101")

    def test_dry_run_prints_it(self):
        rc, err = self.tick(data(lost(100, 1300, NOW - timedelta(minutes=5)), *green_others()), lost_pods_health(), dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh.writes(), [])
        self.assertIn("### ⚪ Smoke gate: run lost", err)


class WhenItComments(Harness):
    def test_green_aborted_and_setup_dead_runs_get_no_comment(self):
        greens = green_others()
        aborted = run(200, 1301, NOW - timedelta(minutes=10), failing=TRIO, result="ABORTED")
        setup = run(201, 1302, NOW - timedelta(minutes=8), tasks=[], result="FAILURE", minutes=2)
        emptied = run(202, 1303, NOW - timedelta(minutes=6), tasks=[task(n, "eee") for n in sorted(ADMITTED)], result="FAILURE")
        rc, err = self.tick(data(*greens, aborted, setup, emptied), green_health())
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh.calls, [])
        self.assertIn("gate comments: 0 red runs", err)
        self.assertEqual(self.recorded()["last_comment_tick"], NOW.isoformat())

    def test_only_runs_since_the_last_tick_and_the_first_tick_looks_back_an_hour(self):
        old = run(100, 1300, NOW - timedelta(hours=2), failing=TRIO)
        recent = run(101, 1301, NOW - timedelta(minutes=30), failing=TRIO)
        self.tick(data(old, recent, *other_runs()), outage_health())
        self.assertEqual(self.gh.writes(), [("POST", "repos/gke-labs/kube-agents/issues/1301/comments")])
        later = NOW + timedelta(minutes=15)
        newer = run(102, 1302, later - timedelta(minutes=5), failing=TRIO)
        self.tick(data(old, recent, newer, *other_runs()), outage_health(), now=later)
        self.assertEqual([p for _, p in self.gh.writes()][-1], "repos/gke-labs/kube-agents/issues/1302/comments")
        self.assertEqual(len(self.gh.writes()), 2, "1301 is not commented on again")

    def test_a_later_run_edits_the_existing_comment_and_the_same_build_is_never_posted_twice(self):
        first = run(100, 1300, NOW - timedelta(minutes=30), failing=TRIO)
        self.tick(data(first, *other_runs()), outage_health())
        self.assertEqual(self.gh.writes(), [("POST", "repos/gke-labs/kube-agents/issues/1300/comments")])
        # Same build seen again (the watermark moved back, say): skipped.
        self.state.write_text(json.dumps(dict(self.recorded(), last_comment_tick=(NOW - timedelta(hours=1)).isoformat())))
        self.tick(data(first, *other_runs()), outage_health())
        self.assertEqual(len(self.gh.writes()), 1)
        # A new red on the same PR: PATCH the remembered comment, no search.
        later = NOW + timedelta(hours=1)
        second = run(101, 1300, later - timedelta(minutes=5), failing=["agent-kanban-smoke"])
        self.tick(data(first, second, *green_others()), green_health(), now=later)
        self.assertEqual(self.gh.writes()[-1], ("PATCH", "repos/gke-labs/kube-agents/issues/comments/501"))
        self.assertEqual([m for m, _, _ in self.gh.calls].count("GET"), 1, "the first tick searched once; the edit did not")
        self.assertEqual(self.recorded()["comments"]["1300"]["build_id"], "101")

    def test_a_marked_comment_from_before_the_state_file_is_edited_not_duplicated(self):
        gh = FakeGh(existing={1300: [{"id": 42, "body": "hello"}, {"id": 43, "body": gate_comment.MARKER + "\n### old"}]})
        self.tick(data(run(100, 1300, NOW - timedelta(minutes=5), failing=TRIO), *other_runs()), outage_health(), gh=gh)
        self.assertEqual(gh.writes(), [("PATCH", "repos/gke-labs/kube-agents/issues/comments/43")])
        self.assertEqual(self.recorded()["comments"]["1300"]["comment_id"], 43)

    def test_a_post_failure_is_a_warning_and_the_run_is_retried_next_tick(self):
        failing = FakeGh(fail_writes=True)
        mine = run(100, 1300, NOW - timedelta(minutes=5), failing=TRIO)
        rc, err = self.tick(data(mine, *other_runs()), outage_health(), gh=failing)
        self.assertEqual(rc, 0, "never fails the job")
        self.assertIn("warning: gh api POST", err)
        self.assertIn("warning: no comment landed on #1300", err)
        recorded = self.recorded()
        self.assertNotIn("build_id", recorded["comments"]["1300"], "the build is not recorded as commented")
        self.assertEqual(recorded["comments"]["1300"]["failures"], 1)
        self.assertEqual(recorded["last_comment_tick"], (NOW - timedelta(minutes=5, seconds=1)).isoformat())
        # Next tick, GitHub is back: the same build is commented on once.
        self.tick(data(mine, *other_runs()), outage_health(), now=NOW + timedelta(minutes=15))
        self.assertEqual(self.gh.writes(), [("POST", "repos/gke-labs/kube-agents/issues/1300/comments")])

    def test_the_clock_is_data_jsons_horizon_so_a_run_finishing_during_the_tick_is_not_lost(self):
        # Tick 1: data.json collected at NOW (its generated_at); no --now, so
        # the watermark is NOW, not the later wall clock of the comment step.
        self.tick(data(*other_runs(), generated_at=NOW), outage_health(), now=None)
        self.assertEqual(self.recorded()["last_comment_tick"], NOW.isoformat())
        # A run finished 2 minutes after that collect, while the tick was
        # still rendering and publishing; the next collect (15 minutes on)
        # is the first data.json that carries it.
        late = run(100, 1300, NOW + timedelta(minutes=2), failing=TRIO)
        self.tick(data(late, *other_runs(), generated_at=NOW + timedelta(minutes=15)), outage_health(), now=None)
        self.assertEqual(self.gh.writes(), [("POST", "repos/gke-labs/kube-agents/issues/1300/comments")])
        self.assertEqual(self.recorded()["last_comment_tick"], (NOW + timedelta(minutes=15)).isoformat())
        # The overlap re-scans the last half hour every tick and never
        # re-comments a recorded build.
        self.tick(data(late, *other_runs(), generated_at=NOW + timedelta(minutes=30)), outage_health(), now=None)
        self.tick(data(late, *other_runs(), generated_at=NOW + timedelta(minutes=45)), outage_health(), now=None)
        self.assertEqual(len(self.gh.writes()), 1)
        self.assertEqual(gate_comment.SCAN_OVERLAP, timedelta(minutes=30))

    def test_a_transient_edit_failure_never_leaves_a_second_marked_comment(self):
        first = run(100, 1300, NOW - timedelta(minutes=30), failing=TRIO)
        self.tick(data(first, *other_runs()), outage_health())
        self.assertEqual(self.gh.writes(), [("POST", "repos/gke-labs/kube-agents/issues/1300/comments")])
        # The remembered comment 501 still exists (the search finds it) but
        # the PATCH fails: no POST, retried next tick.
        flaky = FakeGh(existing={1300: [{"id": 501, "body": gate_comment.MARKER + "\n### old"}]}, fail_writes=True)
        later = NOW + timedelta(hours=1)
        second = run(101, 1300, later - timedelta(minutes=5), failing=["agent-kanban-smoke"])
        rc, err = self.tick(data(first, second, *green_others()), green_health(), now=later, gh=flaky)
        self.assertEqual(rc, 0)
        self.assertEqual(flaky.writes(), [("PATCH", "repos/gke-labs/kube-agents/issues/comments/501")])
        self.assertIn("retried next tick", err)
        self.assertEqual(self.recorded()["comments"]["1300"]["build_id"], "100", "the new build is not recorded as commented")
        # A comment found by search whose edit fails is the same: no POST.
        found_only = FakeGh(existing={1301: [{"id": 77, "body": gate_comment.MARKER + "\n### old"}]}, fail_writes=True)
        self.state.unlink()
        self.tick(data(run(200, 1301, NOW - timedelta(minutes=5), failing=TRIO), *other_runs()), outage_health(), gh=found_only)
        self.assertEqual(found_only.writes(), [("PATCH", "repos/gke-labs/kube-agents/issues/comments/77")])

    def test_a_pull_request_that_cannot_be_commented_on_is_given_up_after_three_attempts(self):
        failing = FakeGh(fail_writes=True)
        mine = run(100, 1300, NOW - timedelta(minutes=5), failing=TRIO)
        for attempt in range(1, 4):
            rc, err = self.tick(data(mine, *other_runs()), outage_health(), now=NOW + timedelta(minutes=15 * attempt), gh=failing)
            self.assertEqual(rc, 0)
        posts = [p for m, p in failing.writes() if m == "POST"]
        self.assertEqual(len(posts), 3)
        self.assertIn("after 3 attempts; giving up on this build", err)
        recorded = self.recorded()
        self.assertEqual((recorded["comments"]["1300"]["build_id"], recorded["comments"]["1300"]["failures"]), ("100", 3))
        self.assertEqual(recorded["last_comment_tick"], (NOW + timedelta(minutes=45)).isoformat(), "the watermark is no longer pinned")
        # A fourth tick asks GitHub nothing for that build.
        self.tick(data(mine, *other_runs()), outage_health(), now=NOW + timedelta(minutes=60), gh=failing)
        self.assertEqual(len(failing.writes()), 3)
        # Before the third failure the watermark was pinned to the run.
        # (Checked on a fresh state: one failure, watermark just before it.)
        self.state.unlink()
        once = FakeGh(fail_writes=True)
        self.tick(data(mine, *other_runs()), outage_health(), gh=once)
        self.assertEqual(self.recorded()["last_comment_tick"], (NOW - timedelta(minutes=5, seconds=1)).isoformat())
        self.assertEqual(self.recorded()["comments"]["1300"]["failures"], 1)

    def test_dry_run_prints_and_posts_nothing(self):
        mine = run(100, 1300, NOW - timedelta(minutes=5), failing=TRIO)
        rc, err = self.tick(data(mine, *other_runs()), outage_health(), dry_run=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.gh.writes(), [])
        self.assertIn("--dry-run: would comment on #1300 (build 100", err)
        self.assertIn("### ❌ Smoke gate: failed · 3 of 7 cases", err)
        self.assertIn("--dry-run: would POST repos/gke-labs/kube-agents/issues/1300/comments", err)

    def test_the_class_vocabulary_is_classify_pys(self):
        from eval_dashboard import classify

        self.assertEqual((gate_comment.CLS_SHARED, gate_comment.CLS_ONLY_THIS_PR, gate_comment.CLS_STORM), (classify.CLS_SHARED, classify.CLS_ONLY_THIS_PR, classify.CLS_STORM))
        self.assertEqual(gate_comment.ONLY_PR_WINDOW, classify.ONLY_PR_WINDOW)


if __name__ == "__main__":
    unittest.main()
