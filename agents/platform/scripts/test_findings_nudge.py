import http.client
import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.absolute()))

import findings_nudge as nudge
import findings_queue as fq

UTC = timezone.utc


def at(day: int, hour: int, minute: int = 0) -> datetime:
    """A moment in October 2026, UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def finding(**overrides) -> dict:
    row = {
        "id": "fnd_0001",
        "check_slug": "probes-readiness",
        "severity": "critical",
        "rank_score": 288,
        "rubric": {"B": 8, "L": 6, "detect": 3, "recover": 3, "C": 1.0},
        "project": "",
        "cluster": "prod",
        "namespace": "payments",
        "object": "api",
        "title": "no readinessProbe on api",
        "recommendation": {"action": "add a readinessProbe", "rationale": "traffic", "risk": "5xx"},
        "state": "queued",
        "first_shown_at": None,
        "provider_managed": False,
        "actionable": True,
    }
    row.update(overrides)
    return row


def shown(when: datetime, **overrides) -> dict:
    """A row the nudge already added, still waiting for a decision."""
    return finding(state="surfaced", first_shown_at=when.strftime("%Y-%m-%d %H:%M:%S"), **overrides)


def plan_for(rows, now=at(6, 12), added=None):
    return fq.pace(rows, now, fq.PacingLimits(), added or {"critical": 0, "noncritical": 0})


class ComposeTests(unittest.TestCase):
    def test_nothing_to_say_is_an_empty_string(self):
        self.assertEqual(nudge.compose(plan_for([]), 0, daily=True), "")
        # A critical waiting for tomorrow's budget is not something to say.
        self.assertEqual(nudge.compose(plan_for([finding()], added={"critical": 2}), 1, daily=False), "")

    def test_a_message_opens_with_the_heading_and_ends_with_the_count(self):
        message = nudge.compose(plan_for([finding(id="a"), finding(id="b", check_slug="x", severity="minor")]), 2, False)
        self.assertTrue(message.startswith(f"{nudge.HEADING}\n\n"))
        self.assertTrue(message.endswith("2 findings are open on the queue. Ask for the full list."))

    def test_new_criticals_are_numbered_with_score_location_action_and_id(self):
        message = nudge.compose(plan_for([finding(id="a", title="first"), finding(id="b", check_slug="x", title="second")]), 2, False)
        self.assertIn("New: 2 critical findings.", message)
        self.assertIn("1. [288] first\n   prod/payments/api\n   add a readinessProbe\n   id: a", message)
        self.assertIn("2. [288] second", message)

    def test_one_critical_reads_as_singular(self):
        self.assertIn("New: 1 critical finding.", nudge.compose(plan_for([finding()]), 1, False))

    def test_new_noncriticals_say_so(self):
        message = nudge.compose(plan_for([finding(severity="major")], now=at(6, 16)), 1, False)
        self.assertIn("New: 1 finding, none of them critical.", message)

    def test_a_gathered_line_lists_its_other_objects_and_counts_its_ids(self):
        rows = [finding(id=f"p{i}", object=f"d{i}") for i in range(8)]
        message = nudge.compose(plan_for(rows), 8, False)
        self.assertIn("New: 1 critical finding.", message)
        self.assertIn("also payments/d1, payments/d2, payments/d3, payments/d4, payments/d5 and 2 more", message)
        self.assertIn("id: p0 (a decision on it covers all 8 objects in this item)", message)

    def test_the_covered_count_is_every_row_a_decision_reaches(self):
        # Added with 16 members; a later sweep stopped reporting 13 of them.
        # They are no longer pending, but a decision on p0 still decides them.
        rows = [shown(at(5, 16), id=f"p{i}", object=f"d{i}", severity="major") for i in range(3)]
        rows += [
            shown(at(5, 16), id=f"p{i}", object=f"d{i}", severity="major", absent_since="2026-10-05 20:00:00")
            for i in range(3, 16)
        ]
        message = nudge.compose(plan_for(rows), 1, daily=True)
        self.assertIn("also payments/d1, payments/d2\n", message)
        self.assertIn("id: p0 (a decision on it covers all 16 objects in this item, 13 of them not listed because "
                      "the last complete sweep did not report them)", message)

    def test_a_lone_row_names_no_count(self):
        message = nudge.compose(plan_for([finding(id="solo")]), 1, False)
        self.assertIn("id: solo\n", message)
        self.assertNotIn("covers", message)

    def test_reminders_appear_only_in_the_daily_part(self):
        plan = plan_for([shown(at(5, 12), id="old")])
        self.assertEqual(nudge.compose(plan, 1, daily=False), "")
        self.assertIn("Still open: 1 critical finding, undecided.", nudge.compose(plan, 1, daily=True))

    def test_the_stop_add_notice_names_what_blocks_and_how_to_release_it(self):
        plan = plan_for([shown(at(5, 16), id="m0", severity="major", title="no limits"), finding(id="c0", check_slug="x")])
        message = nudge.compose(plan, 2, daily=True)
        self.assertIn("New findings are on hold until this one is dismissed, snoozed or accepted. 1 critical finding is waiting.", message)
        self.assertIn("no limits", message)
        self.assertIn("id: m0", message)
        self.assertIn("dismiss (won't fix), snooze until a date, or accept (being worked on)", message)
        self.assertIn("A decision on an id covers every undecided object in its item.", message)
        self.assertNotIn("New: ", message)

    def test_the_waiting_count_says_how_many_are_critical(self):
        rows = [shown(at(5, 16), id="m0", severity="major"), finding(id="c0", check_slug="x")]
        rows += [finding(id=f"n{i}", check_slug=f"n{i}", severity="minor") for i in range(2)]
        message = nudge.compose(plan_for(rows), 4, daily=True)
        self.assertIn("1 critical finding and 2 other findings are waiting.", message)
        rows = [shown(at(5, 16), id="m0", severity="major"), finding(id="n0", check_slug="n0", severity="minor")]
        self.assertIn("1 finding is waiting.", nudge.compose(plan_for(rows), 2, daily=True))

    def test_a_cluster_scoped_finding_names_its_cluster_once(self):
        message = nudge.compose(plan_for([finding(namespace="", object="prod", title="WI disabled")]), 1, False)
        self.assertIn("\n   prod\n", message)
        self.assertNotIn("prod/prod", message)

    def test_a_finding_with_a_project_leads_with_it(self):
        message = nudge.compose(plan_for([finding(project="acme-prod")]), 1, False)
        self.assertIn("acme-prod/prod/payments/api", message)

    def test_a_finding_with_no_recommended_action_prints_without_a_blank_line(self):
        message = nudge.compose(plan_for([finding(recommendation={})]), 1, False)
        self.assertNotIn("\n   \n", message)

    def test_provider_managed_observations_are_counted_never_named(self):
        rows = [finding(id="obs", check_slug="x", provider_managed=True, actionable=False, title="kube-system thing"), finding(id="c0")]
        message = nudge.compose(plan_for(rows), 2, False)
        self.assertNotIn("kube-system thing", message)
        self.assertIn("1 of them is provider-managed, with no next step you can take", message)


class NudgeHarness(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        self.err = io.StringIO()
        for target, value in ((sys, "stdout"), (sys, "stderr")):
            patcher = unittest.mock.patch.object(target, value, self.out if value == "stdout" else self.err)
            patcher.start()
            self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.gateway = Path(tmp.name)
        self.home = self.gateway / "profiles" / "platform"
        self.home.mkdir(parents=True)
        env = unittest.mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        for name in fq.PACING_ENV.values():
            os.environ.pop(name, None)

    def run_at(self, now, ranked, added=None, fail=None, surfaced_error=None):
        """Drive `main` at `now` against a stubbed queue, recording every request."""
        self.out.seek(0)
        self.out.truncate()
        self.calls, self.marks = [], []

        def fake_request(endpoint, path, body=None, method=""):
            self.calls.append(path)
            if fail and path != "/v1/findings/expire-snoozes":
                raise fail
            if path == "/v1/findings/expire-snoozes":
                return {"expired": 0}
            if path == "/v1/findings/ranked":
                return {"findings": ranked}
            if path.startswith("/v1/findings/additions?day="):
                self.day_asked = path.split("=", 1)[1]
                return {"day": self.day_asked, **(added or {"critical": 0, "noncritical": 0})}
            if path.endswith("/surfaced"):
                if surfaced_error:
                    raise surfaced_error
                self.marks.append((path.split("/")[3], body))
                return {}
            raise AssertionError(f"unexpected request {path}")

        with unittest.mock.patch.object(nudge, "_request", side_effect=fake_request):
            return nudge.main([], clock=lambda: now)

    def state(self) -> dict:
        path = self.home / nudge.STATE_FILE
        return json.loads(path.read_text()) if path.exists() else {}


class MainTests(NudgeHarness):
    def test_noon_adds_the_top_two_criticals_and_marks_them_as_additions(self):
        rows = [finding(id=f"c{i}", check_slug=f"c{i}", rank_score=300 - i) for i in range(6)]
        code = self.run_at(at(6, 12), rows)
        self.assertEqual(code, 0)
        self.assertIn("New: 2 critical findings.", self.out.getvalue())
        self.assertEqual(self.day_asked, "2026-10-06")
        self.assertEqual(
            self.marks,
            [
                ("c0", {"publisher": "nudge", "added_class": "critical", "run": "2026-10-06T12:00:00Z"}),
                ("c1", {"publisher": "nudge", "added_class": "critical", "run": "2026-10-06T12:00:00Z"}),
            ],
        )
        self.assertEqual(self.calls[:3], ["/v1/findings/expire-snoozes", "/v1/findings/ranked", "/v1/findings/additions?day=2026-10-06"])
        self.assertEqual(self.state()[nudge.DAILY_KEY], "2026-10-06")

    def test_an_hour_with_nothing_to_say_prints_nothing(self):
        rows = [shown(at(6, 12), id="c0"), shown(at(6, 12), id="c1", check_slug="x"), finding(id="c2", check_slug="y")]
        self.run_at(at(6, 12), [])
        code = self.run_at(at(6, 13), rows, added={"critical": 2, "noncritical": 0})
        self.assertEqual(code, 0)
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.marks, [])

    def test_a_morning_hour_adds_nothing(self):
        code = self.run_at(at(6, 11), [finding()])
        self.assertEqual(code, 0)
        self.assertEqual(self.out.getvalue(), "")
        self.assertNotIn(nudge.DAILY_KEY, self.state())

    def test_reminders_go_out_once_a_day(self):
        rows = [shown(at(5, 12), id="old")]
        self.run_at(at(6, 12), rows)
        self.assertIn("Still open: 1 critical finding", self.out.getvalue())
        self.assertEqual(self.marks, [("old", {"publisher": "nudge"})])

        self.run_at(at(6, 13), rows)
        self.assertEqual(self.out.getvalue(), "")

        self.run_at(at(7, 12), rows)
        self.assertIn("Still open: 1 critical finding", self.out.getvalue())

    def test_the_daily_part_waits_for_the_first_run_after_noon(self):
        rows = [shown(at(5, 12), id="old")]
        self.run_at(at(6, 9), rows)
        self.assertEqual(self.out.getvalue(), "")
        # The noon run was missed (a restart, say); the next one carries it.
        self.run_at(at(6, 14), rows)
        self.assertIn("Still open", self.out.getvalue())

    def test_stop_add_posts_a_notice_once_a_day_and_adds_nothing(self):
        rows = [shown(at(5, 16), id="m0", severity="major"), finding(id="c0", check_slug="x")]
        self.run_at(at(6, 12), rows)
        message = self.out.getvalue()
        self.assertIn("New findings are on hold until this one is dismissed, snoozed or accepted.", message)
        self.assertIn("id: m0", message)
        self.assertEqual(self.marks, [("m0", {"publisher": "nudge"})])

        for hour in (13, 16, 23):
            self.run_at(at(6, hour), rows)
            self.assertEqual(self.out.getvalue(), "", hour)

        self.run_at(at(7, 12), rows)
        self.assertIn("New findings are on hold", self.out.getvalue())

    def test_noncriticals_are_added_from_the_afternoon_hour(self):
        rows = [finding(id=f"m{i}", check_slug=f"m{i}", severity="major") for i in range(4)]
        self.run_at(at(6, 15), rows)
        self.assertEqual(self.out.getvalue(), "")
        self.run_at(at(6, 16), rows)
        self.assertIn("New: 3 findings, none of them critical.", self.out.getvalue())
        self.assertEqual([body["added_class"] for _, body in self.marks], ["noncritical"] * 3)

    def test_the_hour_is_configurable(self):
        rows = [finding(severity="major")]
        with unittest.mock.patch.dict(os.environ, {"FINDINGS_NONCRITICAL_AFTER_HOUR": "13"}):
            self.run_at(at(6, 13), rows)
        self.assertIn("none of them critical", self.out.getvalue())

    def test_the_daily_limit_is_configurable_and_a_bad_value_is_reported(self):
        rows = [finding(id=f"c{i}", check_slug=f"c{i}") for i in range(3)]
        with unittest.mock.patch.dict(os.environ, {"FINDINGS_DAILY_CRITICALS": "1"}):
            self.run_at(at(6, 12), rows)
        self.assertIn("New: 1 critical finding.", self.out.getvalue())

        with unittest.mock.patch.dict(os.environ, {"FINDINGS_DAILY_CRITICALS": "lots"}):
            self.run_at(at(7, 12), rows)
        self.assertIn("New: 2 critical findings.", self.out.getvalue())
        self.assertIn("FINDINGS_DAILY_CRITICALS='lots'", self.err.getvalue())

    def test_a_member_joining_a_reminded_line_is_marked_shown_with_it(self):
        rows = [shown(at(5, 12), id="p0"), finding(id="p1", object="web")]
        self.run_at(at(6, 12), rows)
        self.assertEqual(self.marks, [("p0", {"publisher": "nudge"}), ("p1", {"publisher": "nudge"})])

    def test_a_failed_surfaced_call_costs_the_bookkeeping_not_the_message(self):
        code = self.run_at(at(6, 12), [finding(id="a")], surfaced_error=urllib.error.URLError("refused"))
        self.assertEqual(code, 0)
        self.assertIn("no readinessProbe on api", self.out.getvalue())
        self.assertIn("could not mark a surfaced", self.err.getvalue())

    def test_an_addition_whose_mark_failed_is_not_announced_again_that_day(self):
        rows = [finding(id="a")]
        refused = urllib.error.URLError("refused")
        self.run_at(at(6, 12), rows, surfaced_error=refused)
        self.assertIn("New: 1 critical finding.", self.out.getvalue())
        for hour in (13, 18):
            self.run_at(at(6, hour), rows, surfaced_error=refused)
            self.assertEqual(self.out.getvalue(), "", hour)
        self.run_at(at(7, 12), rows)
        self.assertIn("New: 1 critical finding.", self.out.getvalue())

    def test_an_announced_critical_whose_mark_failed_still_spends_the_day_budget(self):
        a = finding(id="a", check_slug="a", rank_score=300)
        b = finding(id="b", check_slug="b", rank_score=290)
        c = finding(id="c", check_slug="c", rank_score=400)
        self.run_at(at(6, 12), [a, b], surfaced_error=urllib.error.URLError("refused"))
        self.assertIn("New: 2 critical findings.", self.out.getvalue())
        # c outranks both, but today's two were already announced.
        self.run_at(at(6, 13), [c, a, b])
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.marks, [])
        self.run_at(at(7, 12), [c, a, b])
        self.assertIn("New: 2 critical findings.", self.out.getvalue())
        self.assertEqual([fid for fid, _ in self.marks], ["c", "a"])

    def test_an_announced_noncritical_whose_mark_failed_holds_back_a_higher_ranked_one(self):
        # Through stop-add: an unrecorded non-critical stops every addition,
        # as a pending one does, so the day's budget is never reached.
        rows = [finding(id=f"m{i}", check_slug=f"m{i}", severity="major", rank_score=90 - i) for i in range(3)]
        self.run_at(at(6, 16), rows, surfaced_error=urllib.error.URLError("refused"))
        self.assertIn("New: 3 findings, none of them critical.", self.out.getvalue())
        self.run_at(at(6, 17), [finding(id="m9", check_slug="m9", severity="major", rank_score=95)] + rows)
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.marks, [])

    def test_an_announced_critical_whose_mark_failed_still_holds_noncriticals_back(self):
        critical = finding(id="a", check_slug="a")
        self.run_at(at(6, 12), [critical], surfaced_error=urllib.error.URLError("refused"))
        self.run_at(at(6, 16), [critical, finding(id="m0", check_slug="m0", severity="major")])
        self.assertNotIn("none of them critical", self.out.getvalue())
        self.assertEqual(self.marks, [])

    def test_an_announced_noncritical_whose_mark_failed_still_stops_every_addition(self):
        major = finding(id="m0", check_slug="m0", severity="major")
        self.run_at(at(6, 16), [major], surfaced_error=urllib.error.URLError("refused"))
        self.run_at(at(6, 17), [finding(id="c", check_slug="c"), major])
        self.assertNotIn("New:", self.out.getvalue())
        self.assertEqual(self.marks, [])

    def test_a_mark_that_raises_skips_neither_the_other_marks_nor_the_daily_record(self):
        rows = [shown(at(5, 12), id="old"), finding(id="new", check_slug="x")]
        code = self.run_at(at(6, 12), rows, surfaced_error=http.client.IncompleteRead(b""))
        self.assertEqual(code, 0)
        self.assertEqual(self.err.getvalue().count("could not mark"), 2)
        self.assertEqual(self.state()[nudge.DAILY_KEY], "2026-10-06")

    def test_the_open_count_counts_items(self):
        rows = [finding(id=f"p{i}", object=f"d{i}") for i in range(3)] + [finding(id="x", check_slug="x")]
        self.run_at(at(6, 12), rows)
        self.assertIn("2 findings are open on the queue.", self.out.getvalue())

    def test_the_open_count_counts_provider_managed_observations_by_item(self):
        # Two lines of nameable rows, and ten observations of one check on
        # one cluster: three findings, one of them provider-managed.
        rows = [finding(id=f"p{i}", object=f"d{i}") for i in range(3)] + [finding(id="x", check_slug="x")]
        rows += [
            finding(id=f"obs{i}", check_slug="dns", namespace="kube-system", object=f"pod{i}",
                    provider_managed=True, actionable=False)
            for i in range(10)
        ]
        self.run_at(at(6, 12), rows)
        self.assertIn(
            "3 findings are open on the queue. Ask for the full list. 1 of them is provider-managed,",
            self.out.getvalue(),
        )

    def test_a_failed_expiry_costs_snooze_lateness_not_the_message(self):
        def fake_request(endpoint, path, body=None, method=""):
            if path == "/v1/findings/expire-snoozes":
                raise urllib.error.URLError("refused")
            if path == "/v1/findings/ranked":
                return {"findings": [finding()]}
            if path.startswith("/v1/findings/additions"):
                return {"critical": 0, "noncritical": 0}
            return {}

        with unittest.mock.patch.object(nudge, "_request", side_effect=fake_request):
            code = nudge.main([], clock=lambda: at(6, 12))
        self.assertEqual(code, 0)
        self.assertIn("no readinessProbe on api", self.out.getvalue())
        self.assertIn("could not expire lapsed snoozes", self.err.getvalue())


class FirstReportTests(NudgeHarness):
    def file_scan(self, when: datetime, text: str | None = None) -> Path:
        """Onboarding's marker as `bootstrap_scan_gate.py` writes it, filed at `when`."""
        marker = self.gateway / nudge.SCAN_FILED_MARKER
        marker.write_text(text if text is not None else f"task_id=t_1\nfiled_at={int(when.timestamp())}\n")
        return marker

    def test_nothing_is_added_while_the_first_report_is_on_its_way(self):
        self.file_scan(at(6, 9))
        self.run_at(at(6, 12), [finding()])
        self.assertEqual(self.out.getvalue(), "")
        self.assertEqual(self.marks, [])
        # A held run says so, so a stuck onboarding is not a silent one.
        err = self.err.getvalue()
        self.assertIn("adding nothing until the first inventory report's delivery is claimed", err)
        self.assertIn("or until 2026-10-07 09:00 UTC", err)
        self.assertIn(nudge.DELIVERED_MARKER, err)
        self.assertEqual(err.count(nudge.LOG_PREFIX), 1)

    def test_still_held_23_hours_after_the_scan_was_filed(self):
        self.file_scan(at(5, 13))
        self.run_at(at(6, 12), [finding()])
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("adding nothing until", self.err.getvalue())

    def test_no_longer_held_25_hours_after_the_scan_was_filed(self):
        self.file_scan(at(5, 11))
        self.run_at(at(6, 12), [finding()])
        self.assertIn("New: 1 critical finding.", self.out.getvalue())
        err = self.err.getvalue()
        self.assertIn("still undelivered more than 24 hours after its sweep was filed", err)
        self.assertIn("filed 2026-10-05 11:00 UTC by its filed_at= line", err)
        self.assertEqual(err.count(nudge.LOG_PREFIX), 1)

    def test_an_unparseable_marker_falls_back_to_its_mtime(self):
        for text in ("t_1", "task_id=t_1\nfiled_at=soon\n"):
            with self.subTest(text=text):
                marker = self.file_scan(at(6, 12), text)
                for filed, held in ((at(5, 13), True), (at(5, 11), False)):
                    os.utime(marker, (filed.timestamp(), filed.timestamp()))
                    # Each case starts from a fresh day: no addition announced yet.
                    (self.home / nudge.STATE_FILE).unlink(missing_ok=True)
                    self.err.seek(0)
                    self.err.truncate()
                    self.assertEqual(nudge.first_report_hold(self.gateway, at(6, 12))[0], held)
                    self.run_at(at(6, 12), [finding()])
                    self.assertIn("by its mtime", self.err.getvalue())
                    self.assertEqual("New: 1 critical finding." in self.out.getvalue(), not held)

    def test_a_marker_removed_while_it_is_read_is_not_held(self):
        # The re-arm runbook deletes it; a run that saw it a moment before
        # must not fail on the read that follows.
        self.assertIsNone(nudge._filed_at(self.gateway / nudge.SCAN_FILED_MARKER))
        self.file_scan(at(6, 9))
        with unittest.mock.patch.object(nudge, "_filed_at", return_value=None):
            self.assertEqual(nudge.first_report_hold(self.gateway, at(6, 12)), (False, ""))
            self.run_at(at(6, 12), [finding()])
        self.assertIn("New: 1 critical finding.", self.out.getvalue())

    def test_additions_start_once_it_was_delivered(self):
        self.file_scan(at(6, 9))
        (self.gateway / nudge.DELIVERED_MARKER).write_text("")
        self.run_at(at(6, 12), [finding()])
        self.assertIn("New: 1 critical finding.", self.out.getvalue())
        self.assertEqual(self.err.getvalue(), "")

    def test_an_install_that_never_ran_onboarding_is_not_held(self):
        self.assertEqual(nudge.first_report_hold(self.gateway, at(6, 12)), (False, ""))
        self.run_at(at(6, 12), [finding()])
        self.assertIn("New: 1 critical finding.", self.out.getvalue())
        self.assertEqual(self.err.getvalue(), "")

    def test_the_gateway_home_is_found_from_a_shell_in_the_container(self):
        self.file_scan(at(6, 9))
        with unittest.mock.patch.dict(os.environ, {"HERMES_HOME": str(self.gateway)}):
            self.assertEqual(nudge._homes(), (self.gateway, self.home))
            self.assertTrue(nudge.first_report_hold(nudge._homes()[0], at(6, 12))[0])


class FailureTests(NudgeHarness):
    def test_an_unreadable_queue_fails_once_a_day(self):
        refused = urllib.error.URLError("refused")
        self.assertEqual(self.run_at(at(6, 3), [], fail=refused), 1)
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("could not read the queue", self.err.getvalue())

        self.assertEqual(self.run_at(at(6, 4), [], fail=refused), 0)
        self.assertEqual(self.run_at(at(6, 23), [], fail=refused), 0)
        self.assertIn("already reported a failure today", self.err.getvalue())

        self.assertEqual(self.run_at(at(7, 0), [], fail=refused), 1)

    def test_an_unexpected_read_error_also_fails_once_a_day(self):
        broken = http.client.IncompleteRead(b"{")
        self.assertEqual(self.run_at(at(6, 12), [], fail=broken), 1)
        self.assertEqual(self.run_at(at(6, 13), [], fail=broken), 0)

    def test_a_failure_after_the_read_fails_once_a_day(self):
        with unittest.mock.patch.object(fq, "pace", side_effect=KeyError("rubric")):
            self.assertEqual(self.run_at(at(6, 12), [finding()]), 1)
            self.assertEqual(self.run_at(at(6, 13), [finding()]), 0)
        self.assertEqual(self.out.getvalue(), "")
        self.assertIn("failed before posting", self.err.getvalue())

    def test_a_failure_does_not_spend_the_daily_part(self):
        self.run_at(at(6, 12), [], fail=urllib.error.URLError("refused"))
        self.run_at(at(6, 13), [shown(at(5, 12), id="old")])
        self.assertIn("Still open", self.out.getvalue())


class RosterTests(unittest.TestCase):
    """The job entry is the whole deployment of this script, so it earns a test."""

    def test_the_roster_runs_this_script_hourly_and_adds_after_the_last_daily_audit(self):
        roster = json.loads(
            (Path(__file__).parent.parent / "cron" / "jobs.json").read_text(encoding="utf-8")
        )
        jobs = {job["id"]: job for job in roster["jobs"]}
        job = jobs["findings-morning-nudge"]

        self.assertEqual(job["script"], "findings_nudge.py")
        self.assertTrue(job["no_agent"])
        self.assertTrue(job["enabled"])
        # Anything but an audible target writes the run to `last_output` and
        # delivers nowhere, so a broken nudge would read as a quiet hour.
        self.assertIn(job["deliver"], ("chat", "all"))
        self.assertEqual(job["schedule"]["expr"], job["schedule"]["display"])
        # Hourly, so the afternoon hour is a setting rather than a schedule.
        self.assertEqual(job["schedule"]["expr"].split()[1:], ["*", "*", "*", "*"])

        def minute_of_day(job_id):
            minute, hour_field = jobs[job_id]["schedule"]["expr"].split()[:2]
            return int(hour_field) * 60 + int(minute)

        # The daily audits are themselves a source of findings; adding before
        # they ran would publish a list a day behind its own inputs.
        latest_daily = max(
            minute_of_day(job_id)
            for job_id, other in jobs.items()
            if other["schedule"]["expr"].split()[4] == "*"
            and other["schedule"]["expr"].split()[1].isdigit()
            and not other.get("no_agent")
        )
        self.assertGreater(fq.REMIND_HOUR * 60, latest_daily)


if __name__ == "__main__":
    unittest.main()
