"""coverage_gaps.py: one `presubmit-gate` issue per domain with no blocking
roster seat, kept current, closed when the domain is covered again; and the
digest's line per uncovered domain.

The checkout is synthetic (domains.yaml, three roster files, task.yaml per
case) and the nights are built with test_eval_dashboard_nightly_bugs.py's
helpers, whose FakeGh stands in for `gh api`.
"""

import contextlib
import datetime
import io
import json
import os
import pathlib
import subprocess
import tempfile
import textwrap
import types
import typing
import unittest
import unittest.mock

import yaml
from eval_dashboard import coverage_gaps, nightly_bugs, post_health
from test_eval_dashboard_nightly_bugs import FakeGh, night, task

ISSUES = "repos/gke-labs/kube-agents/issues"
NOW = "2026-10-26T13:00:00+00:00"
WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci-health.yml"
STEP_NAME = "File an issue per domain with no presubmit seat (dry run unless armed)"
POST_STEP_NAME = "Post on state change or digest"


class Expired(typing.NamedTuple):
    slug: str
    owner: str | None
    issue: int | None
    expires: datetime.date
    days_past: int


def fake_domain_allowlist():
    """domain_allowlist's interface (#3981): load() -> (owners, entries) and
    expired_records(entries, owners, today); a record holds on its day."""
    def load(path):
        doc = yaml.safe_load(path.read_text())
        return {r["slug"]: r.get("owner") for r in doc["domains"]}, doc.get("allowlist") or []

    def expired_records(entries, owners, today):
        return [Expired(e["slug"], e.get("owner") or owners.get(e["slug"]), e.get("issue"), e["expires"], (today - e["expires"]).days)
                for e in entries if isinstance(e, dict) and isinstance(e.get("expires"), datetime.date) and e["expires"] < today]

    return types.SimpleNamespace(load=load, expired_records=expired_records)


try:
    import domain_allowlist as REAL_DOMAIN_ALLOWLIST
except ImportError:
    REAL_DOMAIN_ALLOWLIST = None

DOMAINS = """\
domains:
  - slug: alpha
    name: Alpha
    owner: alice
    backup: bob
    meaning: Checks alpha.
  - slug: beta
    name: Beta
    owner: carol
    backup: dave
    meaning: Checks beta.
  - slug: gamma
    name: Gamma
    owner: erin
    backup: frank
    meaning: Checks gamma.

allowlist:
  # beta, since 2026-10-17 (#3120): its one roster case was demoted.
  - beta
  - slug: gamma
    since: 2026-10-21
    issue: 3598
    owner: grace
    expires: 2026-11-04
    held_out: gamma-held
"""
SPEC = "verification_spec:\n  - type: reply_contains\n    phrases: [x]\n"
CASES = {
    "alpha-seat": ("alpha", SPEC),
    "beta-good": ("beta", SPEC),
    "beta-flaky": ("beta", SPEC),
    "beta-young": ("beta", SPEC),
    "beta-xfail": ("beta", SPEC + "expected_fail: true\n"),
    "gamma-held": ("gamma", SPEC),
}


def three(name, *results):
    return task(name, *results)


def nights(beta_flaky=("pass", "fail", "fail")):
    """Seven nights: beta-good passes every rep (21/21), beta-flaky passes
    one in three, beta-young ran only the last night, gamma-held 18/21."""
    runs = []
    for day in range(14, 21):
        tasks = [three("alpha-seat", "pass", "pass", "pass"), three("beta-good", "pass", "pass", "pass"),
                 three("beta-flaky", *beta_flaky), three("beta-xfail", "fail", "fail", "fail"),
                 three("gamma-held", "pass", "pass", "pass" if day > 16 else "fail")]
        if day == 20:
            tasks.append(three("beta-young", "pass", "pass", "pass"))
        runs.append(night(day, tasks))
    return runs


def data_doc(runs=None):
    cases = [{"name": name, "domain": domain, "active": name in ("alpha-seat", "gamma-held"), "nightly_active": True} for name, (domain, _) in CASES.items()]
    return {"schema_version": 1, "generated_at": NOW, "cases": cases, "runs": nights() if runs is None else runs}


def bot_issue(number, slug, body=None, title=None, **extra):
    issue = {"number": number, "title": title or coverage_gaps.TITLE.format(slug=slug), "body": body if body is not None else coverage_gaps.MARKER.format(slug=slug) + "\nold body",
             "created_at": "2026-10-18T13:00:00Z", "comments": 0}
    issue.update(extra)
    return issue


class Harness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        self.root = self.dir / "repo"
        (self.root / "docs" / "designs").mkdir(parents=True)
        (self.root / "docs" / "designs" / "domains.yaml").write_text(DOMAINS)
        for name, (domain, extra) in CASES.items():
            path = self.root / "bench" / "tasks" / name
            path.mkdir(parents=True)
            (path / "task.yaml").write_text(f"name: {name}\ndomain: {domain}\n{extra}")
        (self.root / "hack" / "eval").mkdir(parents=True)
        self.roster(["alpha-seat"])
        (self.root / "hack" / "eval" / "presubmit-cases.txt").write_text("./tasks/alpha-seat/task.yaml\n# held out\n./tasks/gamma-held/task.yaml\n")
        sleep = unittest.mock.patch.object(nightly_bugs.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)
        allowlist = unittest.mock.patch.object(coverage_gaps, "domain_allowlist", fake_domain_allowlist())
        allowlist.start()
        self.addCleanup(allowlist.stop)

    def roster(self, names):
        (self.root / "hack" / "eval" / "blocking-roster.txt").write_text("# the roster\n" + "".join(f"{n}\n" for n in names))

    def gaps(self, doc=None, store=None):
        return coverage_gaps.find_gaps(self.root, doc or data_doc(), store, datetime.datetime.fromisoformat(NOW))

    def run_once(self, gh=None, live=True, doc=None, store=None):
        gh = gh or FakeGh()
        path = self.dir / "data.json"
        path.write_text(json.dumps(doc or data_doc()))
        argv = ["--data", str(path), "--repo-root", str(self.root), "--now", NOW] + (["--live"] if live else [])
        if store is not None:
            store_path = self.dir / "store.json"
            store_path.write_text(json.dumps(store))
            argv += ["--store", str(store_path)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = coverage_gaps.main(argv, gh_runner=gh)
        self.assertEqual(rc, 0, err.getvalue())
        return gh, out.getvalue(), err.getvalue()

    def posted(self, gh):
        return [b for m, p, b in gh.writes() if m == "POST" and p == ISSUES]


class FindGapsTest(Harness):
    def test_uncovered_domains_their_owner_how_long_and_why(self):
        beta, gamma = self.gaps()
        self.assertEqual([beta["slug"], gamma["slug"]], ["beta", "gamma"], "alpha has a roster seat; oldest gap first")
        self.assertEqual((beta["owner"], beta["backup"], beta["days"], beta["issue"]), ("carol", "dave", 9, 3120), "since and issue from the comment above a bare slug")
        self.assertEqual((gamma["owner"], gamma["days"], gamma["issue"], gamma["held_out_case"]), ("grace", 5, 3598, "gamma-held"), "a record's own fields; its owner wins")

    def test_candidates_are_ranked_by_record_against_the_bar(self):
        beta, gamma = self.gaps()
        self.assertEqual([c["case"] for c in beta["candidates"]], ["beta-good", "beta-young", "beta-flaky"], "meeting the bar first, then by rate; expected_fail is left out")
        good, young, flaky = beta["candidates"]
        self.assertEqual((good["passed"], good["graded"], good["nights"], good["meets_bar"]), (21, 21, 7, True))
        self.assertEqual((flaky["passed"], flaky["graded"], flaky["meets_bar"]), (7, 21, False))
        self.assertEqual((young["graded"], young["meets_bar"]), (3, False), "100% over 3 runs is too few for the bar")
        self.assertEqual(coverage_gaps.bar_text(young), "too few runs, 3 of 20")
        [held] = gamma["candidates"]
        self.assertTrue(held["held_out"], "on the presubmit file, off the roster")
        self.assertEqual((held["passed"], held["graded"], held["meets_bar"]), (18, 21, False))

    def test_a_case_without_a_spec_is_no_candidate(self):
        (self.root / "bench" / "tasks" / "beta-young" / "task.yaml").write_text("name: beta-young\ndomain: beta\n")
        self.assertNotIn("beta-young", [c["case"] for c in self.gaps()[0]["candidates"]])

    def test_a_rate_just_under_the_bar_never_reads_as_the_bar(self):
        self.assertEqual(coverage_gaps.percent(26 / 29), "89%")
        self.assertEqual(coverage_gaps.percent(37 / 41), "90%")

    def test_a_since_written_as_a_timestamp_is_its_day(self):
        (self.root / "docs" / "designs" / "domains.yaml").write_text(DOMAINS.replace("since: 2026-10-21", "since: 2026-10-21 09:00:00"))
        gamma = next(g for g in self.gaps() if g["slug"] == "gamma")
        self.assertEqual((gamma["since"], gamma["days"]), (datetime.date(2026, 10, 21), 5))
        self.assertIsNotNone(self.run_once())

    def test_the_store_column_reads_the_next_mode_series(self):
        next_mode = {"cases": {"beta-good": {"record": {"state": "would-admit", "runs": 21, "passes": 21}}}}
        today = {"beta-good": {"record": {"state": "collecting", "runs": 3, "passes": 3}}}
        with unittest.mock.patch.object(coverage_gaps.trend, "trend_document", return_value={"cases": today, "next": next_mode}):
            self.assertEqual(coverage_gaps.store_records({}, data_doc())["beta-good"]["state"], "would-admit")
        with unittest.mock.patch.object(coverage_gaps.trend, "trend_document", return_value={"cases": today, "next": None}):
            self.assertEqual(coverage_gaps.store_records({}, data_doc())["beta-good"]["state"], "collecting", "today's only without next-mode records")

    def test_a_store_written_back_after_a_failed_read_is_not_this_ticks(self):
        self.assertIsNone(coverage_gaps.store_records({"error": "store.py timed out", "records": []}, data_doc()))
        self.assertIsNone(coverage_gaps.store_records({"records": [], "partial": {"fetched": 800, "remaining": 412}}, data_doc()), "a read stopped at its deadline")

    def test_a_since_after_today_reads_as_not_recorded(self):
        (self.root / "docs" / "designs" / "domains.yaml").write_text(DOMAINS.replace("since: 2026-10-21", "since: 2026-11-30"))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            gamma = next(g for g in self.gaps() if g["slug"] == "gamma")
        self.assertEqual((gamma["since"], gamma["days"]), (None, None))
        self.assertIn("after today", err.getvalue())
        # A not-planned close then holds by the undated rule.
        closed = bot_issue(4221, "gamma", state="closed", state_reason="not_planned", closed_at="2026-10-25T10:00:00Z")
        gh, _, _ = self.run_once(FakeGh(closed_issues=[closed]))
        self.assertEqual([b["labels"][1] for b in self.posted(gh)], ["domain:beta"])

    def test_a_roster_case_without_a_spec_covers_nothing(self):
        # alpha then reads as uncovered off the allowlist, which main's lint
        # forbids: the read is not trusted.
        (self.root / "bench" / "tasks" / "alpha-seat" / "task.yaml").write_text("name: alpha-seat\ndomain: alpha\n")
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaisesRegex(coverage_gaps.BadRead, "alpha"):
            self.gaps()

    def test_the_evidence_store_adds_its_verdict_and_ranks_would_admit_first(self):
        records = {"beta-flaky": {"state": "would-admit", "runs": 21, "passes": 20}, "beta-good": {"state": "collecting", "runs": 9, "passes": 9}}
        with unittest.mock.patch.object(coverage_gaps, "store_records", return_value=records):
            beta, _ = self.gaps(store={})
        self.assertEqual([c["case"] for c in beta["candidates"]][:2], ["beta-flaky", "beta-good"])
        self.assertEqual(coverage_gaps.store_text(beta["candidates"][1]["store"]), "collecting, 9/9, 11 more runs for a full window")
        self.assertEqual(coverage_gaps.store_text(beta["candidates"][2]["store"]), "no record at the current key")
        self.assertEqual(coverage_gaps.store_text(None), "not read this tick")

    def test_allowlist_entries_read_as_bare_slugs_or_records(self):
        self.assertEqual(coverage_gaps.allowlist_entry("beta")["slug"], "beta")
        record = coverage_gaps.allowlist_entry({"slug": "gamma", "since": "2026-10-21", "issue": "#3598", "expires": datetime.date(2026, 11, 4)})
        self.assertEqual((record["since"], record["issue"]), (datetime.date(2026, 10, 21), 3598))
        self.assertIsNone(coverage_gaps.allowlist_entry({"since": "2026-10-21"}))
        self.assertIsNone(coverage_gaps.allowlist_entry(""))
        self.assertIsNone(coverage_gaps.allowlist_entry({"slug": "x", "since": "soon"})["since"])

    def test_a_bad_or_misplaced_comment_date_reads_as_not_recorded(self):
        line = "  # beta, since 2026-10-17 (#3120): its one roster case was demoted.\n"
        for comment, since in (
            ("  # beta, since 2026-13-17 (#3120): typo.\n", None),
            ("  # beta: demoted.\n  # since 2026-09-29 it is seated held out.\n", None),
            (line + "\n", datetime.date(2026, 10, 17)),
        ):
            (self.root / "docs" / "designs" / "domains.yaml").write_text(DOMAINS.replace(line, comment))
            with contextlib.redirect_stderr(io.StringIO()):
                beta = next(g for g in self.gaps() if g["slug"] == "beta")
            self.assertEqual(beta["since"], since, comment)

    def test_both_day_counts_roll_over_on_the_same_utc_day(self):
        (self.root / "docs" / "designs" / "domains.yaml").write_text(DOMAINS.replace("expires: 2026-11-04", "expires: 2026-10-23"))
        late = datetime.datetime(2026, 10, 27, 1, 0, tzinfo=datetime.timezone.utc)  # Oct 26, 9 PM in Toronto
        gamma = coverage_gaps.find_gaps(self.root, data_doc(), None, late)[-1]
        self.assertEqual((gamma["days"], gamma["expired_days"]), (6, 4))

    def test_an_entry_with_no_date_says_so(self):
        text = DOMAINS.replace("  # beta, since 2026-10-17 (#3120): its one roster case was demoted.\n", "  # beta, since the row was added.\n")
        (self.root / "docs" / "designs" / "domains.yaml").write_text(text)
        beta = next(g for g in self.gaps() if g["slug"] == "beta")
        self.assertIsNone(beta["days"])
        self.assertIn("uncovered, start date not recorded (owner: carol)", coverage_gaps.digest_lines([beta])[0])


class FilingTest(Harness):
    def test_files_one_issue_per_uncovered_domain_assigned_to_the_owner(self):
        gh, out, _ = self.run_once()
        beta, gamma = self.posted(gh)
        self.assertEqual(beta["labels"], ["presubmit-gate", "domain:beta"])
        self.assertEqual(beta["assignees"], ["carol"])
        self.assertEqual(gamma["assignees"], ["grace"], "the record's owner")
        self.assertTrue(beta["body"].startswith(coverage_gaps.MARKER.format(slug="beta")))
        self.assertIn("for 9 days (since 2026-10-17)", beta["body"])
        self.assertIn("Backup: @dave.", beta["body"])
        self.assertIn("for #3120 since 2026-10-17", beta["body"])
        self.assertIn("| `beta-good` | 21/21 over 7 nights (100%) | meets 90% | not read this tick |", beta["body"])
        self.assertIn("`gamma-held` still runs on the presubmit as a held-out seat.", gamma["body"])
        self.assertIn("**Assigned to** @carol (the domain owner in `docs/designs/domains.yaml`).", beta["body"])
        self.assertIn("**Assigned to** @grace (the owner on the domain's allowlist record).", gamma["body"])
        self.assertIn("| `gamma-held` (held out on the presubmit) | 18/21 over 7 nights (85%) | below 90% |", gamma["body"])
        self.assertTrue(out.startswith("coverage gaps (live): file 2, update 0,"), out)

    def test_a_refused_owner_files_unassigned_and_says_so(self):
        gh, _, _ = self.run_once(FakeGh(refuse_assignees=("carol",)))
        posted = self.posted(gh)
        unassigned = [b for b in posted if "assignees" not in b]
        self.assertEqual(len(unassigned), 1)
        self.assertIn("**Unassigned:** GitHub refused `carol`", unassigned[0]["body"])

    def test_an_open_issue_is_edited_in_place_and_left_alone_when_current(self):
        gaps = {g["slug"]: g for g in self.gaps()}
        _, current = coverage_gaps.render_issue(gaps["gamma"], assignees=["grace"])
        gh, out, _ = self.run_once(FakeGh(open_issues=[bot_issue(4200, "beta"), bot_issue(4201, "gamma", body=current, assignees=[{"login": "grace"}])]))
        self.assertEqual(self.posted(gh), [], "nothing filed a second time")
        [(method, path, body)] = gh.writes()
        self.assertEqual((method, path), ("PATCH", f"{ISSUES}/4200"))
        self.assertIn("for 9 days (since 2026-10-17)", body["body"])
        self.assertNotIn("assignees", body, "assignees are never touched")
        self.assertIn("unchanged: gamma on #4201", out)

    def test_the_assignment_line_reads_the_assignees_github_holds(self):
        gh, _, _ = self.run_once(FakeGh(open_issues=[bot_issue(4200, "beta", assignees=[{"login": "zoe"}])]))
        [body] = [b["body"] for m, p, b in gh.writes() if m == "PATCH"]
        self.assertIn("**Assigned to** @zoe. Owner: @carol (the domain owner in `docs/designs/domains.yaml`). Backup: @dave.", body)
        gh, _, _ = self.run_once(FakeGh(open_issues=[bot_issue(4200, "beta")]))
        [body] = [b["body"] for m, p, b in gh.writes() if m == "PATCH"]
        self.assertIn("**Unassigned.** Owner: @carol", body, "never claims an assignment GitHub does not hold")
        refused = bot_issue(4200, "beta", body=coverage_gaps.MARKER.format(slug="beta") + "\n**Unassigned:** GitHub refused `carol`")
        gh, _, _ = self.run_once(FakeGh(open_issues=[refused]))
        [body] = [b["body"] for m, p, b in gh.writes() if m == "PATCH"]
        self.assertIn("**Unassigned:** GitHub refused `carol`", body)
        # The owner corrected in domains.yaml: GitHub never saw the new login.
        (self.root / "docs" / "designs" / "domains.yaml").write_text(DOMAINS.replace("owner: carol", "owner: carol2"))
        gh, _, _ = self.run_once(FakeGh(open_issues=[refused]))
        [body] = [b["body"] for m, p, b in gh.writes() if m == "PATCH"]
        self.assertNotIn("refused", body)
        self.assertIn("**Unassigned.** Owner: @carol2", body)

    def test_an_issue_as_filed_is_not_rewritten_on_the_next_tick(self):
        filed = FakeGh()
        self.run_once(filed)
        issues = [dict(b, number=4300 + i, created_at="2026-10-26T13:00:00Z", comments=0, assignees=[{"login": b["assignees"][0]}])
                  for i, b in enumerate(self.posted(filed))]
        gh, out, _ = self.run_once(FakeGh(open_issues=issues))
        self.assertEqual(gh.writes(), [])
        self.assertIn("unchanged: beta on #4300", out)
        self.assertIn("Closing it as not planned keeps it closed for this gap", issues[0]["body"])

    def test_a_human_issue_without_the_marker_is_not_taken_for_the_bots(self):
        human = {"number": 4300, "title": "beta has no presubmit seat", "body": "beta is uncovered", "created_at": "2026-10-18T00:00:00Z"}
        gh, _, _ = self.run_once(FakeGh(open_issues=[human]))
        self.assertEqual(len(self.posted(gh)), 2)

    def test_a_covered_domain_gets_one_comment_and_its_issue_closed(self):
        open_issues = [bot_issue(4210, "alpha")]
        gh, out, _ = self.run_once(FakeGh(open_issues=open_issues))
        writes = [(m, p) for m, p, _ in gh.writes() if p != ISSUES]
        self.assertEqual(writes, [("POST", f"{ISSUES}/4210/comments"), ("PATCH", f"{ISSUES}/4210")])
        comment = next(b for m, p, b in gh.writes() if p.endswith("/comments"))["body"]
        self.assertIn(coverage_gaps.COVERED_MARKER.format(slug="alpha"), comment)
        self.assertIn("`alpha-seat` on `hack/eval/blocking-roster.txt`", comment)
        self.assertEqual(next(b for m, p, b in gh.writes() if m == "PATCH"), {"state": "closed", "state_reason": "completed"})
        self.assertIn("close: alpha #4210 (covered by alpha-seat)", out)

    def test_a_close_whose_comment_landed_is_finished_without_a_second_comment(self):
        marker = coverage_gaps.COVERED_MARKER.format(slug="alpha")
        gh, _, _ = self.run_once(FakeGh(open_issues=[bot_issue(4210, "alpha", comments=1)], comments={4210: [{"body": marker}]}))
        self.assertEqual([(m, p) for m, p, _ in gh.writes() if p != ISSUES], [("PATCH", f"{ISSUES}/4210")])

    def test_a_close_whose_comments_cannot_be_read_waits(self):
        gh, out, _ = self.run_once(FakeGh(open_issues=[bot_issue(4210, "alpha")], fail_comments=True))
        self.assertEqual([(m, p) for m, p, _ in gh.writes() if p != ISSUES], [])
        self.assertIn("failed: alpha", out)

    def test_seating_a_case_closes_the_issue(self):
        self.roster(["alpha-seat", "beta-good"])
        gh, _, _ = self.run_once(FakeGh(open_issues=[bot_issue(4200, "beta")]))
        self.assertIn(("PATCH", f"{ISSUES}/4200"), [(m, p) for m, p, _ in gh.writes()])
        self.assertEqual([b["labels"][1] for b in self.posted(gh)], ["domain:gamma"])

    def test_an_issue_for_a_domain_gone_from_domains_yaml_is_left_open(self):
        gh, _, err = self.run_once(FakeGh(open_issues=[bot_issue(4230, "zeta")]))
        self.assertEqual([(m, p) for m, p, _ in gh.writes() if p != ISSUES], [])
        self.assertIn("left open", err)

    def bad_read(self):
        """main --live against the current checkout: (rc, gh, stderr)."""
        gh = FakeGh(open_issues=[bot_issue(4200, "beta"), bot_issue(4210, "alpha")])
        path = self.dir / "data.json"
        path.write_text(json.dumps(data_doc()))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            rc = coverage_gaps.main(["--data", str(path), "--repo-root", str(self.root), "--live"], gh_runner=gh)
        return rc, gh, err.getvalue()

    def test_a_truncated_roster_read_writes_nothing(self):
        self.roster(["no-such-case"])  # alpha's seat lost from the read
        rc, gh, err = self.bad_read()
        self.assertEqual(rc, 1)
        self.assertEqual(gh.calls, [], "nothing filed, updated or closed")
        self.assertIn("alpha reads as uncovered but is not on the allowlist", err)
        with unittest.mock.patch.object(coverage_gaps, "REPO_ROOT", self.root), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(post_health.coverage_lines(data_doc(), datetime.datetime.fromisoformat(NOW)), [], "the digest claims no gap")

    def test_a_missing_bench_tasks_writes_nothing(self):
        import shutil
        shutil.rmtree(self.root / "bench" / "tasks")
        rc, gh, err = self.bad_read()
        self.assertEqual((rc, gh.calls), (1, []))
        self.assertIn("alpha reads as uncovered", err)

    def test_a_normal_read_still_files(self):
        rc, gh, _ = self.bad_read()
        self.assertEqual(rc, 0)
        self.assertEqual([b["labels"][1] for b in self.posted(gh)], ["domain:gamma"])
        self.assertIn(("PATCH", f"{ISSUES}/4210"), [(m, p) for m, p, _ in gh.writes()], "alpha's issue closed")

    def test_an_unreadable_domains_yaml_writes_nothing(self):
        (self.root / "docs" / "designs" / "domains.yaml").write_text("domains: [\n")
        gh = FakeGh(open_issues=[bot_issue(4200, "beta")])
        path = self.dir / "data.json"
        path.write_text(json.dumps(data_doc()))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            rc = coverage_gaps.main(["--data", str(path), "--repo-root", str(self.root), "--live"], gh_runner=gh)
        self.assertEqual(rc, 1)
        self.assertEqual(gh.calls, [], "not one issue closed as covered")
        self.assertIn("gave no domains", err.getvalue())

    def test_a_tick_with_no_cases_in_data_json_keeps_the_body(self):
        gh, _, _ = self.run_once(FakeGh(open_issues=[bot_issue(4200, "beta"), bot_issue(4201, "gamma")]), doc={"schema_version": 1, "cases": [], "runs": []})
        self.assertEqual(gh.writes(), [])
        gaps = self.gaps({"schema_version": 1, "cases": [], "runs": []})
        self.assertIn("candidates not read (no cases in data.json)", coverage_gaps.digest_lines(gaps)[0])

    def test_a_not_planned_close_after_the_gap_began_is_respected(self):
        closed = bot_issue(4220, "beta", state="closed", state_reason="not_planned", closed_at="2026-10-20T10:00:00Z")
        gh, out, _ = self.run_once(FakeGh(closed_issues=[closed]))
        self.assertEqual([b["labels"][1] for b in self.posted(gh)], ["domain:gamma"])
        self.assertIn("closed by a person: beta #4220", out)
        # Undated: a close within the last 90 days holds, an older one (the
        # listing filters on the last update, not the close) does not.
        text = DOMAINS.replace("  # beta, since 2026-10-17 (#3120): its one roster case was demoted.\n", "")
        (self.root / "docs" / "designs" / "domains.yaml").write_text(text)
        gh, _, _ = self.run_once(FakeGh(closed_issues=[closed]))
        self.assertEqual([b["labels"][1] for b in self.posted(gh)], ["domain:gamma"])
        stale = dict(closed, closed_at="2025-06-01T10:00:00Z")
        gh, _, _ = self.run_once(FakeGh(closed_issues=[stale]))
        self.assertEqual(len(self.posted(gh)), 2)
        (self.root / "docs" / "designs" / "domains.yaml").write_text(DOMAINS)
        # A not-planned close from before this gap began (inside 90 days):
        # holds an undated gap back, not a dated one.
        earlier = dict(closed, closed_at="2026-10-01T10:00:00Z")
        gh, _, _ = self.run_once(FakeGh(closed_issues=[earlier]))
        self.assertEqual(len(self.posted(gh)), 2, "dated: the close is from an earlier gap")
        (self.root / "docs" / "designs" / "domains.yaml").write_text(text)
        gh, _, _ = self.run_once(FakeGh(closed_issues=[earlier]))
        self.assertEqual([b["labels"][1] for b in self.posted(gh)], ["domain:gamma"], "undated: any close in 90 days")
        (self.root / "docs" / "designs" / "domains.yaml").write_text(DOMAINS)
        # A completed close (the bot's own, from an earlier gap) does not hold a new one back.
        done = dict(closed, state_reason="completed")
        gh, _, _ = self.run_once(FakeGh(closed_issues=[done]))
        self.assertEqual(len(self.posted(gh)), 2)


class ExpiryTest(Harness):
    """A record past its `expires` date fails no pull request; the issue
    says so and mentions the owner and backup at most once a day."""

    def setUp(self):
        super().setUp()
        (self.root / "docs" / "designs" / "domains.yaml").write_text(DOMAINS.replace("expires: 2026-11-04", "expires: 2026-10-23"))

    def expired_issue(self, **extra):
        gamma = next(g for g in self.gaps() if g["slug"] == "gamma")
        return bot_issue(4201, "gamma", body=coverage_gaps.render_issue(gamma)[1], **extra)

    def pings(self, gh):
        return [b["body"] for m, p, b in gh.writes() if m == "POST" and p.endswith("/comments")]

    def test_the_body_and_the_digest_say_how_long_ago_it_expired(self):
        beta, gamma = self.gaps()
        self.assertIsNone(beta["expired_days"], "a bare slug has no expiry")
        self.assertEqual(gamma["expired_days"], 3)
        self.assertIn("**The allowlist exception expired on 2026-10-23, 3 days ago.**", coverage_gaps.render_issue(gamma)[1])
        self.assertIn("gamma uncovered 5 days, exception expired 3 days ago (owner: grace)", coverage_gaps.digest_lines([gamma])[0])

    def test_a_record_not_yet_expired_mentions_no_one(self):
        (self.root / "docs" / "designs" / "domains.yaml").write_text(DOMAINS)
        gamma = self.gaps()[-1]
        self.assertIsNone(gamma["expired_days"])
        self.assertNotIn("expired", coverage_gaps.render_issue(gamma)[1])
        self.assertNotIn("expired", coverage_gaps.digest_lines([gamma])[0])
        gh, _, _ = self.run_once(FakeGh(open_issues=[bot_issue(4201, "gamma")]))
        self.assertEqual(self.pings(gh), [])

    def test_without_domain_allowlist_nothing_expires(self):
        with unittest.mock.patch.object(coverage_gaps, "domain_allowlist", None):
            gamma = self.gaps()[-1]
            self.assertIsNone(gamma["expired_days"])
            gh, _, _ = self.run_once(FakeGh(open_issues=[self.expired_issue()]))
        self.assertEqual(self.pings(gh), [])

    @unittest.skipIf(REAL_DOMAIN_ALLOWLIST is None, "scripts/domain_allowlist.py is not on this checkout yet")
    def test_the_real_domain_allowlist_reads_the_same_expiry(self):
        with unittest.mock.patch.object(coverage_gaps, "domain_allowlist", REAL_DOMAIN_ALLOWLIST):
            gamma = self.gaps()[-1]
        self.assertEqual((gamma["expires"], gamma["expired_days"]), (datetime.date(2026, 10, 23), 3))

    def test_an_open_issue_mentions_the_owner_and_backup(self):
        gh, out, _ = self.run_once(FakeGh(open_issues=[self.expired_issue()]))
        [ping] = self.pings(gh)
        self.assertIn("<!-- kube-agents-bot:coverage-gap-expired domain=gamma day=2026-10-26 -->", ping)
        self.assertIn("@grace @frank: the allowlist exception for `gamma`", ping)
        self.assertIn("expired on 2026-10-23, 3 days ago", ping)
        self.assertIn("expired: gamma on #4201, mention @grace @frank (3 days past)", out)

    def test_at_most_one_mention_a_day(self):
        marker = coverage_gaps.EXPIRED_MARKER.format(slug="gamma", day="2026-10-25")
        recent = {4201: [{"body": marker, "created_at": "2026-10-26T03:00:00Z"}]}
        gh, _, _ = self.run_once(FakeGh(open_issues=[self.expired_issue(comments=1)], comments=recent))
        self.assertEqual(self.pings(gh), [], "10 hours since the last one")
        old = {4201: [{"body": marker, "created_at": "2026-10-25T12:00:00Z"}]}
        gh, _, _ = self.run_once(FakeGh(open_issues=[self.expired_issue(comments=1)], comments=old))
        self.assertEqual(len(self.pings(gh)), 1, "25 hours since the last one")

    def test_an_issue_filed_after_the_expiry_carries_the_mention_itself(self):
        gh, _, _ = self.run_once(FakeGh(open_issues=[self.expired_issue(created_at="2026-10-26T11:00:00Z")]))
        self.assertEqual(self.pings(gh), [])
        posted = FakeGh()
        self.run_once(posted)
        gamma = next(b for b in self.posted(posted) if b["labels"][1] == "domain:gamma")
        self.assertIn("@grace", gamma["body"])
        self.assertIn("Backup: @frank.", gamma["body"])
        self.assertEqual(self.pings(posted), [])

    def test_unreadable_comments_wait_and_a_dry_run_sends_nothing(self):
        gh, out, _ = self.run_once(FakeGh(open_issues=[self.expired_issue()], fail_comments=True))
        self.assertEqual(self.pings(gh), [])
        self.assertIn("failed: gamma", out)
        gh, out, _ = self.run_once(FakeGh(open_issues=[self.expired_issue(comments=2)], comments={4201: []}))
        self.assertEqual(self.pings(gh), [], "fewer comments read than the listing counts")
        gh, out, _ = self.run_once(FakeGh(open_issues=[self.expired_issue()]), live=False)
        self.assertEqual(gh.writes(), [])
        self.assertIn("would mention 1 expired", out)


class ListingTrustTest(Harness):
    def test_an_empty_listing_graphql_contradicts_writes_nothing(self):
        gh, _, err = self.run_once(FakeGh(open_issues=[bot_issue(4200, "beta"), bot_issue(4201, "gamma")], empty_open=True))
        self.assertEqual(gh.writes(), [], "no duplicate filed under an untrusted listing")
        self.assertIn("no trusted presubmit-gate issue listing", err)
        self.assertEqual(sum(1 for m, p, _ in gh.calls if p == "graphql"), nightly_bugs.LISTING_ATTEMPTS)
        [query] = {json.dumps(b["variables"]["label"]) for m, p, b in gh.calls if p == "graphql"}
        self.assertEqual(query, '"presubmit-gate"')

    def test_a_listing_that_recovers_on_a_later_read_is_used(self):
        gh, _, _ = self.run_once(FakeGh(open_issues=[bot_issue(4200, "beta"), bot_issue(4201, "gamma")], empty_open=1))
        self.assertEqual(self.posted(gh), [])

    def test_an_unreadable_cross_check_or_listing_writes_nothing(self):
        for fake in (FakeGh(graphql="fail"), FakeGh(graphql="errors"), FakeGh(fail_list=True), FakeGh(fail_closed=True)):
            gh, _, _ = self.run_once(fake)
            self.assertEqual(gh.writes(), [])

    def test_the_closed_listing_reaches_the_oldest_dated_gap_beside_an_undated_one(self):
        text = DOMAINS.replace("since: 2026-10-21", "since: 2026-06-01").replace("  # beta, since 2026-10-17 (#3120): its one roster case was demoted.\n", "")
        (self.root / "docs" / "designs" / "domains.yaml").write_text(text)
        gh, _, _ = self.run_once()
        self.assertTrue(any("state=closed&since=2026-06-01T00:00:00Z" in p for m, p, _ in gh.calls if m == "GET"))

    def test_the_listings_read_the_presubmit_gate_label(self):
        gh, _, _ = self.run_once()
        listed = {p for m, p, _ in gh.calls if m == "GET"}
        self.assertIn(f"{ISSUES}?labels=presubmit-gate&state=open&per_page=100", listed)
        self.assertTrue(any(p.startswith(f"{ISSUES}?labels=presubmit-gate&state=closed&since=2026-10-17T00:00:00Z") for p in listed))


class DryRunTest(Harness):
    def test_dry_run_writes_nothing_and_says_what_it_would_do(self):
        gh, out, err = self.run_once(FakeGh(open_issues=[bot_issue(4210, "alpha")]), live=False)
        self.assertEqual(gh.writes(), [])
        self.assertIn("coverage gaps (dry run): would file 2, would update 0, 0 unchanged, would mention 0 expired, would close 1", out)
        self.assertIn("file: beta [presubmit-gate, domain:beta] assign @carol; mention @dave; uncovered 9 days (since 2026-10-17)", out)
        self.assertIn("beta-good: 21/21 over 7 nights (100%); bar: meets 90%; store: not read this tick", out)
        self.assertIn('--dry-run: would POST repos/gke-labs/kube-agents/issues', err)

    def test_dry_run_is_the_default(self):
        self.assertTrue(coverage_gaps.parse_args(["--data", "x"]).dry_run)


class DigestTest(Harness):
    def test_one_line_per_uncovered_domain_with_owner_days_and_best_candidate(self):
        lines = coverage_gaps.digest_lines(self.gaps())
        self.assertEqual(lines, [
            "🚧 Coverage gap: beta uncovered 9 days (owner: carol); best candidate beta-good, 21/21 on the nightly (100%, meets 90%).",
            "🚧 Coverage gap: gamma uncovered 5 days (owner: grace); best candidate gamma-held, 18/21 on the nightly (85%, below 90%).",
        ])

    def test_no_line_once_every_domain_is_covered(self):
        self.roster(["alpha-seat", "beta-good", "gamma-held"])
        self.assertEqual(coverage_gaps.digest_lines(self.gaps()), [])

    def test_a_domain_with_no_nightly_case_says_so(self):
        doc = data_doc()
        doc["cases"] = [c for c in doc["cases"] if c["name"] != "gamma-held"]
        gamma = self.gaps(doc)[-1]
        self.assertIn("gamma uncovered 5 days (owner: grace); no candidate on the nightly record.", coverage_gaps.digest_lines([gamma])[0])
        self.assertIn(coverage_gaps.NO_CANDIDATES, coverage_gaps.render_issue(gamma)[1])

    def test_post_healths_lines_read_this_checkout_and_a_failure_is_no_lines(self):
        now = datetime.datetime.fromisoformat(NOW)
        with unittest.mock.patch.object(coverage_gaps, "REPO_ROOT", self.root):
            self.assertEqual(len(post_health.coverage_lines(data_doc(), now)), 2)
            self.roster(["alpha-seat", "beta-good", "gamma-held"])
            self.assertEqual(post_health.coverage_lines(data_doc(), now), [])
        err = io.StringIO()
        with unittest.mock.patch.object(coverage_gaps, "find_gaps", side_effect=RuntimeError("boom")), contextlib.redirect_stderr(err):
            self.assertEqual(post_health.coverage_lines(data_doc(), now), [])
        self.assertIn("coverage-gap lines skipped", err.getvalue())

    def test_the_lines_carry_the_store_verdict_when_it_was_read(self):
        records = {"beta-good": {"state": "would-admit", "runs": 21, "passes": 21}}
        with unittest.mock.patch.object(coverage_gaps, "store_records", return_value=records):
            line = coverage_gaps.digest_lines(self.gaps(store={}))[0]
        self.assertTrue(line.endswith("(100%, meets 90%); store would-admit, 21/21."), line)


class WorkflowTest(unittest.TestCase):
    """ci-health.yml: armed by the nightly bug filer's switch, best-effort,
    bounded, and before the post."""

    def steps(self):
        return yaml.safe_load(WORKFLOW.read_text())["jobs"]["refresh-and-adjudicate"]["steps"]

    def argv(self, live, mute, store):
        [step] = [s for s in self.steps() if s.get("name") == STEP_NAME]
        with tempfile.TemporaryDirectory() as tmp:
            fake = pathlib.Path(tmp) / "bin" / "python3"
            fake.parent.mkdir()
            fake.write_text('#!/bin/sh\necho "$@"\n')
            fake.chmod(0o755)
            if store:
                (pathlib.Path(tmp) / "work").mkdir()
                (pathlib.Path(tmp) / "work" / "store.json").write_text("{}")
            env = {"PATH": f"{fake.parent}{os.pathsep}{os.environ['PATH']}", "NIGHTLY_BUGS_LIVE": live, "CI_HEALTH_MUTE": mute}
            proc = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, cwd=tmp, capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout.split()

    def test_only_an_armed_unmuted_switch_goes_live(self):
        self.assertEqual(self.argv("true", "false", False)[-1], "--live")
        self.assertEqual(self.argv("false", "false", False)[-1], "--dry-run")
        self.assertEqual(self.argv("true", "true", False)[-1], "--dry-run", "the mute switch disarms it")

    def test_the_store_is_passed_only_when_the_tick_read_one(self):
        self.assertNotIn("--store", self.argv("false", "false", False))
        self.assertIn("work/store.json", self.argv("false", "false", True))

    def test_best_effort_bounded_and_before_the_post(self):
        steps = self.steps()
        names = [s.get("name") for s in steps]
        [step] = [s for s in steps if s.get("name") == STEP_NAME]
        self.assertTrue(step.get("continue-on-error"))
        self.assertNotIn("if", step, "runs every tick, dry when unarmed")
        self.assertLessEqual(step.get("timeout-minutes"), 3)
        self.assertLess(names.index(STEP_NAME), names.index(POST_STEP_NAME))
        self.assertEqual(step["env"], {"GH_TOKEN": "${{ github.token }}"}, "the workflow's own token, nothing new")
        text = textwrap.dedent(WORKFLOW.read_text())
        self.assertNotIn("COVERAGE_GAPS_LIVE", text, "no switch of its own")


if __name__ == "__main__":
    unittest.main()
