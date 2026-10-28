"""nightly_digest.py: the 9 AM digest's nightly section -- new bugs, still
red, first-time reds, recoveries and infra from last night, capped lists,
the one-line quiet day, a truncated night labelled and not counted, Chat
mentions from CI_HEALTH_CHAT_MENTIONS (plain text for a login it lacks), and
a skip with its reason whenever the section cannot be built truthfully.

The nights are synthetic, built like test_eval_dashboard_nightly_bugs.py's;
the repository facts (task.yaml owners, expected_fail, domains.yaml) come
from testdata_nightly_bugs/, copied into a temporary checkout. Chat user ids
here are fake.
"""

import contextlib
import io
import json
import pathlib
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from eval_dashboard import nightly, nightly_digest, post_health

TESTDATA = pathlib.Path(__file__).resolve().parent / "eval_dashboard" / "testdata_nightly_bugs"
REPO = "gke-labs/kube-agents"
JOB = "ci-kube-agents-eval-nightly"
LOGS = "https://oss.gprow.dev/view/gs/kube-agents-prow/logs"
PAGE = f"<{post_health.NIGHTLY_URL}|Nightly page>"
FAIL_REASON = "VerificationCorrectness=0.0 (floor 1.0) -- names-the-pool: required phrases absent"
STORM_REASON = "agent run failed: http 429 RESOURCE_EXHAUSTED"
CASES = ["case-red", "case-red-extended", "case-maint", "case-outsider", "case-reviewer", "case-noowner", "case-xfail", "case-xfail-cap", "case-green"]
FILLER = ("filler-1", "filler-2", "filler-3", "filler-4")
# The digest goes out Tue 2026-10-20 9:05 AM ET; last night started 00:00Z.
NOW = datetime(2026, 10, 20, 13, 5, tzinfo=timezone.utc)
# Fake Chat user ids.
MENTIONS = {"alice": "1001", "dave": "1002"}


def task(name, *results):
    reps = [{"n": i + 1, "result": r, "reason": None if r == "pass" else (STORM_REASON if r == "infra" else FAIL_REASON)} for i, r in enumerate(results)]
    return {"name": name, "result": "pass" if all(r == "pass" for r in results) else "fail", "reps": reps}


def green(name):
    return task(name, "pass", "pass", "pass")


def red(name):
    return task(name, "fail", "fail", "fail")


def infra(name):
    return task(name, "infra", "infra", "infra")


def storm_partial(name):
    """One rep passed, two recorded as failures with a quota reason."""
    t = task(name, "pass", "fail", "fail")
    for rep in t["reps"][1:]:
        rep["reason"] = STORM_REASON
    return t


def night(day, tasks, truncated=False):
    """One main-part build started 00:00:30Z on 2026-10-<day>, with passing
    filler cases so a red or two is not a run-level event."""
    tasks = list(tasks) + [green(name) for name in FILLER]
    return {"build_id": f"30000000000000{day:02d}0001", "tier": "nightly", "job": JOB, "pr": None,
            "started": f"2026-10-{day:02d}T00:00:30+00:00", "finished": f"2026-10-{day:02d}T00:59:00+00:00",
            "result": "FAILURE" if truncated else "SUCCESS", "eval_verdict": None if truncated else "GREEN",
            "duration_s": 35520, "tasks": tasks}


def data(*runs, extra_cases=()):
    cases = [{"name": name, "domain": "capacity", "active": False, "nightly_active": True} for name in CASES + list(FILLER)]
    cases += [{"name": name, "domain": domain, "active": False, "nightly_active": True} for name, domain in extra_cases]
    return {"schema_version": 1, "generated_at": NOW.isoformat(), "cases": cases, "runs": list(runs)}


def issue(number, case, created, assignee=None, url=None):
    """An open bot-filed `nightly-red` issue for ``case``."""
    return {"number": number, "title": f"Nightly: {case} fails every run, 3 nights in a row",
            "body": f"<!-- kube-agents-bot:nightly-red case={case} -->\n", "created_at": created,
            "html_url": url or f"https://github.com/{REPO}/issues/{number}",
            "assignees": [{"login": assignee}] if assignee else []}


def graphql_answer(open_issues, closed_issues, closed):
    """The filer's GraphQL cross-check (nightly_bugs.LISTING_QUERY) as GitHub
    answers it for the issues held."""
    def conn(held):
        return {"totalCount": len(held), "nodes": [{"number": i["number"]} for i in held]}
    repo = {"open": conn(open_issues)}
    if closed:
        repo["closed"] = conn(closed_issues)
    return {"data": {"repository": repo}}


class StubGh:
    """ghcli.Gh's read surface: the open issues, or None for a failed read;
    ``empty_open``: the REST listing answers [] while GraphQL still sees the
    issues (2026-10-22)."""

    def __init__(self, open_issues=(), fail=False, closed_issues=(), empty_open=False):
        self.repo = REPO
        self.open_issues = list(open_issues)
        self.closed_issues = list(closed_issues)
        self.fail = fail
        self.empty_open = empty_open
        self.calls = []

    def path(self, suffix):
        return f"repos/{self.repo}/{suffix}"

    def call(self, method, path, body=None, paginate=False, read=False):
        self.calls.append((method, path))
        if self.fail:
            return None
        if method == "POST" and path == "graphql":
            return graphql_answer(self.open_issues, self.closed_issues, body["variables"]["closed"])
        if "state=closed" in path:
            return list(self.closed_issues)
        return [] if self.empty_open else list(self.open_issues)


class Harness(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name) / "repo"
        shutil.copytree(TESTDATA, self.root)
        sleep = mock.patch.object(nightly_digest.nightly_bugs.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def build(self, doc, open_issues=(), mentions=None, gh=None):
        with contextlib.redirect_stderr(io.StringIO()):
            return nightly_digest.build(doc, NOW, gh or StubGh(open_issues), self.root, MENTIONS if mentions is None else mentions)


def full_night():
    """Six nights, each case under test in one of the section's lists."""
    nights = []
    for day in range(15, 21):
        tasks = [green("case-green"), red("case-xfail"), red("case-red-extended")]
        tasks.append(red("case-red") if day >= 18 else green("case-red"))
        tasks.append(red("case-maint") if day >= 18 else green("case-maint"))
        tasks.append(red("case-noowner") if day == 20 else green("case-noowner"))
        tasks.append(red("case-reviewer") if day in (17, 18, 19) else green("case-reviewer"))
        tasks.append(infra("case-outsider") if day >= 18 else green("case-outsider"))
        nights.append(night(day, tasks))
    return data(*nights)


FULL_ISSUES = [
    # Filed this morning, after last night's run finished: new.
    issue(4001, "case-red", "2026-10-20T02:00:00Z", assignee="alice"),
    # Filed days ago: still red, tracked.
    issue(4002, "case-red-extended", "2026-10-17T05:00:00Z"),
    # Recovered last night.
    issue(4003, "case-reviewer", "2026-10-19T05:00:00Z", assignee="bob"),
]


class FullSectionTest(Harness):
    def test_a_full_section(self):
        section = self.build(full_night(), FULL_ISSUES)
        self.assertFalse(section.quiet)
        self.assertEqual("\n".join(section.lines), "\n".join([
            (f"🌙 *Nightly, Tue Oct 20* · 6 of 12 scenarios passed · 2 not graded (infrastructure or no line) · ran 9h 52m"
             f" · <{LOGS}/{JOB}/30000000000000200001|run> · {PAGE}"),
            "",
            "🔴 *New bugs filed* (failing every run, 3 nights in a row)",
            f"• Capacity: case-red → <https://github.com/{REPO}/issues/4001|#4001> · <users/1001> (backup <users/1002>)",
            "",
            "🔴 *Owed a bug, not filed yet* (failing every run, 3 or more nights in a row, no open issue)",
            "• Capacity: case-maint, night 3 · @carol (backup <users/1002>)",
            "",
            "🟠 *Still red, already tracked*",
            f"• Capacity: case-red-extended, night 6 · <https://github.com/{REPO}/issues/4002|#4002> · <users/1001>",
            "",
            "🆕 *Red last night, no bug yet* (one is filed after 3 red nights in a row)",
            "• Security: case-noowner (0/3), night 1 · @sam",
            "",
            "✅ *Recovered*",
            f"• Security: case-reviewer, 3/3 after 3 red nights · <https://github.com/{REPO}/issues/4003|#4003>",
            "",
            "⚙️ *Infra*",
            "• Security: case-outsider kept from running, night 3",
            "",
            'Owners: reply in this thread with "on it", "test bug" or "fixed in #NNNN".',
        ]))

    def test_the_digest_carries_the_section_in_place_of_the_one_line(self):
        doc = full_night()
        tmp = pathlib.Path(self.root).parent
        (tmp / "data.json").write_text(json.dumps(doc))
        (tmp / "health.json").write_text(json.dumps({"state": "GREEN", "condition": None, "metrics": {}, "failing_cases": []}))
        sent = []

        class Opener:
            def __call__(self, request, timeout=None):
                sent.append(json.loads(request.data)["text"])
                return contextlib.nullcontext(type("R", (), {"status": 200})())

        def gh_runner(argv, input=None, **kwargs):
            if argv[argv.index("-X") + 1] == "GET":
                out = "" if "state=closed" in argv[argv.index("-X") + 2] else "\n".join(json.dumps(i) for i in FULL_ISSUES)
            else:
                out = json.dumps(graphql_answer(FULL_ISSUES, [], json.loads(input)["variables"]["closed"]))
            return type("P", (), {"returncode": 0, "stdout": out, "stderr": ""})()

        environ = {post_health.SPACE_ENV: "spaces/AAAAtest", post_health.TOKEN_ENV: "fake-token", post_health.ghcli.TOKEN_ENV: "fake-gh",
                   nightly_digest.MENTIONS_ENV: json.dumps(MENTIONS)}
        argv = ["--health", str(tmp / "health.json"), "--state", str(tmp / "state.json"), "--data", str(tmp / "data.json"), "--now", NOW.isoformat()]
        with mock.patch.object(nightly_digest.nightly_bugs, "REPO_ROOT", self.root), contextlib.redirect_stderr(io.StringIO()):
            rc = post_health.main(argv, environ=environ, opener=Opener(), gh_runner=gh_runner)
        self.assertEqual(rc, 0)
        [digest] = sent
        self.assertIn("\n🌙 *Nightly, Tue Oct 20* · 6 of 12 scenarios passed", digest)
        self.assertIn("<users/1001> (backup <users/1002>)", digest)
        self.assertNotIn("🌙 Nightly:", digest, "the one-line summary gives way to the section")
        self.assertNotIn("skipped", digest)


class QuietDayTest(Harness):
    def test_a_quiet_day_is_one_line(self):
        doc = data(*(night(day, [green("case-green"), green("case-red"), red("case-red-extended")]) for day in range(15, 21)))
        section = self.build(doc, [issue(4002, "case-red-extended", "2026-10-17T05:00:00Z")])
        self.assertTrue(section.quiet)
        self.assertEqual(section.lines, [f"🌙 Nightly Oct 20: 6 of 7 passed (6 not graded), no new reds, nothing recovered, 1 still red · {PAGE}"])


    def test_a_second_red_night_is_counted_on_a_quiet_day(self):
        doc = data(*(night(day, [green("case-green"), red("case-red") if day >= 19 else green("case-red")]) for day in range(15, 21)))
        section = self.build(doc)
        self.assertTrue(section.quiet)
        self.assertEqual(section.lines, [f"🌙 Nightly Oct 20: 5 of 6 passed (7 not graded), no new reds, nothing recovered, 1 still red · {PAGE}"])

    def test_a_tracked_case_red_again_after_a_green_night_is_never_quiet(self):
        # case-red-extended has an open issue from an earlier streak, passed
        # on the 19th and failed every rep last night: a regression, not a
        # case "still red".
        doc = data(*(night(day, [green("case-green"), red("case-red-extended") if day != 19 else green("case-red-extended")]) for day in range(15, 21)))
        section = self.build(doc, [issue(4002, "case-red-extended", "2026-10-17T05:00:00Z")])
        self.assertFalse(section.quiet)
        self.assertIn(f"• Capacity: case-red-extended, night 1 · <https://github.com/{REPO}/issues/4002|#4002> · <users/1001>", section.lines)

    def test_a_writers_part_cut_off_is_never_quiet(self):
        nights = [night(day, [green("case-green")]) for day in range(15, 21)]
        writers = night(20, [green("case-red")], truncated=True)
        writers.update(job="ci-kube-agents-eval-nightly-writers", build_id="3000000000000020002")
        section = self.build(data(*nights, writers))
        self.assertFalse(section.quiet)
        self.assertIn("writers part cut off at the deadline, its reds not listed below", section.lines[0])

    def test_a_writers_part_missing_or_still_running_is_named_and_never_quiet(self):
        nights = [night(day, [green("case-green")]) for day in range(15, 21)]
        writers = []
        for day in range(15, 20):
            w = night(day, [green("case-red")])
            w.update(job="ci-kube-agents-eval-nightly-writers", build_id=f"30000000000000{day}0002")
            writers.append(w)
        section = self.build(data(*nights, *writers))
        self.assertFalse(section.quiet)
        self.assertIn("writers part missing", section.lines[0])
        running = data(*nights, *writers)
        running["pending_builds"] = [{"build_id": "3000000000000020002", "tier": "nightly", "job": "ci-kube-agents-eval-nightly-writers",
                                      "log_url": f"{LOGS}/ci-kube-agents-eval-nightly-writers/3000000000000020002", "first_seen": "2026-10-20T06:00:00+00:00"}]
        section = self.build(running)
        self.assertFalse(section.quiet)
        self.assertIn("writers part still running", section.lines[0])


class NightlyPageAgreementTest(Harness):
    def test_the_header_counts_are_the_nightly_pages(self):
        # case-maint passed one rep and failed two on a 429: the filer's
        # classifier reads those two as infrastructure and grades the case
        # a pass, the Nightly page counts it partial (the 2026-10-20 night's
        # 83 against 82). The header follows the page.
        nights = [night(day, [green("case-green"), red("case-red-extended")]) for day in range(15, 20)]
        nights.append(night(20, [green("case-green"), red("case-red-extended"), storm_partial("case-maint"), infra("case-outsider"), red("case-noowner")]))
        for doc in (data(*nights), full_night()):
            [page] = nightly.nightly_document(doc)["nights"][:1]
            counts = page["counts"]
            section = self.build(doc, [issue(4002, "case-red-extended", "2026-10-17T05:00:00Z")])
            ungraded = counts["infra"] + counts["missing"]
            self.assertFalse(section.quiet)
            self.assertIn(f"{counts['passed']} of {counts['recorded']} scenarios passed · {ungraded} not graded", section.lines[0])
        self.assertIn("5 of 9 scenarios passed", self.build(data(*nights), [issue(4002, "case-red-extended", "2026-10-17T05:00:00Z")]).lines[0])


class TrackingTest(Harness):
    def test_a_handed_off_closed_issue_still_tracks_and_a_young_issue_is_not_new(self):
        nights = [night(day, [green("case-green"), red("case-red") if day >= 17 else green("case-red"),
                              red("case-maint") if day >= 19 else green("case-maint"),
                              red("case-noowner") if day == 20 else green("case-noowner")]) for day in range(15, 21)]
        handed_off = {**issue(4005, "case-red", "2026-10-17T05:00:00Z"), "state": "closed", "state_reason": "not_planned", "closed_at": "2026-10-18T05:00:00Z"}
        # A human's issue opened this morning for a case red two nights:
        # tracked, but not a new 3-night bug.
        human = {"number": 4006, "title": "case-maint keeps failing", "body": "see case-maint", "created_at": "2026-10-20T03:00:00Z",
                 "html_url": f"https://github.com/{REPO}/issues/4006", "assignees": []}
        section = self.build(data(*nights), [human], gh=StubGh([human], closed_issues=[handed_off]))
        text = "\n".join(section.lines)
        self.assertNotIn("Owed a bug", text)
        self.assertNotIn("New bugs filed", text)
        self.assertIn(f"• Capacity: case-red, night 4 · <https://github.com/{REPO}/issues/4005|#4005> (closed) · <users/1001>", text)
        self.assertIn(f"• Capacity: case-maint, night 2 · <https://github.com/{REPO}/issues/4006|#4006> · @carol", text)

    def test_a_bug_fixed_while_last_nights_run_ran_tracks_until_three_nights_start_after(self):
        # #4041: the fix closed case-red's bug while the 20th's run, started
        # before it, was going. The filer does not file again, so the case
        # is not owed a bug (a loud line): it is still red, tracked.
        nights = [night(day, [green("case-green"), red("case-red") if day >= 18 else green("case-red")]) for day in range(15, 21)]
        fixed = {**issue(4005, "case-red", "2026-10-18T05:00:00Z"), "state": "closed", "state_reason": "completed", "closed_at": "2026-10-20T00:30:00Z"}
        section = self.build(data(*nights), [], gh=StubGh([], closed_issues=[fixed]))
        self.assertTrue(section.quiet, section.lines)
        self.assertIn("1 still red", section.lines[0])
        # Fixed, then green, then red again: a new failure the closed bug
        # does not track (the filer reads closes against a streak only).
        nights = [night(day, [green("case-green"), red("case-red") if day in (18, 20) else green("case-red")]) for day in range(15, 21)]
        fixed = dict(fixed, closed_at="2026-10-18T00:30:00Z")
        text = "\n".join(self.build(data(*nights), [], gh=StubGh([], closed_issues=[fixed])).lines)
        self.assertIn("Red last night, no bug yet", text)
        self.assertNotIn("#4005", text)


    def test_a_bug_filed_late_for_a_case_not_graded_last_night_is_new(self):
        # case-red was red three nights, then kept from running last night;
        # the filer (just armed, or after an outage) files it this morning.
        nights = [night(day, [green("case-green"), red("case-red") if day in (17, 18, 19) else green("case-red"),
                              red("case-maint") if day in (17, 18, 19) else green("case-maint")]) for day in range(15, 20)]
        nights.append(night(20, [green("case-green"), infra("case-red"), infra("case-maint")]))
        filed = issue(4001, "case-red", "2026-10-20T02:00:00Z", assignee="alice")
        # One filed before last night ended was news yesterday, not today.
        older = issue(4007, "case-maint", "2026-10-19T05:00:00Z")
        section = self.build(data(*nights), [filed, older])
        self.assertFalse(section.quiet)
        text = "\n".join(section.lines)
        self.assertIn(f"🔴 *New bugs filed* (failing every run, 3 nights in a row)\n• Capacity: case-red → <https://github.com/{REPO}/issues/4001|#4001> · <users/1001>", text)
        self.assertNotIn("#4007", text)


class RunEventTest(Harness):
    def test_a_clean_same_date_rerun_after_a_run_level_event_is_read_in_full(self):
        nights = [night(day, [green("case-green"), green("case-noowner")]) for day in range(15, 20)]
        # The cron night: nearly every case failed (a broken image).
        event = night(20, [red("case-green"), red("case-noowner")] + [red(f"broken-{n}") for n in range(20)])
        # A maintainer re-ran the main job clean the same morning; the page
        # and the filer read the re-run.
        rerun = night(20, [green("case-green"), red("case-noowner")])
        rerun.update(build_id="3000000000000020009", started="2026-10-20T03:00:30+00:00", finished="2026-10-20T03:59:00+00:00")
        section = self.build(data(*nights, event, rerun))
        text = "\n".join(section.lines)
        self.assertNotIn(nightly_digest.RUN_EVENT_BODY, text)
        self.assertIn("• Security: case-noowner (0/3), night 1 · @sam", text)


class InfraTest(Harness):
    def test_a_case_with_no_readable_task_yaml_is_not_listed_kept(self):
        # case-gone is on the matrix but its task.yaml is gone from this
        # checkout: the filer files nothing for it, so the digest lists
        # nothing for it.
        nights = [night(day, [green("case-green"), green("case-gone") if day < 18 else infra("case-gone")]) for day in range(15, 21)]
        section = self.build(data(*nights, extra_cases=[("case-gone", "security")]))
        self.assertNotIn("case-gone", "\n".join(section.lines))


class TruncatedNightTest(Harness):
    def test_a_truncated_night_is_labelled_and_not_counted(self):
        nights = [night(day, [green("case-green"), red("case-red") if day >= 18 else green("case-red")]) for day in range(15, 20)]
        # Last night was cut off at the deadline with case-red's third red
        # and case-noowner's first among what it graded: neither counts.
        nights.append(night(20, [green("case-green"), red("case-red"), red("case-noowner")], truncated=True))
        section = self.build(data(*nights))
        self.assertEqual(section.lines, [
            (f"🌙 *Nightly, Tue Oct 20* · cut off at the job deadline: truncated, not counted · ran 9h 52m"
             f" · <{LOGS}/{JOB}/30000000000000200001|run> · {PAGE}"),
            "⚙️ *Infra*",
            f"• Main part lost: ended before its verdict after 9h 52m (result FAILURE); 7 cases recorded · <{LOGS}/{JOB}/30000000000000200001|run>",
        ])
        text = "\n".join(section.lines)
        for word in ("passed", "case-red", "case-noowner"):
            self.assertNotIn(word, text)


class MentionsTest(Harness):
    def test_a_login_missing_from_the_map_is_plain_text(self):
        section = self.build(full_night(), FULL_ISSUES, mentions={})
        text = "\n".join(section.lines)
        self.assertNotIn("<users/", text)
        self.assertIn("· @alice (backup @dave)", text)
        self.assertIn("· @carol (backup @dave)", text)

    def test_the_map_is_read_leniently_and_never_echoed(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(nightly_digest.load_mentions(None), {})
            self.assertEqual(nightly_digest.load_mentions(""), {})
            self.assertEqual(nightly_digest.load_mentions("{not json"), {})
            self.assertEqual(nightly_digest.load_mentions('["alice"]'), {})
            self.assertEqual(
                nightly_digest.load_mentions('{"Alice": "users/1001", "dave": 1002, "eve": "eve@example.com"}'),
                {"alice": "1001", "dave": "1002"},
            )
        self.assertIn("1 entry is not a numeric Chat user id", err.getvalue())
        self.assertNotIn("1001", err.getvalue())
        # A map written the other way round: its keys are the ids.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(nightly_digest.load_mentions('{"123456789012345678901": "alice", "users/1002": "dave"}'), {})
        self.assertIn("2 entries are not a numeric Chat user id", err.getvalue())
        self.assertNotIn("123456789012345678901", err.getvalue())
        self.assertNotIn("1002", err.getvalue())
        section = self.build(full_night(), FULL_ISSUES, mentions={"alice": "1001"})
        self.assertIn("<users/1001>", "\n".join(section.lines), "login matching ignores letter case")


class CappingTest(Harness):
    def test_each_list_is_capped_at_five_lines(self):
        names = [f"cap-{n}" for n in range(1, 8)]
        for name in names:
            (self.root / "bench" / "tasks" / name).mkdir()
            (self.root / "bench" / "tasks" / name / "task.yaml").write_text(f"id: {name}\ndomain: security\n")
        nights = [night(day, [green(n) for n in names]) for day in range(15, 20)]
        nights.append(night(20, [red(n) for n in names]))
        doc = data(*nights, extra_cases=[(n, "security") for n in names])
        # Seven reds against four filler passes would be a run-level event;
        # more filler keeps it a night of ordinary reds.
        for run in doc["runs"]:
            run["tasks"] += [green(f"filler-extra-{n}") for n in range(10)]
        section = self.build(doc)
        first = section.lines[section.lines.index("🆕 *Red last night, no bug yet* (one is filed after 3 red nights in a row)"):]
        self.assertEqual(first[1:7], [
            "• Security: cap-1 (0/3), night 1 · @sam",
            "• Security: cap-2 (0/3), night 1 · @sam",
            "• Security: cap-3 (0/3), night 1 · @sam",
            "• Security: cap-4 (0/3), night 1 · @sam",
            "• Security: cap-5 (0/3), night 1 · @sam",
            f"• …and 2 more: {PAGE}",
        ])
        self.assertEqual(first[7], "")


class SkipTest(Harness):
    def test_the_section_skips_rather_than_guess(self):
        doc = full_night()
        with self.assertRaisesRegex(nightly_digest.Skipped, "no GitHub token"):
            nightly_digest.build(doc, NOW, None, self.root)
        with self.assertRaisesRegex(nightly_digest.Skipped, "could not be read"):
            self.build(doc, gh=StubGh(fail=True))
        with self.assertRaisesRegex(nightly_digest.Skipped, "no nightly run started in the last 36 hours"):
            self.build(data(night(10, [green("case-green")])))
        # Last night's build is in flight with no part recorded yet: the
        # one-line summary above says "still running", and so does the reason.
        late = data(night(19, [green("case-green")]))
        late["pending_builds"] = [{"build_id": "3000000000000020001", "tier": "nightly", "job": JOB, "first_seen": "2026-10-20T05:00:00+00:00"}]
        with self.assertRaisesRegex(nightly_digest.Skipped, "^last night's run is still running$"):
            self.build(late)
        writers_only = night(20, [green("case-green")])
        writers_only.update(job="ci-kube-agents-eval-nightly-writers", build_id="3000000000000020002")
        with self.assertRaisesRegex(nightly_digest.Skipped, "main part is missing"):
            self.build(data(night(19, [green("case-green")]), writers_only))
        # The main part ran as two jobs on the 19th; on the 20th main-b never
        # landed, so the section would cover half the matrix.
        half = night(19, [green("case-b")])
        half.update(job="ci-kube-agents-eval-nightly-main-b", build_id="3000000000000019003")
        with self.assertRaisesRegex(nightly_digest.Skipped, "^last night's main-b part is missing"):
            self.build(data(night(19, [green("case-green")]), half, night(20, [green("case-green")]), extra_cases=(("case-b", "capacity"),)))
        # main-b still in flight at 9 AM: held back, not built on half the matrix.
        running = data(night(19, [green("case-green")]), half, night(20, [green("case-green")]), extra_cases=(("case-b", "capacity"),))
        running["pending_builds"] = [{"build_id": "3000000000000020003", "tier": "nightly", "job": "ci-kube-agents-eval-nightly-main-b",
                                      "log_url": f"{LOGS}/ci-kube-agents-eval-nightly-main-b/3000000000000020003", "first_seen": "2026-10-20T05:00:00+00:00"}]
        with self.assertRaisesRegex(nightly_digest.Skipped, "^last night's main-b part is still running$"):
            self.build(running)
        with mock.patch.object(nightly_digest, "yaml", None), self.assertRaisesRegex(nightly_digest.Skipped, "pyyaml"):
            self.build(doc)
        # Any other failure is a skip line, never a lost digest.
        with mock.patch.object(nightly_digest, "build", side_effect=KeyError("counts")), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(post_health.nightly_section(doc, NOW, StubGh()), "building it failed (KeyError); see the job log")
        bad = [issue(4001, "case-red", "2026-10-20T02:00:00Z", url="https://github.com/elsewhere/repo/issues/4001")]
        with self.assertRaisesRegex(nightly_digest.Skipped, "malformed"):
            self.build(doc, bad)

    def test_an_issue_listing_the_filer_would_not_trust_is_not_read(self):
        # The REST listing answers [] while the issues exist and GraphQL
        # sees them: the filer refuses that listing, and so does the section,
        # rather than call every tracked case unfiled.
        gh = StubGh(FULL_ISSUES, empty_open=True)
        with self.assertRaisesRegex(nightly_digest.Skipped, r"^GitHub's nightly-red issue list could not be trusted \(the open listing holds 0 issues, GraphQL 3 \(missing #4001, #4002, #4003\)\)$"):
            self.build(full_night(), gh=gh)
        self.assertEqual(sum(1 for m, p in gh.calls if m == "GET" and "state=open" in p), nightly_digest.nightly_bugs.LISTING_ATTEMPTS)

    def test_a_skip_keeps_the_one_line_and_says_why(self):
        reason = "the nightly-red issues could not be read from GitHub"
        doc = full_night()
        text = post_health.render_digest({"metrics": {}}, NOW, doc, reason)
        self.assertIn("\n🌙 Nightly: ", text)
        self.assertIn(f"\n{post_health.NIGHTLY_URL}\n🌙 Nightly section skipped: {reason}.\n", text)


if __name__ == "__main__":
    unittest.main()
