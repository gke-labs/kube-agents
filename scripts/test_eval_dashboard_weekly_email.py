"""weekly_email.py: the weekly health email renders only when every
integrity check (#3440, "Integrity checks") passes, one test class per check.

The nights are synthetic, built like test_eval_dashboard_nightly_bugs.py's:
two weeks of nightly runs (2026-10-05 .. 2026-10-18), an evidence store
naming each night's model, and a temporary checkout holding domains.yaml and
the task.yaml files the bug filer reads.
"""

import contextlib
import copy
import datetime
import io
import json
import pathlib
import tempfile
import unittest
import unittest.mock

import yaml
from eval_dashboard import post_health
from eval_dashboard import weekly_email as we

JOB = "ci-kube-agents-eval-nightly"
MAIN_B_JOB = "ci-kube-agents-eval-nightly-main-b"
WRITERS_JOB = "ci-kube-agents-eval-nightly-writers"
# A build id's last digit per job, so a split night's builds differ.
JOB_DIGIT = {JOB: 1, MAIN_B_JOB: 2, WRITERS_JOB: 3}
REPO = "gke-labs/kube-agents"
WEEK_END = datetime.date(2026, 10, 18)
NOW = datetime.datetime(2026, 10, 19, 13, 0, tzinfo=datetime.timezone.utc)
THIS_WEEK = list(range(12, 19))
LAST_WEEK = list(range(5, 12))
MODEL = "gemini-3-1-pro-preview-kubeagents-mcp"
OTHER_MODEL = "claude-opus-5-5-kubeagents-mcp"
WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci-health.yml"

DOMAINS = [
    {"slug": "chat", "name": "Chat (routing)", "owner": "a", "backup": "b", "meaning": "Answers questions."},
    {"slug": "cost", "name": "Cost", "owner": "a", "backup": "b", "meaning": "Finds idle resources."},
    {"slug": "obs", "name": "Observability", "owner": "a", "backup": "b", "meaning": "Checks metrics."},
]
# case -> domain. chat and cost have three scenarios (a trend is called);
# obs has two (few scenarios).
CASES = {"chat-1": "chat", "chat-2": "chat", "chat-3": "chat",
         "cost-1": "cost", "cost-2": "cost", "cost-3": "cost",
         "obs-1": "obs", "obs-2": "obs"}


def task(name, passes, reps=3, agent_s=180.0):
    results = ["pass"] * passes + ["fail"] * (reps - passes)
    return {"name": name, "result": "pass" if passes == reps else "fail",
            "reps": [{"n": i + 1, "result": r, "reason": None if r == "pass" else "check failed", "agent_s": agent_s}
                     for i, r in enumerate(results)]}


def run(day, tasks, truncated=False, job=JOB):
    return {"build_id": f"3000000000000{day:02d}00{JOB_DIGIT[job]}", "tier": "nightly", "job": job, "pr": None,
            "started": f"2026-10-{day:02d}T00:00:30+00:00", "finished": f"2026-10-{day:02d}T06:00:00+00:00",
            "result": "FAILURE" if truncated else "SUCCESS", "eval_verdict": None if truncated else "GREEN",
            "duration_s": 20000, "tasks": tasks}


def passes_for(case, day):
    """Every case passes 3/3 except: cost passes 2/3 last week and 1/3 this
    week (a drop), obs-1 fails every run."""
    if case == "obs-1":
        return 0
    if case.startswith("cost"):
        return 2 if day in LAST_WEEK else 1
    return 3


def week_runs(days=THIS_WEEK + LAST_WEEK, cases=CASES, skip=()):
    return [run(d, [task(c, passes_for(c, d)) for c in cases if (c, d) not in skip]) for d in sorted(days)]


# The split nightly: chat on the main job (main-a), cost on main-b, obs on
# the writers job.
SPLIT = {JOB: ["chat-1", "chat-2", "chat-3"], MAIN_B_JOB: ["cost-1", "cost-2", "cost-3"], WRITERS_JOB: ["obs-1", "obs-2"]}


def split_runs(days=THIS_WEEK + LAST_WEEK, absent=(), truncated=()):
    """One build per part per night; ``absent`` and ``truncated`` hold
    ``(job, day)`` pairs: no build, or a build cut off at the deadline."""
    return [run(d, [task(c, passes_for(c, d)) for c in cases], truncated=(job, d) in truncated, job=job)
            for d in sorted(days) for job, cases in SPLIT.items() if (job, d) not in absent]


def data_of(runs, cases=CASES, off_matrix=()):
    return {"schema_version": 1, "generated_at": "2026-10-19T12:00:00+00:00",
            "cases": [{"name": c, "domain": dom, "active": False, "nightly_active": c not in off_matrix} for c, dom in cases.items()],
            "runs": runs}


def store_of(runs, model=MODEL, models=None, skip=()):
    records = []
    for r in runs:
        day = int(r["started"][8:10])
        if day in skip:
            continue
        records.append({"case": "chat-1", "build": r["build_id"],
                        "key": {"setup_id": (models or {}).get(day, model), "judge_model": "j"}})
    return {"records": records}


class GhResult:
    def __init__(self, rc=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = rc, stdout, stderr


def bug(case, domain, number=4000, closed_at=None, state_reason=None):
    """A nightly-red issue as the bug filer files it, in REST shape."""
    return {"number": number, "title": f"Nightly: {case} fails every run, 3 nights in a row",
            "body": f"<!-- kube-agents-bot:nightly-red case={case} -->\n", "created_at": "2026-10-16T12:00:00Z",
            "closed_at": closed_at, "state_reason": state_reason,
            "labels": [{"name": "nightly-red"}, {"name": f"domain:{domain}"}]}


def bugs_of(opened=None, closed=()):
    """Default: one open bug, for obs-1, which fails every night."""
    return {"open": [bug("obs-1", "obs")] if opened is None else list(opened), "closed": list(closed)}


def node(issue):
    return {"number": issue["number"], "title": issue["title"], "body": issue["body"], "createdAt": issue["created_at"],
            "closedAt": issue["closed_at"], "stateReason": (issue["state_reason"] or "").upper() or None,
            "labels": {"nodes": issue["labels"]}}


class FakeGh:
    """A `gh api graphql` runner answering the nightly-red issues by state,
    ``page`` at a time."""

    def __init__(self, opened=(), closed=(), fail=False, page=100):
        self.issues = {"OPEN": [node(i) for i in opened], "CLOSED": [node(i) for i in closed]}
        self.fail = fail
        self.page = page
        self.calls = []

    def __call__(self, argv, input=None, **kwargs):
        body = json.loads(input) if input else None
        self.calls.append((argv, body))
        if self.fail:
            return GhResult(rc=1, stderr="HTTP 502")
        variables = body["variables"]
        nodes = self.issues[variables["states"][0]]
        start = int(variables["after"] or 0)
        chunk = nodes[start:start + self.page]
        more = start + self.page < len(nodes)
        reply = {"data": {"repository": {"issues": {"totalCount": len(nodes), "nodes": chunk,
                                                     "pageInfo": {"hasNextPage": more, "endCursor": str(start + self.page) if more else None}}}}}
        return GhResult(stdout=json.dumps(reply))


class Base(unittest.TestCase):
    """A checkout with domains.yaml and one task.yaml per case."""

    domains = DOMAINS

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / "docs" / "designs").mkdir(parents=True)
        (self.root / "docs" / "designs" / "domains.yaml").write_text(yaml.safe_dump({"domains": self.domains}))
        for case, domain in CASES.items():
            path = self.root / "bench" / "tasks" / case / "task.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(f"id: {case}\ndomain: {domain}\n")

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self):
        return we.load_domains(self.root)

    def week(self, data, store=None):
        return we.build_week(data, store, self.rows(), WEEK_END, self.root)

    def produce(self, data, store=None, bugs=None):
        """``(html, failures)``; bugs default to ``bugs_of()``."""
        store = store_of(data["runs"]) if store is None else store
        bugs = bugs_of() if bugs is None else bugs
        return we.produce(data, store, self.rows(), WEEK_END, bugs, NOW, REPO, self.root)

    def failures(self, week, data, bugs=None, page=None):
        bugs = bugs_of() if bugs is None else bugs
        return we.integrity_failures(week, self.rows(), bugs, data, page)

    def assertRefused(self, failures, needle):
        self.assertTrue(any(needle in f for f in failures), f"no failure containing {needle!r} in {failures}")


class TheEmail(Base):
    def test_renders_the_sections_from_the_data(self):
        page, failures = self.produce(data_of(week_runs()))
        self.assertEqual(failures, [])
        self.assertIn(we.ROBOT_URL, page)
        self.assertIn("Week of Oct 12", page)
        self.assertIn("Needs attention", page)
        self.assertIn("By area", page)
        self.assertIn("How we fix what fails", page)
        # Owners are never shown; the meanings are.
        self.assertIn("Finds idle resources.", page)
        # The name's parenthetical gloss is dropped.
        self.assertIn(">Chat <span", page)

    def test_main_writes_the_page_and_reads_counts_from_github(self):
        data = data_of(week_runs())
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "data.json").write_text(json.dumps(data))
            (tmp / "store.json").write_text(json.dumps(store_of(data["runs"])))
            out = tmp / we.OUT_NAME
            gh = FakeGh(opened=[bug("obs-1", "obs")])
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = we.main(["--data", str(tmp / "data.json"), "--store", str(tmp / "store.json"), "--out", str(out),
                              "--week-end", WEEK_END.isoformat(), "--repo-root", str(self.root)], gh_runner=gh, now=NOW)
            self.assertEqual(rc, 0)
            self.assertIn("Open bugs (1)", out.read_text())
            self.assertEqual(gh.calls[0][1]["variables"]["label"], "nightly-red")

    def test_main_refuses_without_writing_and_prints_why(self):
        data = data_of(week_runs())
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "data.json").write_text(json.dumps(data))
            out = tmp / we.OUT_NAME
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = we.main(["--data", str(tmp / "data.json"), "--out", str(out), "--week-end", WEEK_END.isoformat(),
                              "--repo-root", str(self.root)], gh_runner=FakeGh(opened=[bug("obs-1", "obs")]), now=NOW)
            self.assertEqual(rc, 1)
            self.assertFalse(out.exists())
            self.assertIn("NOT rendered", err.getvalue())
            self.assertIn("footer:", err.getvalue())

    def test_default_week_is_the_one_ending_the_sunday_before(self):
        self.assertEqual(we.default_week_end(datetime.date(2026, 10, 19)), datetime.date(2026, 10, 18))
        self.assertEqual(we.default_week_end(datetime.date(2026, 10, 22)), datetime.date(2026, 10, 18))
        self.assertEqual(we.default_week_end(datetime.date(2026, 10, 25)), datetime.date(2026, 10, 18))

    def test_the_workflow_renders_it_beside_data_json_and_mails_nothing(self):
        text = WORKFLOW.read_text()
        self.assertIn("scripts/eval_dashboard/weekly_email.py", text)
        self.assertIn(f"--out work/{we.OUT_NAME}", text)
        self.assertIn(f'"$DASHBOARD_BUCKET/{we.OUT_NAME}"', text)
        # After the bug filer, so a bug it files this tick counts.
        self.assertLess(text.index("nightly_bugs.py --data"), text.index("weekly_email.py --data"))
        self.assertNotIn("sendmail", text)

    def test_the_workflow_posts_the_summary_after_the_page_is_uploaded(self):
        text = WORKFLOW.read_text()
        step = text[text.index("- name: Post the weekly health summary"):]
        step = step[:step.index("\n      - name:")]
        self.assertIn("--chat-out work/weekly-chat.json", text)
        self.assertLess(text.index(f'"$DASHBOARD_BUCKET/{we.OUT_NAME}"'), text.index("python3 scripts/eval_dashboard/weekly_chat.py"))
        # Only after a render that passed, muted with the rest, with the
        # token the digest's poster already mints and nothing new.
        self.assertIn("if: steps.weekly_email.outcome == 'success' && env.CI_HEALTH_MUTE != 'true'", step)
        self.assertIn("CI_HEALTH_CHAT_TOKEN: ${{ steps.chat_token.outputs.token }}", step)
        self.assertIn("--message work/weekly-chat.json", step)
        self.assertIn('--state "$DASHBOARD_BUCKET/weekly-chat-state.json"', step)
        self.assertNotIn("secrets.", step)
        self.assertNotIn("vars.", step)


class Nights(Base):
    """How nights are read: run-level events left out, re-runs merged per case."""

    def test_a_run_level_event_is_left_out_and_named(self):
        runs = [r if not r["started"].startswith("2026-10-15") else run(15, [task(c, 0) for c in CASES]) for r in week_runs()]
        week = self.week(data_of(runs), store_of(runs))
        self.assertNotIn("2026-10-15", week["counted"])
        self.assertEqual(week["left_out"]["2026-10-15"], we.WHY_RUN_EVENT)

    def test_a_same_date_rerun_keeps_the_cases_it_did_not_run(self):
        runs = week_runs()
        rerun = run(18, [task("chat-1", 0), task("chat-2", 3), task("chat-3", 3)])
        rerun.update(build_id="3000000000000189999", started="2026-10-18T05:00:00+00:00")
        week = self.week(data_of(runs + [rerun]), store_of(runs + [rerun]))
        clean = self.week(data_of(runs), store_of(runs))
        # The re-run's chat-1 verdict replaces the cron night's; every other
        # case keeps its cron-night verdict.
        self.assertEqual(week["overall"]["graded"], clean["overall"]["graded"])
        self.assertEqual(week["overall"]["passed"], clean["overall"]["passed"] - 3)

    def test_rounding_and_dates(self):
        self.assertEqual(we.pct_of(169, 200), 85)  # 84.5 rounds up
        self.assertEqual(we.pct_of(1, 3), 33)
        self.assertEqual(we.date_range(datetime.date(2026, 10, 26), datetime.date(2026, 11, 1)), "Oct 26 – Nov 1")
        self.assertEqual(we.date_range(datetime.date(2026, 10, 12), datetime.date(2026, 10, 18)), "Oct 12–18")


class SplitNights(Base):
    """A night split into main (main-a), main-b and writers builds."""

    def test_a_split_night_reads_like_one_build(self):
        split = self.week(data_of(split_runs()), store_of(split_runs()))
        whole = self.week(data_of(week_runs()), store_of(week_runs()))
        self.assertEqual(split["counted"], whole["counted"])
        self.assertEqual(split["overall"], whole["overall"])
        self.assertEqual([(r["slug"], r["pct"]) for r in split["rows"]], [(r["slug"], r["pct"]) for r in whole["rows"]])

    def test_night_reps_reads_every_part_and_main_wins_a_case_both_recorded(self):
        night = {"main": run(18, [task("chat-1", 3)]),
                 "writers": run(18, [task("chat-1", 0), task("obs-1", 1)], job=WRITERS_JOB)}
        reps = we.night_reps(night)
        self.assertEqual({c: (r["passed"], r["graded"]) for c, r in reps.items()}, {"chat-1": (3, 3), "obs-1": (1, 3)})

    def test_the_model_on_record_for_any_part_names_the_night(self):
        runs = split_runs()
        store = store_of([r for r in runs if r["job"] == WRITERS_JOB])
        page, failures = self.produce(data_of(runs), store)
        self.assertEqual(failures, [])
        self.assertIn(f"model: {MODEL}", page)

    def test_a_writers_only_night_has_no_main_part(self):
        runs = split_runs(absent={(JOB, 15), (MAIN_B_JOB, 15)})
        week = self.week(data_of(runs), store_of(runs))
        self.assertNotIn("2026-10-15", week["counted"])
        self.assertEqual(week["left_out"]["2026-10-15"], we.WHY_NO_MAIN)
        page, failures = self.produce(data_of(runs), store_of(runs))
        self.assertEqual(failures, [])
        self.assertIn(f"Oct 15 ({we.WHY_NO_MAIN})", page)

    def test_a_main_rerun_after_a_cut_main_keeps_the_dates_other_parts(self):
        # main was cut on the 14th and re-run the same day; the re-run's night
        # borrows the 14th's main-b and writers, so the week reads as if main
        # had finished the first time.
        runs = split_runs(truncated={(JOB, 14)})
        rerun = run(14, [task(c, passes_for(c, 14)) for c in SPLIT[JOB]])
        rerun.update(build_id="30000000000001409", started="2026-10-14T09:00:00+00:00", finished="2026-10-14T15:00:00+00:00")
        runs.append(rerun)
        split = self.week(data_of(runs), store_of(runs))
        whole = self.week(data_of(split_runs()), store_of(split_runs()))
        self.assertIn("2026-10-14", split["counted"])
        self.assertEqual(split["overall"], whole["overall"])
        self.assertEqual([(r["slug"], r["pct"]) for r in split["rows"]], [(r["slug"], r["pct"]) for r in whole["rows"]])

    def test_a_night_without_main_b_is_not_counted(self):
        # main-b ran the cost cases every other night, so a night without it
        # graded half the main matrix: the digest withholds its verdict.
        runs = split_runs(absent={(MAIN_B_JOB, 14)})
        week = self.week(data_of(runs), store_of(runs))
        self.assertNotIn("2026-10-14", week["counted"])
        self.assertEqual(week["left_out"]["2026-10-14"], we.WHY_NO_MAIN_B)
        page, failures = self.produce(data_of(runs), store_of(runs))
        self.assertEqual(failures, [])
        self.assertIn(f"Oct 14 ({we.WHY_NO_MAIN_B})", page)

    def test_a_truncated_writers_part_beside_a_clean_main_leaves_the_night_out(self):
        runs = split_runs(truncated={(WRITERS_JOB, 13)})
        week = self.week(data_of(runs), store_of(runs))
        self.assertNotIn("2026-10-13", week["counted"])
        self.assertEqual(week["left_out"]["2026-10-13"], we.WHY_CUT)
        clean = self.week(data_of(split_runs()), store_of(split_runs()))
        self.assertEqual(len(week["counted"]), len(clean["counted"]) - 1)
        page, failures = self.produce(data_of(runs), store_of(runs))
        self.assertEqual(failures, [])
        self.assertIn("Oct 13 (cut off)", page)

    def test_a_main_rerun_keeps_the_other_parts_of_the_cron_night(self):
        runs = split_runs()
        rerun = run(18, [task("chat-1", 0), task("chat-2", 3), task("chat-3", 3)])
        rerun.update(build_id="3000000000000189999", started="2026-10-18T05:00:00+00:00")
        week = self.week(data_of(runs + [rerun]), store_of(runs + [rerun]))
        clean = self.week(data_of(runs), store_of(runs))
        self.assertIn("2026-10-18", week["counted"])
        # The re-run's chat-1 verdict replaces the cron night's; main-b's and
        # the writers' cases keep their cron-night verdicts.
        self.assertEqual(week["overall"]["graded"], clean["overall"]["graded"])
        self.assertEqual(week["overall"]["passed"], clean["overall"]["passed"] - 3)


class SameCases(Base):
    """Check 1: week-over-week only over the scenarios graded both weeks."""

    def test_a_scenario_graded_in_one_week_only_is_left_out(self):
        cases = dict(CASES, **{"chat-new": "chat"})
        runs = [run(d, [task(c, passes_for(c, d)) for c in CASES] + ([task("chat-new", 0)] if d in THIS_WEEK else []))
                for d in THIS_WEEK + LAST_WEEK]
        week = self.week(data_of(runs, cases), store_of(runs))
        self.assertNotIn("chat-new", week["cases"])
        self.assertEqual(week["overall"]["scenarios"], len(CASES))
        self.assertEqual(week["one_week_only"], ["chat-new"])
        page, failures = self.produce(data_of(runs, cases), store_of(runs))
        self.assertEqual(failures, [])
        self.assertIn("1 graded in only one week are left out", page)

    def test_a_since_removed_case_is_left_out_so_the_rows_add_up_to_the_tile(self):
        # No task.yaml on this checkout: collect files its runs as "unknown",
        # which is no area row, so it is left out of the tile too.
        cases = dict(CASES, **{"gone-1": "unknown"})
        runs = [run(d, [task(c, passes_for(c, d)) for c in cases]) for d in THIS_WEEK + LAST_WEEK]
        week = self.week(data_of(runs, cases, off_matrix={"gone-1"}), store_of(runs))
        self.assertNotIn("gone-1", week["cases"])
        self.assertEqual(week["removed"], ["gone-1"])
        self.assertEqual(week["one_week_only"], [])
        self.assertEqual(week["overall"]["scenarios"], len(CASES))
        self.assertEqual(sum(r["scenarios"] for r in week["rows"]), week["overall"]["scenarios"])
        page, failures = self.produce(data_of(runs, cases, off_matrix={"gone-1"}), store_of(runs))
        self.assertEqual(failures, [])
        self.assertIn("1 no longer in the suite are left out", page)

    def test_a_live_case_whose_domain_cannot_be_read_refuses_the_week(self):
        # A quoted `domain: "chat"` reads as unknown, but the case is still on
        # the nightly matrix: it is not "no longer in the suite", so it stays
        # compared, no area row holds it, and the week is refused.
        cases = dict(CASES, **{"quoted-1": "unknown"})
        runs = [run(d, [task(c, passes_for(c, d)) for c in cases]) for d in THIS_WEEK + LAST_WEEK]
        data = data_of(runs, cases)
        week = self.week(data, store_of(runs))
        self.assertEqual(week["removed"], [])
        self.assertIn("quoted-1", week["cases"])
        _, failures = self.produce(data, store_of(runs))
        self.assertRefused(failures, "the area rows hold 8 scenarios but 9 were compared")

    def test_a_removed_case_counted_in_the_tile_refuses(self):
        cases = dict(CASES, **{"gone-1": "unknown"})
        runs = [run(d, [task(c, passes_for(c, d)) for c in cases]) for d in THIS_WEEK + LAST_WEEK]
        data = data_of(runs, cases, off_matrix={"gone-1"})
        week = self.week(data, store_of(runs))
        week["cases"].append("gone-1")
        week["overall"]["scenarios"] += 1
        failures = self.failures(week, data)
        self.assertRefused(failures, "not the ones graded in both weeks")
        self.assertRefused(failures, "the area rows hold 8 scenarios but 9 were compared")

    def test_rows_that_do_not_add_up_to_the_tile_refuse(self):
        data = data_of(week_runs())
        week = self.week(data, store_of(data["runs"]))
        week["rows"] = [r for r in week["rows"] if r["slug"] != "obs"]
        week["attention"] = [s for s in week["attention"] if s != "obs"]
        self.assertRefused(self.failures(week, data), "the area rows hold 6 scenarios but 8 were compared")

    def test_a_count_that_does_not_match_the_compared_set_refuses(self):
        data = data_of(week_runs())
        week = self.week(data, store_of(data["runs"]))
        week["overall"]["scenarios"] += 1
        self.assertRefused(self.failures(week, data), "same cases")
        week = self.week(data, store_of(data["runs"]))
        week["overall"]["passed"] += 1
        self.assertRefused(self.failures(week, data), "same cases")


class TruncatedNights(Base):
    """Check 2: a night cut off at the deadline counts for nothing."""

    def test_a_cut_off_night_is_not_counted(self):
        runs = week_runs()
        runs.append(run(19 - 1, [task(c, 0) for c in CASES], truncated=True))
        # The cut-off re-run of the 18th sorts after the good one; the good one stands.
        week = self.week(data_of(runs), store_of(runs))
        self.assertIn("2026-10-18", week["counted"])
        clean = self.week(data_of(week_runs()), store_of(week_runs()))
        self.assertEqual(week["overall"]["pct"], clean["overall"]["pct"])

    def test_a_date_with_only_a_cut_off_night_is_left_out_and_named(self):
        runs = [r if not r["started"].startswith("2026-10-13") else run(13, [task(c, 0) for c in CASES], truncated=True)
                for r in week_runs()]
        week = self.week(data_of(runs), store_of(runs))
        self.assertNotIn("2026-10-13", week["counted"])
        self.assertEqual(week["left_out"]["2026-10-13"], we.WHY_CUT)
        page, failures = self.produce(data_of(runs), store_of(runs))
        self.assertEqual(failures, [])
        self.assertIn("Oct 13 (cut off)", page)

    def test_counting_a_cut_off_night_refuses(self):
        runs = [r if not r["started"].startswith("2026-10-13") else run(13, [task(c, 0) for c in CASES], truncated=True)
                for r in week_runs()]
        data = data_of(runs)
        week = self.week(data, store_of(runs))
        week["counted"].append("2026-10-13")
        self.assertRefused(self.failures(week, data), "truncated nights")


class NotEnoughData(Base):
    """Check 3: fewer than MIN_COUNTED_NIGHTS counted nights, no trend."""

    def three_nights(self):
        return week_runs(days=THIS_WEEK[:3] + LAST_WEEK)

    def test_three_nights_say_not_enough_data_and_call_no_change(self):
        runs = self.three_nights()
        page, failures = self.produce(data_of(runs), store_of(runs))
        self.assertEqual(failures, [])
        self.assertIn("Not enough data this week", page)
        self.assertIn(we.CHANGE_NONE, page)
        self.assertNotIn("&#9660;", page)
        self.assertNotIn("&#9650;", page)
        self.assertNotIn("steady", page)

    def test_a_date_with_no_run_is_named_and_the_intro_says_counted(self):
        runs = self.three_nights()
        page, failures = self.produce(data_of(runs), store_of(runs))
        self.assertEqual(failures, [])
        self.assertIn("3 of this week&#x27;s nights counted, and a trend needs 4", page)
        self.assertNotIn("ran to completion", page)
        for day in ("Oct 15", "Oct 16", "Oct 17", "Oct 18"):
            self.assertIn(f"{day} ({we.WHY_NO_RUN})", page)
        self.assertNotIn(we.CUT_NONE, page)

    def test_a_trend_called_on_too_few_nights_refuses(self):
        runs = self.three_nights()
        data = data_of(runs)
        week = self.week(data, store_of(runs))
        week["rows"][0]["change"], week["rows"][0]["trend"] = 0, we.TREND_STEADY
        self.assertRefused(self.failures(week, data), "not enough data")


class FewScenarios(Base):
    """Check 4: an area with fewer than MIN_TREND_SCENARIOS gets no trend."""

    def test_a_two_scenario_area_is_marked_few_and_not_called(self):
        runs = week_runs()
        # obs-2 drops from 3/3 to 0/3: a big drop, on a two-scenario area.
        runs = [run(int(r["started"][8:10]), [t if t["name"] != "obs-2" or int(r["started"][8:10]) in LAST_WEEK
                                               else task("obs-2", 0) for t in r["tasks"]]) for r in runs]
        week = self.week(data_of(runs), store_of(runs))
        obs = next(r for r in week["rows"] if r["slug"] == "obs")
        self.assertTrue(obs["few"])
        self.assertIsNone(obs["trend"])
        page, failures = self.produce(data_of(runs), store_of(runs), bugs=bugs_of([bug("obs-1", "obs"), bug("obs-2", "obs", 4001)]))
        self.assertEqual(failures, [])
        self.assertIn("few scenarios", page)
        self.assertNotIn("Observability (down", page)

    def test_a_trend_on_a_small_area_refuses(self):
        data = data_of(week_runs())
        week = self.week(data, store_of(data["runs"]))
        obs = next(r for r in week["rows"] if r["slug"] == "obs")
        obs["change"], obs["trend"] = -10, we.TREND_DOWN
        self.assertRefused(self.failures(week, data), "few scenarios")


class ClaimsFollowRules(Base):
    """Check 5: every sentence is a template, and meets its rule."""

    def test_the_intro_makes_only_the_claims_the_numbers_allow(self):
        page, failures = self.produce(data_of(week_runs()))
        self.assertEqual(failures, [])
        self.assertIn("In great shape (85% or more, and not down): Chat.", page)
        self.assertIn("Passed less than half the time: Cost.", page)
        self.assertIn("Down from last week: Cost (down 34 pts).", page)

    def test_great_shape_below_85_refuses(self):
        data = data_of(week_runs())
        week = self.week(data, store_of(data["runs"]))
        week["claims"].append({"kind": we.CLAIM_GREAT, "subjects": ["cost"], "values": {}})
        self.assertRefused(self.failures(week, data), "'in great shape' for Cost")

    def test_a_wrong_down_figure_refuses(self):
        data = data_of(week_runs())
        week = self.week(data, store_of(data["runs"]))
        down = next(c for c in week["claims"] if c["kind"] == we.CLAIM_DOWN)
        down["values"]["pts"]["cost"] = 20
        self.assertRefused(self.failures(week, data), "'down 20 pts' for Cost")

    def test_less_than_half_at_50_refuses(self):
        data = data_of(week_runs())
        week = self.week(data, store_of(data["runs"]))
        week["claims"].append({"kind": we.CLAIM_HALF, "subjects": ["chat"], "values": {}})
        self.assertRefused(self.failures(week, data), "'less than half the time' for Chat")

    def test_text_outside_the_templates_refuses(self):
        data = data_of(week_runs())
        week = self.week(data, store_of(data["runs"]))
        week["claims"].append({"kind": "editorial", "subjects": [], "values": {}})
        self.assertRefused(self.failures(week, data), "'editorial' is not a template")


class NeedsAttentionIsComputed(Base):
    """Check 6: below 65%, or down 5+ points; nothing added or removed."""

    def test_the_computed_set(self):
        data = data_of(week_runs())
        week = self.week(data, store_of(data["runs"]))
        self.assertEqual(week["attention"], ["obs", "cost"])

    def test_the_empty_state_claims_no_trend_on_a_short_week(self):
        cases = {c: d for c, d in CASES.items() if d == "chat"}
        short = data_of(week_runs(days=THIS_WEEK[:3] + LAST_WEEK, cases=cases), cases)
        page = we.render(self.week(short, store_of(short["runs"])), bugs_of([]))
        self.assertIn(we.ATTENTION_NONE_NO_TREND, page)
        self.assertNotIn("none is down", page)
        full = data_of(week_runs(cases=cases), cases)
        page = we.render(self.week(full, store_of(full["runs"])), bugs_of([]))
        self.assertIn(we.ATTENTION_NONE, page)

    def test_an_area_added_or_removed_by_hand_refuses(self):
        data = data_of(week_runs())
        week = self.week(data, store_of(data["runs"]))
        week["attention"] = ["cost"]
        self.assertRefused(self.failures(week, data), "needs attention")
        week["attention"] = ["chat", "cost", "obs"]
        self.assertRefused(self.failures(week, data), "needs attention")


class BugsExist(Base):
    """Check 7: an area owed a bug has one open; counts come from GitHub."""

    def test_counts_shown_are_githubs(self):
        opened = [bug("obs-1", "obs"), bug("cost-1", "cost", 4001), bug("cost-2", "cost", 4002)]
        page, failures = self.produce(data_of(week_runs()), bugs=bugs_of(opened))
        self.assertEqual(failures, [])
        self.assertIn("Open bugs (2)", page)
        self.assertIn("Open bugs (1)", page)

    def test_a_red_streak_with_no_issue_refuses(self):
        _, failures = self.produce(data_of(week_runs()), bugs=bugs_of([]))
        self.assertRefused(failures, "bugs: Observability has obs-1 failing every run")

    def test_a_bug_for_another_case_does_not_count(self):
        _, failures = self.produce(data_of(week_runs()), bugs=bugs_of([bug("obs-2", "obs")]))
        self.assertRefused(failures, "bugs: Observability has obs-1")

    def test_a_closed_bug_that_still_tracks_counts(self):
        # Handed off as not planned before the streak's newest night finished,
        # with no pass since: tracked.
        handed_off = bug("obs-1", "obs", closed_at="2026-10-17T12:00:00Z", state_reason="not_planned")
        _, failures = self.produce(data_of(week_runs()), bugs=bugs_of([], [handed_off]))
        self.assertEqual(failures, [])
        # The same close as "completed" tracks too: some of the streak's
        # nights started before it, so it has not had three nights that all
        # started after it (#4041).
        completed = dict(handed_off, state_reason="completed")
        _, failures = self.produce(data_of(week_runs()), bugs=bugs_of([], [completed]))
        self.assertEqual(failures, [])
        # A "completed" close before the streak's oldest night started does not.
        fixed = bug("obs-1", "obs", closed_at="2026-10-15T12:00:00Z", state_reason="completed")
        _, failures = self.produce(data_of(week_runs()), bugs=bugs_of([], [fixed]))
        self.assertRefused(failures, "bugs: Observability has obs-1")

    def test_a_bug_closed_after_the_newest_red_night_counts(self):
        # A fix that merged after the streak's newest night finished (the 18th,
        # 06:00) and before the next nightly: tracked whatever the reason.
        seen = bug("obs-1", "obs", closed_at="2026-10-18T12:00:00Z", state_reason="completed")
        _, failures = self.produce(data_of(week_runs()), bugs=bugs_of([], [seen]))
        self.assertEqual(failures, [])
        # Only that branch holds a hand-off with no horizon to confirm it
        # against: the hand-off and completed-close rules both pass on it.
        handed_off = dict(seen, state_reason="not_planned")
        week = self.week(data_of(week_runs()))
        self.assertEqual(we.owed_bugs(week["streaks"], bugs_of([], [handed_off]), None), {})
        handed_off_before = dict(handed_off, closed_at="2026-10-17T12:00:00Z")
        self.assertEqual(we.owed_bugs(week["streaks"], bugs_of([], [handed_off_before]), None), {"obs": ["obs-1"]})

    def test_an_expected_fail_case_is_owed_nothing(self):
        path = self.root / "bench" / "tasks" / "obs-1" / "task.yaml"
        path.write_text(path.read_text() + "expected_fail: true\n")
        _, failures = self.produce(data_of(week_runs()), bugs=bugs_of([]))
        self.assertEqual(failures, [])

    def test_a_case_that_passed_after_the_week_is_owed_nothing(self):
        runs = week_runs() + [run(19, [task(c, 3) for c in CASES])]
        _, failures = self.produce(data_of(runs), store_of(runs), bugs=bugs_of([]))
        self.assertEqual(failures, [])

    def test_github_unreadable_refuses(self):
        data = data_of(week_runs())
        _, failures = we.produce(data, store_of(data["runs"]), self.rows(), WEEK_END, None, NOW, REPO, self.root)
        self.assertRefused(failures, "could not be read from GitHub")

    def test_the_read_pages_and_checks_the_total(self):
        data = data_of(week_runs())
        opened = [bug(f"c{i}", "obs", 5000 + i) for i in range(5)]
        closed = [bug("obs-1", "obs", 4000, closed_at="2026-10-18T12:00:00Z", state_reason="not_planned")]
        gh = FakeGh(opened=opened, closed=closed, page=2)
        bugs = we.read_bugs(we.ghcli.Gh(REPO, gh), data, REPO)
        self.assertEqual([i["number"] for i in bugs["open"]], [5000 + i for i in range(5)])
        self.assertEqual(bugs["closed"][0]["state_reason"], "not_planned")
        self.assertEqual(we.domain_counts(bugs["open"], ["obs", "cost"]), {"obs": 5, "cost": 0})
        closed_call = next(b for _, b in gh.calls if b["variables"]["states"] == ["CLOSED"])
        self.assertEqual(closed_call["variables"]["since"], "2026-10-05T00:00:30Z")

        def short(argv, input=None, **kwargs):
            reply = {"data": {"repository": {"issues": {"totalCount": 5, "nodes": [],
                                                         "pageInfo": {"hasNextPage": False, "endCursor": None}}}}}
            return GhResult(stdout=json.dumps(reply))
        self.assertIsNone(we.read_bugs(we.ghcli.Gh(REPO, short), data, REPO))
        self.assertIsNone(we.read_bugs(we.ghcli.Gh(REPO, FakeGh(fail=True)), data, REPO))


class LinksWellFormed(Base):
    """Check 8: every href and src is a well-formed https link."""

    def test_every_link_on_the_page_is_https_and_the_bug_search_is_exact(self):
        page, failures = self.produce(data_of(week_runs()))
        self.assertEqual(failures, [])
        links = we.LINK_ATTR_RE.findall(page)
        self.assertTrue(links)
        self.assertTrue(all(we.well_formed(we.html.unescape(u)) for u in links))
        self.assertIn("https://github.com/gke-labs/kube-agents/issues?q=is%3Aissue%20is%3Aopen%20label%3Anightly-red%20label%3Adomain%3Acost", page)

    def test_a_malformed_link_refuses(self):
        original = we.ROBOT_URL
        try:
            we.ROBOT_URL = "https://gke-labs.github.io/kube agents/agent-icon.png"
            _, failures = self.produce(data_of(week_runs()))
        finally:
            we.ROBOT_URL = original
        self.assertRefused(failures, "links:")

    def test_well_formed(self):
        self.assertTrue(we.well_formed("https://example.com/a?q=b%20c"))
        self.assertFalse(we.well_formed("http://example.com/"))
        self.assertFalse(we.well_formed("https://example.com/a b"))
        self.assertFalse(we.well_formed("https://example.com/%zz"))
        self.assertFalse(we.well_formed("https:///nohost"))


class DomainsMatch(Base):
    """Check 9: the data's domains and domains.yaml agree."""

    def test_a_domain_missing_from_domains_yaml_refuses(self):
        cases = dict(CASES, **{"stray-1": "stray"})
        runs = [run(d, [task(c, passes_for(c, d)) for c in cases]) for d in THIS_WEEK + LAST_WEEK]
        _, failures = self.produce(data_of(runs, cases), store_of(runs))
        self.assertRefused(failures, "domains: stray (cases stray-1) is in the data")

    def test_a_domains_yaml_row_with_no_data_refuses_unless_new(self):
        self.domains = DOMAINS + [{"slug": "quiet", "name": "Quiet", "owner": "a", "backup": "b", "meaning": "m"}]
        self.tearDown()
        self.setUp()
        _, failures = self.produce(data_of(week_runs()))
        self.assertRefused(failures, "domains: quiet is in docs/designs/domains.yaml")
        self.domains = DOMAINS + [{"slug": "quiet", "name": "Quiet", "owner": "a", "backup": "b", "meaning": "m", "new": True}]
        self.tearDown()
        self.setUp()
        _, failures = self.produce(data_of(week_runs()))
        self.assertEqual(failures, [])


class FooterFromMetadata(Base):
    """Check 10: the model and dates in the footer are the run's own."""

    def test_the_footer_names_the_model_and_range_on_record(self):
        page, failures = self.produce(data_of(week_runs()))
        self.assertEqual(failures, [])
        self.assertIn(f"Data from the nightly runs of Oct 5–18 &middot; model: {MODEL}", page)

    def test_a_model_change_is_said_in_the_intro_and_the_footer(self):
        runs = week_runs()
        store = store_of(runs, models={d: OTHER_MODEL for d in THIS_WEEK})
        page, failures = self.produce(data_of(runs), store)
        self.assertEqual(failures, [])
        self.assertIn(f"This week ran {OTHER_MODEL}; last week ran {MODEL}", page)
        self.assertIn(f"{OTHER_MODEL} (this week), {MODEL} (last week)", page)

    def test_a_counted_night_with_no_model_on_record_refuses(self):
        runs = week_runs()
        _, failures = self.produce(data_of(runs), store_of(runs, skip=(14,)))
        self.assertRefused(failures, "footer: 2026-10-14 has no model")

    def test_a_footer_naming_another_model_refuses(self):
        data = data_of(week_runs())
        store = store_of(data["runs"])
        week = self.week(data, store)
        week["footer"]["models"] = [OTHER_MODEL]
        self.assertRefused(we.recheck_footer(week, data, store), "footer:")

    def test_a_footer_with_another_range_refuses(self):
        data = data_of(week_runs())
        week = copy.deepcopy(self.week(data, store_of(data["runs"])))
        week["footer"]["range"] = ["2026-10-01", "2026-10-18"]
        self.assertRefused(self.failures(week, data), "footer:")


# Task time by case and week: last week every run takes 3 minutes; this week
# chat takes 2, observability 4 and cost 10.
THIS_WEEK_AGENT_S = {"chat": 120.0, "obs": 240.0, "cost": 600.0}
LAST_WEEK_AGENT_S = 180.0


def timed_runs(days=THIS_WEEK + LAST_WEEK, truncated=()):
    return [run(d, [task(c, passes_for(c, d), agent_s=LAST_WEEK_AGENT_S if d in LAST_WEEK else THIS_WEEK_AGENT_S[CASES[c]])
                    for c in CASES], truncated=d in truncated) for d in sorted(days)]


class TaskTime(Base):
    """The median, the 90th percentile and the slowest area, from the graded
    repetitions' agent_s."""

    def test_percentile(self):
        self.assertAlmostEqual(we.percentile([float(v) for v in range(1, 11)], we.TIME_PERCENTILE), 9.1)
        self.assertEqual(we.percentile([42.0], we.TIME_PERCENTILE), 42.0)
        self.assertIsNone(we.percentile([], we.TIME_PERCENTILE))

    def test_the_week_carries_the_median_the_tail_and_the_slowest_area(self):
        runs = timed_runs()
        week = self.week(data_of(runs), store_of(runs))
        overall = week["overall"]
        # 63 runs at 2 min, 42 at 4, 63 at 10: the middle is 4, the tail 10.
        self.assertEqual(overall["timed_runs"], 168)
        self.assertEqual(overall["agent_s_median"], 240.0)
        self.assertEqual(overall["agent_s_p90"], 600.0)
        self.assertEqual(overall["prev_agent_s_median"], 180.0)
        self.assertEqual(overall["slowest_area"], "cost")
        self.assertEqual({r["slug"]: r["agent_s_median"] for r in week["rows"]}, {"chat": 120.0, "cost": 600.0, "obs": 240.0})

    def test_a_tie_for_slowest_names_the_first_area_by_name(self):
        week = self.week(data_of(week_runs()), store_of(week_runs()))
        self.assertEqual(week["overall"]["slowest_area"], "chat")

    def test_the_tile_shows_the_tail(self):
        runs = timed_runs()
        page, failures = self.produce(data_of(runs), store_of(runs))
        self.assertEqual(failures, [])
        self.assertIn("middle of 168 timed runs; slowest 10% over 10 min", page)


class ChatPost(Base):
    """The weekly Chat summary: the page's own week, in Chat's markup."""

    def chat(self, runs, bugs=None):
        week, page, failures = we.produce_week(data_of(runs), store_of(runs), self.rows(), WEEK_END,
                                               bugs_of() if bugs is None else bugs, NOW, REPO, self.root)
        self.assertEqual(failures, [])
        return we.chat_text(week), page

    def test_a_trend_week(self):
        text, page = self.chat(timed_runs())
        self.assertEqual(text, "\n".join([
            "*kube-agents weekly health · Oct 12–18*",
            "63% of nightly test runs passed (105 of 168) across 8 scenarios, down 12 points from last week.",
            "",
            "*Needs attention*",
            f"• Observability: 50%, few scenarios, <{we.bugs_url(REPO, 'obs')}|1 open bug>",
            f"• Cost: 33%, down 34, <{we.bugs_url(REPO, 'cost')}|0 open bugs>",
            "",
            "Strongest: Chat 100%",
            "⏱ Task time: typical 4 min (up 1 min from last week), slowest 10% over 10 min · slowest area: Cost (10 min)",
            "",
            f"<{we.REPORT_URL}|full report> · <{we.DASHBOARD_URL}|dashboard>",
        ]))
        # The page says the same.
        self.assertIn("&#9660; 12 pts vs last week", page)
        self.assertIn("Open bugs (1)", page)
        self.assertIn("Open bugs (0)", page)
        self.assertEqual(we.REPORT_URL, f"https://storage.cloud.google.com/kube-agents-dashboards/evals/{we.OUT_NAME}")

    def test_a_week_with_too_few_nights_says_why_and_calls_no_change(self):
        text, _ = self.chat(timed_runs(truncated=THIS_WEEK[3:]))
        lines = text.splitlines()
        self.assertEqual(lines[1], "63% of nightly test runs passed (45 of 72) across 8 scenarios.")
        self.assertEqual(lines[2], "No week-over-week trend: only 3 of 7 nights counted (4 cut off).")
        self.assertIn("• Cost: 33%, <", text)
        self.assertIn("⏱ Task time: typical 4 min, slowest 10% over 10 min · slowest area: Cost (10 min)", text)
        self.assertNotIn("from last week", text)
        self.assertNotIn("same as last week", text)

    def test_a_short_last_week_is_the_one_named(self):
        text, _ = self.chat(week_runs(days=THIS_WEEK + LAST_WEEK[:3]))
        self.assertIn("No week-over-week trend: only 3 of last week's 7 nights counted (4 not run).", text)

    def test_several_reasons_are_counted_apart(self):
        runs = [r for r in timed_runs(truncated=THIS_WEEK[3:6]) if r["started"][8:10] != "18"]
        text, _ = self.chat(runs)
        self.assertIn("No week-over-week trend: only 3 of 7 nights counted (3 cut off, 1 not run).", text)

    def test_no_area_needing_attention_and_no_timing(self):
        cases = {c: d for c, d in CASES.items() if d == "chat"}
        runs = [run(int(r["started"][8:10]), [{**t, "reps": [{k: v for k, v in rep.items() if k != "agent_s"} for rep in t["reps"]]}
                                              for t in r["tasks"]]) for r in week_runs(cases=cases)]
        week = self.week(data_of(runs, cases), store_of(runs))
        we.render(week, bugs_of([]))
        text = we.chat_text(week)
        self.assertIn(we.CHAT_ATTENTION_NONE, text)
        self.assertIn(we.CHAT_TIME_NONE, text)
        self.assertIn("steady with last week.", text)

    def test_a_model_change_is_said(self):
        runs = week_runs()
        store = store_of(runs, models={d: OTHER_MODEL for d in THIS_WEEK})
        week, _, failures = we.produce_week(data_of(runs), store, self.rows(), WEEK_END, bugs_of(), NOW, REPO, self.root)
        self.assertEqual(failures, [])
        self.assertIn(f"This week ran {OTHER_MODEL}; last week ran {MODEL}", we.chat_text(week))

    def test_main_writes_the_summary_beside_the_page(self):
        runs = timed_runs()
        data = data_of(runs)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "data.json").write_text(json.dumps(data))
            (tmp / "store.json").write_text(json.dumps(store_of(runs)))
            chat = tmp / "weekly-chat.json"
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = we.main(["--data", str(tmp / "data.json"), "--store", str(tmp / "store.json"), "--out", str(tmp / we.OUT_NAME),
                              "--chat-out", str(chat), "--week-end", WEEK_END.isoformat(), "--repo-root", str(self.root)],
                             gh_runner=FakeGh(opened=[bug("obs-1", "obs")]), now=NOW)
            self.assertEqual(rc, 0)
            summary = json.loads(chat.read_text())
            self.assertEqual(summary["week_end"], WEEK_END.isoformat())
            self.assertEqual(summary["text"], self.chat(runs)[0])
            self.assertEqual(summary["card"]["cardId"], we.CARD_ID)
            self.assertEqual(set(summary), {we.CHAT_WEEK_KEY, we.CHAT_TEXT_KEY, we.CHAT_CARD_KEY})

    def test_a_refused_week_writes_no_summary_and_says_so(self):
        data = data_of(timed_runs())
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            (tmp / "data.json").write_text(json.dumps(data))
            chat = tmp / "weekly-chat.json"
            err = io.StringIO()
            # No store.json: the footer check refuses the week.
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = we.main(["--data", str(tmp / "data.json"), "--out", str(tmp / we.OUT_NAME), "--chat-out", str(chat),
                              "--week-end", WEEK_END.isoformat(), "--repo-root", str(self.root)],
                             gh_runner=FakeGh(opened=[bug("obs-1", "obs")]), now=NOW)
            self.assertEqual(rc, 1)
            self.assertFalse(chat.exists())
            self.assertIn("footer:", err.getvalue())
            self.assertIn("nothing is posted for this week", err.getvalue())


# Tokens by week: last week every run billed 100k; this week chat 120k, cost
# 150k and observability 90k.
THIS_WEEK_TOKENS = {"chat": 120_000, "cost": 150_000, "obs": 90_000}
LAST_WEEK_TOKENS = 100_000


def with_tokens(runs, count_of, partial=lambda day, case, n: False):
    """``runs`` with every rep carrying ``count_of(day, case)`` tokens (None
    for no record), ``partial`` for the reps it names."""
    out = copy.deepcopy(runs)
    for r in out:
        day = int(r["started"][8:10])
        for t in r["tasks"]:
            for rep in t["reps"]:
                count = count_of(day, t["name"])
                if count is not None:
                    rep["tokens"] = {"input": count // 2, "output": count // 4, "total": count,
                                     "partial": partial(day, t["name"], rep["n"])}
    return out


def tokened_runs(**kwargs):
    return with_tokens(timed_runs(**kwargs),
                       lambda day, case: LAST_WEEK_TOKENS if day in LAST_WEEK else THIS_WEEK_TOKENS[CASES[case]])


class Tokens(Base):
    """The typical run's tokens: the median full count over the compared
    scenarios' graded repetitions, its change from last week, and the
    partial counts left out; the page's tile and the Chat line agree."""

    def produce_week(self, runs):
        week, page, failures = we.produce_week(data_of(runs), store_of(runs), self.rows(), WEEK_END, bugs_of(), NOW, REPO, self.root)
        self.assertEqual(failures, [])
        return week, page

    def test_rep_tokens(self):
        self.assertIsNone(we.rep_tokens({"n": 1}))
        self.assertEqual(we.rep_tokens({"tokens": {"input": 1, "output": 2, "total": 9, "partial": False}}), (9, False))
        self.assertEqual(we.rep_tokens({"tokens": {"input": 1, "output": 2, "total": 9, "partial": True}}), (9, True))
        # No total: input + output, short of what the workers billed.
        self.assertEqual(we.rep_tokens({"tokens": {"input": 1, "output": 2, "total": None, "partial": False}}), (3, True))
        self.assertIsNone(we.rep_tokens({"tokens": {"input": None, "output": 2, "total": None, "partial": False}}))
        self.assertEqual(we.rep_tokens({"tokens": {"input": 0, "output": 0, "total": 0, "partial": False}}), (0, False))

    def test_tokens_text(self):
        self.assertEqual([we.tokens_text(n) for n in (0, 999, 999.5, 1_000, 134_600, 999_400, 999_600, 1_250_000)],
                         ["0", "999", "1k", "1k", "135k", "999k", "1.0M", "1.2M"])

    def test_a_week_with_tokens_and_its_change(self):
        week, page = self.produce_week(tokened_runs())
        overall = week["overall"]
        # 63 runs at 120k, 63 at 150k, 42 at 90k: the middle is 120k, up 20% on 100k.
        self.assertEqual(overall["tokens_median"], 120_000)
        self.assertEqual(overall["prev_paired_tokens_median"], 100_000)
        self.assertEqual(overall["counted_runs"], 168)
        self.assertEqual(overall["tokens_partial"], 0)
        line = "🔢 Tokens: typical 120k per run (▲ 20% vs last week on the same scenarios)"
        self.assertEqual(we.chat_tokens(week), line)
        self.assertIn(line, we.chat_text(week).splitlines())
        time = we.chat_card(week)["card"]["sections"][3]
        self.assertEqual(time["header"], "Task time")
        self.assertEqual(card_texts(time)[1], line)
        self.assertIn('<td style="width:25%;', page)
        self.assertIn("120k</div>", page)
        self.assertIn("typical tokens per run", page)
        self.assertIn("middle of 168 counted runs; &#9650; 20% vs last week on the same scenarios<", page)

    def test_a_drop_and_no_change(self):
        week, page = self.produce_week(with_tokens(timed_runs(), lambda day, case: 200_000 if day in LAST_WEEK else 150_000))
        self.assertEqual(we.chat_tokens(week), "🔢 Tokens: typical 150k per run (▼ 25% vs last week on the same scenarios)")
        self.assertIn("&#9660; 25% vs last week", page)
        week, page = self.produce_week(with_tokens(timed_runs(), lambda day, case: 150_000))
        self.assertEqual(we.chat_tokens(week), "🔢 Tokens: typical 150k per run (flat vs last week on the same scenarios)")
        self.assertIn("; flat vs last week on the same scenarios", page)

    def test_a_week_without_tokens_omits_the_line_and_the_tile(self):
        week, page = self.produce_week(timed_runs())
        self.assertIsNone(we.chat_tokens(week))
        self.assertNotIn("Tokens", we.chat_text(week))
        self.assertEqual(len(we.chat_card(week)["card"]["sections"][3]["widgets"]), 1)
        self.assertNotIn("tokens per run", page)
        self.assertNotIn("width:25%", page)

    def test_tokens_only_last_week_show_no_change(self):
        runs = with_tokens(timed_runs(), lambda day, case: LAST_WEEK_TOKENS if day in LAST_WEEK else None)
        week, page = self.produce_week(runs)
        self.assertIsNone(we.chat_tokens(week))
        self.assertNotIn("tokens per run", page)
        runs = with_tokens(timed_runs(), lambda day, case: None if day in LAST_WEEK else 120_000)
        week, page = self.produce_week(runs)
        self.assertEqual(we.chat_tokens(week), "🔢 Tokens: typical 120k per run")
        self.assertIn("middle of 168 counted runs</div>", page)

    def test_partial_counts_are_left_out_of_the_median_and_counted(self):
        # This week's first rep of every cost run is short and billed 10k.
        runs = with_tokens(timed_runs(), lambda day, case: LAST_WEEK_TOKENS if day in LAST_WEEK else THIS_WEEK_TOKENS[CASES[case]],
                           partial=lambda day, case, n: day in THIS_WEEK and CASES[case] == "cost" and n == 1)
        for r in runs:
            for t in r["tasks"]:
                for rep in t["reps"]:
                    if rep["tokens"]["partial"]:
                        rep["tokens"]["total"] = 10_000
        week, page = self.produce_week(runs)
        overall = week["overall"]
        self.assertEqual(overall["tokens_partial"], 21)
        self.assertEqual(overall["counted_runs"], 147)
        self.assertEqual(overall["tokens_median"], 120_000)
        line = "🔢 Tokens: typical 120k per run (▲ 20% vs last week on the same scenarios) · 21 partial"
        self.assertEqual(we.chat_tokens(week), line)
        self.assertEqual(card_texts(we.chat_card(week)["card"]["sections"][3])[1], line)
        self.assertIn("middle of 147 counted runs; &#9650; 20% vs last week on the same scenarios; 21 partial left out", page)

    def test_a_week_of_only_partial_counts(self):
        runs = with_tokens(timed_runs(), lambda day, case: None if day in LAST_WEEK else 5_000,
                           partial=lambda day, case, n: True)
        week, page = self.produce_week(runs)
        self.assertEqual(we.chat_tokens(week), "🔢 Tokens: no full count · 168 partial")
        self.assertIn("no full count; 168 partial left out", page)

    def test_no_change_on_a_week_with_no_trend(self):
        week, _ = self.produce_week(tokened_runs(truncated=THIS_WEEK[3:]))
        self.assertEqual(we.chat_tokens(week), "🔢 Tokens: typical 120k per run")

    def test_infra_reps_and_one_week_scenarios_are_not_counted(self):
        runs = tokened_runs()
        for r in runs:
            for t in r["tasks"]:
                t["reps"].append({"n": 4, "result": "infra", "reason": "KUBE_AGENTS_INFRA_FAILURE", "agent_s": 1.0,
                                  "tokens": {"input": 1, "output": 1, "total": 2, "partial": True}})
        week, _ = self.produce_week(runs)
        self.assertEqual(week["overall"]["counted_runs"], 168)
        self.assertEqual(week["overall"]["tokens_partial"], 0)

    def test_the_change_compares_only_scenarios_fully_counted_both_weeks(self):
        # Last week only cost had full counts (100k). This week chat and obs
        # are fully counted at 200k too, so the week's median is 200k, but
        # cost, the one scenario counted both weeks, held at 100k: steady.
        runs = with_tokens(timed_runs(), lambda day, case: 100_000 if day in LAST_WEEK or CASES[case] == "cost" else 200_000,
                           partial=lambda day, case, n: day in LAST_WEEK and CASES[case] != "cost")
        week, page = self.produce_week(runs)
        self.assertEqual(week["overall"]["tokens_median"], 200_000)
        self.assertEqual(week["overall"]["paired_tokens_median"], 100_000)
        self.assertEqual(we.chat_tokens(week), "🔢 Tokens: typical 200k per run (flat vs last week on the same scenarios)")
        self.assertIn("; flat vs last week on the same scenarios", page)

    def test_a_scenario_keeping_one_full_count_does_not_move_the_change(self):
        # Chat at 50k and the rest at 150k, both weeks. This week every
        # non-chat scenario keeps one full count, the rest partial. Pooled,
        # the 63 chat counts would pull the median to 50k (a 67% drop);
        # each scenario weighs the same, so nothing changed.
        runs = with_tokens(timed_runs(), lambda day, case: 50_000 if CASES[case] == "chat" else 150_000,
                           partial=lambda day, case, n: day in THIS_WEEK and CASES[case] != "chat"
                           and (day, n) != (THIS_WEEK[0], 1))
        week, page = self.produce_week(runs)
        self.assertEqual(week["overall"]["paired_tokens_median"], 150_000)
        self.assertEqual(week["overall"]["prev_paired_tokens_median"], 150_000)
        self.assertEqual(we.tokens_change(week["overall"]), 0)
        self.assertIn("(flat vs last week on the same scenarios)", we.chat_tokens(week))
        self.assertIn("; flat vs last week on the same scenarios", page)

    def test_the_page_and_the_card_show_the_same_numbers(self):
        for runs in (tokened_runs(), with_tokens(timed_runs(), lambda day, case: 1_234_567),
                     with_tokens(timed_runs(), lambda day, case: 200_000 if day in LAST_WEEK else 150_000)):
            week, page = self.produce_week(runs)
            figures = we.tokens_figures(week["overall"])
            line = we.chat_tokens(week)
            self.assertEqual(card_texts(we.chat_card(week)["card"]["sections"][3])[1], line)
            self.assertIn(line, we.chat_text(week).splitlines())
            self.assertIn(f"{figures['typical']}</div>", page)
            self.assertIn(f"typical {figures['typical']} per run", line)
            pct = figures["pct"]
            if pct:
                self.assertIn(f"{abs(pct)}% vs last week", page)
                self.assertIn(f"{abs(pct)}% vs last week on the same scenarios", line)
            else:
                self.assertIn("flat vs last week on the same scenarios", page)
                self.assertIn("flat vs last week on the same scenarios", line)


def card_texts(section):
    """Each widget's text, a decoratedText's or a textParagraph's."""
    return [(w.get("decoratedText") or w.get("textParagraph") or {}).get("text") for w in section["widgets"]]


class ChatCard(Base):
    """The weekly Chat post as a cardsV2 card: the same week as chat_text."""

    def card(self, runs, store=None):
        week, _, failures = we.produce_week(data_of(runs), store or store_of(runs), self.rows(), WEEK_END, bugs_of(), NOW, REPO, self.root)
        self.assertEqual(failures, [])
        return week, we.chat_card(week)

    def test_a_trend_week(self):
        week, card = self.card(timed_runs())
        self.assertEqual(card["cardId"], "weekly-health")
        self.assertEqual(card["card"]["header"], {"title": "kube-agents · weekly health", "subtitle": "Nightly evals · Oct 12–18",
                                                  "imageUrl": "https://gke-labs.github.io/kube-agents/agent-icon.png", "imageType": "SQUARE"})
        lead, attention, areas, time, close = card["card"]["sections"]
        self.assertNotIn("header", lead)
        self.assertEqual(card_texts(lead), ["<b>63% of test runs passed</b>", "105 of 168 runs · 8 scenarios",
                                            f'<font color="{we.BAD}">▼ 12 points from last week</font>'])
        self.assertEqual(attention["header"], "Needs attention")
        self.assertEqual(card_texts(attention), ["🟠 Observability 50%, few scenarios · 1 open bug", "🔴 Cost 33%, down 34 · 0 open bugs"])
        self.assertEqual([w["decoratedText"]["button"] for w in attention["widgets"]],
                         [{"text": "View", "onClick": {"openLink": {"url": we.bugs_url(REPO, slug)}}} for slug in ("obs", "cost")])
        self.assertEqual(areas["header"], "By area")
        self.assertEqual(card_texts(areas), ["Chat  ▰▰▰▰▰▰▰▰▰▰  100%", "Observability  ▰▰▰▰▰▱▱▱▱▱  50%",
                                             "Cost  ▰▰▰▱▱▱▱▱▱▱  33%"])
        # Long rows wrap rather than cut off the figures at the end.
        self.assertTrue(all(w["decoratedText"]["wrapText"] for w in attention["widgets"] + areas["widgets"]))
        # Three areas: nothing to collapse.
        self.assertNotIn("collapsible", areas)
        self.assertEqual(time, {"header": "Task time", "widgets": [
            {"textParagraph": {"text": "⏱ Typical 4 min · slowest 10% over 10 min · Slowest area: Cost (10 min)"}}]})
        self.assertNotIn("header", close)
        self.assertEqual(close["widgets"][0]["buttonList"]["buttons"], [
            {"text": "Full report", "onClick": {"openLink": {"url": "https://storage.cloud.google.com/kube-agents-dashboards/evals/health-email.html"}}},
            {"text": "Dashboard", "onClick": {"openLink": {"url": "https://storage.cloud.google.com/kube-agents-dashboards/evals/index.html"}}},
            {"text": "All open bugs", "onClick": {"openLink": {
                "url": "https://github.com/gke-labs/kube-agents/issues?q=is%3Aissue%20is%3Aopen%20label%3Anightly-red"}}},
        ])
        self.assertEqual(card_texts(close)[1], f'<font color="{we.FAINT}">Model: {MODEL} · next mode</font>')
        # The page's numbers, and the plain-text post beside it.
        self.assertIn("down 12 points from last week", we.chat_text(week))
        for url in (we.bugs_url(REPO, "obs"), we.bugs_url(REPO, "cost")):
            self.assertTrue(we.well_formed(url))

    def test_an_up_week_and_a_steady_week(self):
        week, _ = self.card(timed_runs())
        week["overall"].update(trend=we.TREND_UP, change=4)
        self.assertEqual(card_texts(we.chat_card(week)["card"]["sections"][0])[2], f'<font color="{we.GOOD}">▲ 4 points from last week</font>')
        week["overall"].update(trend=we.TREND_STEADY, change=1)
        self.assertEqual(card_texts(we.chat_card(week)["card"]["sections"][0])[2], "→ steady with last week")

    def test_a_no_trend_week_says_why(self):
        _, card = self.card(timed_runs(truncated=THIS_WEEK[3:]))
        self.assertEqual(card_texts(card["card"]["sections"][0]), [
            "<b>63% of test runs passed</b>", "45 of 72 runs · 8 scenarios", "No trend this week: 3 of 7 nights counted (4 cut off)"])
        self.assertIn("🔴 Cost 33% · 0 open bugs", card_texts(card["card"]["sections"][1]))

    def test_a_short_last_week_is_the_one_named(self):
        _, card = self.card(week_runs(days=THIS_WEEK + LAST_WEEK[:3]))
        self.assertIn("No trend this week: 3 of last week's 7 nights counted (4 not run)", card_texts(card["card"]["sections"][0]))

    def test_a_model_change_is_said_and_both_models_named(self):
        runs = week_runs()
        _, card = self.card(runs, store_of(runs, models={d: OTHER_MODEL for d in THIS_WEEK}))
        lead = card_texts(card["card"]["sections"][0])
        self.assertEqual(lead[-1], f"This week ran {OTHER_MODEL}; last week ran {MODEL}, so the change compares two models.")
        self.assertIn(f"Models: {OTHER_MODEL} (this week), {MODEL} (last week) · next mode", card_texts(card["card"]["sections"][-1])[1])

    def test_no_area_needing_attention_omits_the_section_and_no_timing(self):
        cases = {c: d for c, d in CASES.items() if d == "chat"}
        runs = [run(int(r["started"][8:10]), [{**t, "reps": [{k: v for k, v in rep.items() if k != "agent_s"} for rep in t["reps"]]}
                                              for t in r["tasks"]]) for r in week_runs(cases=cases)]
        week = self.week(data_of(runs, cases), store_of(runs))
        we.render(week, bugs_of([]))
        card = we.chat_card(week)
        headers = [s.get("header") for s in card["card"]["sections"]]
        self.assertEqual(headers, [None, "By area", "Task time", None])
        self.assertEqual(card_texts(card["card"]["sections"][2]), ["⏱ No timing on record"])

    def test_bars_have_ten_steps_and_only_a_full_score_fills_one(self):
        self.assertEqual(we.card_bar(0), "▱▱▱▱▱▱▱▱▱▱")
        self.assertEqual(we.card_bar(37), "▰▰▰▱▱▱▱▱▱▱")
        self.assertEqual(we.card_bar(96), "▰▰▰▰▰▰▰▰▰▱")
        self.assertEqual(we.card_bar(100), "▰▰▰▰▰▰▰▰▰▰")

    def test_more_areas_than_shown_collapse_to_the_best(self):
        week, _ = self.card(timed_runs())
        with unittest.mock.patch.object(we, "CARD_AREAS_SHOWN", 2):
            areas = we.chat_card(week)["card"]["sections"][2]
        self.assertTrue(areas["collapsible"])
        self.assertEqual(areas["uncollapsibleWidgetsCount"], 2)
        self.assertTrue(card_texts(areas)[0].startswith("Chat  "))

    def test_names_are_escaped_for_the_cards_html(self):
        week, _ = self.card(timed_runs())
        week["rows"][0]["name"] = "A <b> & B"
        self.assertTrue(card_texts(we.chat_card(week)["card"]["sections"][2])[0].startswith("A &lt;b&gt; &amp; B  "))

    def test_the_message_carries_the_text_as_the_fallback(self):
        week, card = self.card(timed_runs())
        body = post_health.Sender.body(we.chat_text(week), card)
        # The fallback is a notification's plain text: Chat's markup is dropped.
        self.assertEqual(body["cardsV2"], [card])
        fallback = body["fallbackText"]
        self.assertTrue(fallback.startswith("kube-agents weekly health · Oct 12–18\n63% of nightly test runs passed"))
        self.assertIn("• Observability: 50%, few scenarios, 1 open bug", fallback)
        self.assertIn("full report · dashboard", fallback)
        self.assertNotIn("*", fallback)
        self.assertNotIn("<", fallback)
        self.assertEqual(post_health.Sender.body("plain"), {"text": "plain"})


if __name__ == "__main__":
    unittest.main()
