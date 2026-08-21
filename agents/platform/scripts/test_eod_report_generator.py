#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for the deterministic k8s-event-watcher daily activity summary."""

import contextlib
import datetime
import io
import os
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))

import eod_report_generator
from eod_report_generator import (
    LISTED_SEVERITIES,
    filter_and_aggregate_events,
    generate_markdown_report,
    load_intercepted_events,
)


def exclude(*namespaces):
    """Replace the excluded-namespace set for the duration of a `with` block."""
    return mock.patch.dict(os.environ, {"EOD_EXCLUDE_NAMESPACES": ",".join(namespaces)})


def recap(events, **kwargs):
    """Aggregate `events` and render them, returning `(summary, report)`.

    Every test that asserts on the rendered recap goes through here, so the
    cluster name and date are fixed rather than reaching for the clock. Both
    halves are returned because most assertions want to pin the number and the
    line that prints it together.
    """
    kwargs.setdefault("cluster_name", "test-cluster")
    kwargs.setdefault("report_date", "2026-08-14")
    summary = filter_and_aggregate_events(events)
    return summary, generate_markdown_report(summary, **kwargs)


# `1. 🔹 *`ns/workload`* (`reason` • 4 events)`
_ENTRY = re.compile(
    r"^\d+\. "
    + re.escape(eod_report_generator._LISTED_EMOJI)
    + r" \*`(?P<label>[^`]+)`\* \(`(?P<reason>[^`]+)` • (?P<count>\d+) events?\)$"
)


def listing_of(report):
    """The workload breakdown, parsed into dicts, or `[]` when the section is absent.

    Asserting on the parsed section rather than on `assertIn("BackOff", report)`
    is the difference between "this workload is in the listing" and "this string
    is somewhere in the recap" — the latter also passes when the name only
    appears in the headline, or in a heading, or in another workload's message.
    """
    heading = f"*{eod_report_generator._SECTION_HEADING}*"
    lines = report.splitlines()
    if heading not in lines:
        return []
    entries = []
    for line in lines[lines.index(heading) + 1:]:
        matched = _ENTRY.match(line)
        if matched:
            entries.append(
                {
                    "label": matched["label"],
                    "reason": matched["reason"],
                    "count": int(matched["count"]),
                    "message": None,
                }
            )
        elif line.startswith("    • *Issue:* ") and entries:
            entries[-1]["message"] = line[len("    • *Issue:* "):]
        elif not line or line.startswith("_"):
            break
    return entries


def listed_labels(report):
    """`namespace/workload` for each entry in the breakdown, in printed order."""
    return [entry["label"] for entry in listing_of(report)]



def event(**overrides):
    """One ledger row, with the fields a test does not care about filled in.

    `occurrences` is 1 because that is the only value production writes: the
    watcher drops duplicates before /inject and the payload's `count` comes
    from the dedupNewIncident branch, which hardcodes 1. Tests that assert on
    richer values were asserting a shape the system cannot produce. The one
    exception is a test about the *unit* a count is kept in, where the whole
    point is that rows and occurrences stop agreeing when they differ — see
    `test_the_total_is_in_the_same_unit_the_listing_prints`.
    """
    row = {
        "namespace": "prod-api",
        "workload": "payment-api",
        "object_kind": "Pod",
        # One pod per workload unless a test overrides it. The recap counts
        # withheld alerts per UID, so N rows of one workload is one pod
        # re-offering, and a test that means N replicas has to say so.
        "object_uid": None,
        "reason": "OOMKilled",
        "message": "Memory cgroup out of memory",
        "severity": "Critical",
        "occurrences": 1,
        "notified": True,
        "created_at": "2026-08-10 12:00:00",
    }
    row.update(overrides)
    if row.get("object_uid") is None:
        row["object_uid"] = f"uid-{row['workload']}"
    return row


def listed(**overrides):
    """One ledger row the recap will actually list: informational, held back from chat.

    `event()` defaults to an alerted Critical because that is the row most of the
    tally tests are about. The listing is fixed to Info in `LISTED_SEVERITIES`, so
    a test about grouping, ranking or formatting has to seed this instead — with
    an `event()` its entry list is empty and every assertion about ordering passes
    over nothing.
    """
    return event(severity="Info", notified=False, **overrides)


class TestEODWatcherRecap(unittest.TestCase):
    """The listing is fixed to Info in `LISTED_SEVERITIES` and nothing widens it.

    A test about grouping or formatting therefore seeds `listed()` rows rather
    than `event()`, whose default Critical is never listed.
    """

    def test_the_recap_lists_the_suppressed_event_and_not_the_alerted_one(self):
        """The inversion, stated directly: chat got the alert, the recap gets the rest.

        A Critical was posted to chat the moment it happened, so naming it again
        in the recap repeats what the on-call already read. The informational event
        is the only one of the two nobody was told about.
        """
        events = [
            event(),
            event(reason="BackOff", severity="Info", notified=False),
        ]
        summary, report = recap(events)

        self.assertEqual(summary["alerts_posted"], 1)
        self.assertEqual(summary["suppressed_info"], 1)
        self.assertEqual(summary["total_occurrences"], 2)
        # Both counted in the telemetry; only the suppressed one is listed.
        self.assertEqual(summary["unique_incidents"], 2)
        self.assertEqual([e["reason"] for e in summary["entries"]], ["BackOff"])
        self.assertEqual([e["reason"] for e in listing_of(report)], ["BackOff"])
        self.assertNotIn("OOMKilled", report)
        # The alert is still accounted for as a number, so a digest of nothing
        # but routine cannot be mistaken for a fleet that had a quiet day.
        self.assertIn("*1 alert* went to chat as it happened", report)
        self.assertIn("*1 informational event* held back from chat in this window", report)

    def test_one_reason_at_two_grades_does_not_share_a_group(self):
        """`BackOff` is Info when typed Normal and Warning when it is not.

        Keyed without the grade, forty Info rows and one Warning row are a
        single group whose severity is whichever arrived first — so the whole
        group is listed or dropped on a coin flip, taking the other grade with
        it.
        """
        events = [
            event(workload="api", reason="BackOff", severity="Warning", notified=True)
        ] + [
            event(workload="api", reason="BackOff", severity="Info", notified=False)
            for _ in range(40)
        ]
        summary = filter_and_aggregate_events(events)

        self.assertEqual(summary["unique_incidents"], 2)
        self.assertEqual([e["count"] for e in summary["entries"]], [40])
        self.assertEqual(summary["entries"][0]["severity"], "Info")
        self.assertEqual(summary["alerts_posted"], 1)
        self.assertEqual(summary["suppressed_info"], 40)

    def test_routine_churn_is_the_subject_not_the_thing_crowded_out(self):
        """The same fixture the old ranking test used, with the intent inverted.

        Three routine image-pull BackOffs outrank the two real warnings by
        count. That used to be the failure mode — the warnings fell off a list
        read for warnings. Now the warnings went to chat when they happened and
        the churn is what the digest is for, so the ranking is simply correct.
        """
        events = [
            event(
                workload=f"noisy-{i}", reason="BackOff", severity="Info",
                notified=False,
            )
            for i in range(3)
            for _ in range(40)
        ] + [
            event(workload="payment-api", reason="OOMKilled") for _ in range(6)
        ] + [
            event(workload="checkout", reason="CrashLoopBackOff") for _ in range(4)
        ]
        summary, report = recap(events)

        self.assertEqual(
            sorted(e["workload"] for e in summary["entries"]),
            ["noisy-0", "noisy-1", "noisy-2"],
        )
        self.assertEqual(
            sorted(listed_labels(report)),
            ["prod-api/noisy-0", "prod-api/noisy-1", "prod-api/noisy-2"],
        )

    def test_the_entry_list_is_cut_at_ten_and_says_how_many_it_dropped(self):
        """Info churn is high-cardinality, so silent truncation loses most of it."""
        events = [
            event(workload=f"svc-{i:02d}", reason="BackOff", severity="Info", notified=False)
            for i in range(14)
        ]
        summary, report = recap(events)

        self.assertEqual(len(summary["entries"]), 14)
        self.assertEqual(len(listing_of(report)), 10)
        self.assertIn("…and 4 further groups not listed.", report)


    def test_an_all_suppressed_day_is_the_populated_report_now(self):
        """Nine informational events used to be a blank recap; they are its body."""
        summary, report = recap([event(severity="Info", notified=False) for _ in range(9)])

        self.assertEqual(len(summary["entries"]), 1)
        self.assertEqual(summary["entries"][0]["count"], 9)
        self.assertEqual([e["count"] for e in listing_of(report)], [9])
        self.assertIn("*0 alerts* went to chat", report)
        self.assertIn("*9 informational events* held back from chat in this window", report)

    def test_a_cap_dropped_alert_is_not_an_informational_event(self):
        """`notified = 0` covers two outcomes; counting them as one hides alerts.

        Forty OOMKills against a ceiling of ten leaves thirty rows the severity
        gate never touched — graded Critical, on their way to chat, stopped by
        the budget. Counted as "informational" they inflate the one number this
        recap exists to report, and read as routine churn.

        The recap does not name them; this is about the tally staying honest.
        """
        events = [event() for _ in range(10)] + [
            event(notified=False) for _ in range(30)
        ]
        summary = filter_and_aggregate_events(events)

        self.assertEqual(summary["alerts_posted"], 10)
        self.assertEqual(summary["cap_dropped"], 30)
        self.assertEqual(summary["suppressed_info"], 0)

        report = generate_markdown_report(
            summary, cluster_name="test-cluster"
        )
        self.assertIn("*0 informational events* held back from chat in this window", report)

    def test_a_withheld_alert_is_counted_but_not_named(self):
        """Told, not listed.

        A ceiling-withheld alert is graded Critical or Warning, so it is not
        this recap's subject and does not join the breakdown — the workload and
        the reason stay out. That it happened at all is a different question,
        and the answer has to be printed: the recap is the only channel that
        reaches the on-call unprompted, and alert loss is the one thing this
        ledger uniquely records. `GET /v1/alert-quota` and the quota counter
        only help a reader who already suspects it.

        SOP: "What this recap does not report".
        """
        events = [
            event(workload="checkout", reason="CrashLoopBackOff", notified=False)
            for _ in range(12)
        ]
        summary = filter_and_aggregate_events(events)
        report = generate_markdown_report(
            summary, cluster_name="test-cluster"
        )

        self.assertEqual(summary["cap_dropped"], 12)
        self.assertIn("*1 alert withheld by the daily ceiling and never reached chat.*", report)
        # Counted, and still not listed: the breakdown is informational events.
        self.assertNotIn("checkout", report)

    def test_the_withheld_line_sizes_the_loss_in_alerts_not_rows(self):
        """One failure re-offered all afternoon is one alert chat did not get.

        A quota refusal makes the watcher forget the dedup entry, so the same
        crash loop writes a fresh row every kubelet re-emit. Chat would have
        received one alert and deduplicated the rest, so printing the row count
        sizes the loss two orders of magnitude high. The row count still vetoes
        the all-clear, where any row at all is the whole question.
        """
        events = [
            event(workload="api", reason="CrashLoopBackOff", notified=False)
            for _ in range(50)
        ] + [event(workload="web", reason="OOMKilled", notified=False)]
        summary = filter_and_aggregate_events(events)
        report = generate_markdown_report(summary, cluster_name="test-cluster")

        self.assertEqual(summary["cap_dropped"], 51)
        self.assertEqual(summary["cap_dropped_alerts"], 2)
        self.assertIn("*2 alerts withheld by the daily ceiling and never reached chat.*", report)
        self.assertNotIn("Nothing was held back from chat in this window", report)
        self.assertNotIn("CrashLoopBackOff", report)

    def test_the_withheld_line_counts_replicas_separately(self):
        """The other half of the same number, and it fails the opposite way.

        `clean_workload_name` strips the replica suffix before the row is
        written, so forty pods of one Deployment share a `workload`, a
        `namespace`, a `reason`, a `severity` and `notified = 0`. Counting on
        those alone reports a rollout that OOMKilled the whole Deployment as
        one withheld alert, which is the reading that makes an on-call skip it.
        The watcher held forty dedup keys and forty alerts is what chat lost.
        """
        events = [
            event(workload="payment-api", object_uid=f"pod-{i}", notified=False)
            for i in range(30)
        ]
        summary = filter_and_aggregate_events(events)
        report = generate_markdown_report(summary, cluster_name="test-cluster")

        self.assertEqual(summary["cap_dropped_alerts"], 30)
        self.assertIn("*30 alerts withheld by the daily ceiling and never reached chat.*", report)

    def test_a_replica_re_offering_is_still_one_withheld_alert(self):
        """The control for the two tests above: both effects at once.

        Three pods of one Deployment, each re-offered ten times after the
        ceiling refused it. Neither counting rows nor counting cleaned
        workloads gets this right.
        """
        events = [
            event(workload="payment-api", object_uid=f"pod-{i}", notified=False)
            for i in range(3)
            for _ in range(10)
        ]
        summary = filter_and_aggregate_events(events)

        self.assertEqual(summary["cap_dropped"], 30)
        self.assertEqual(summary["cap_dropped_alerts"], 3)

    def test_a_clean_day_is_not_told_about_alerts_it_did_not_lose(self):
        """The control. Without it the assertion above passes on a fixed string."""
        summary = filter_and_aggregate_events([event(), listed()])
        report = generate_markdown_report(summary, cluster_name="test-cluster")

        self.assertEqual(summary["cap_dropped"], 0)
        self.assertEqual(summary["delivery_failed"], 0)
        self.assertNotIn("withheld by the daily ceiling", report)
        self.assertNotIn("failed to post to chat", report)

    def test_a_failed_delivery_is_counted_on_its_own_line(self):
        """Nothing else counts this one at all.

        A ceiling drop is at least in `k8s_event_watcher_events_quota_suppressed_total`.
        A failed post is recorded as sent by every metric there is; the ledger's
        `delivery_error` column is its only trace.
        """
        summary = filter_and_aggregate_events(
            [event(notified=True, delivery_error="502 from chat.googleapis.com")
             for _ in range(3)]
        )
        report = generate_markdown_report(summary, cluster_name="test-cluster")

        self.assertEqual(summary["delivery_failed"], 3)
        self.assertIn("*3 alerts failed to post to chat.*", report)

    def test_a_withheld_alert_is_never_covered_by_an_all_clear(self):
        """The regression this file exists to prevent, at its sharpest.

        Thirty Critical alerts the ceiling ate: chat never saw them and no triage
        session was opened. Printing "nothing was held back" over them, or a
        green header, is the same lie in one character. Now that the count is
        printed the card says so outright — but the veto stays, because a
        reader who skims to the ✅ must not find one.
        """
        events = [event(notified=False) for _ in range(30)]
        summary = filter_and_aggregate_events(events)
        report = generate_markdown_report(
            summary, cluster_name="test-cluster"
        )

        self.assertEqual(summary["cap_dropped"], 30)
        self.assertIn("*1 alert withheld by the daily ceiling and never reached chat.*", report)
        self.assertNotIn("Nothing was held back from chat in this window", report)
        self.assertNotIn("🟢", report)
        self.assertIn("📊", report)

    def test_the_withheld_line_is_above_the_body(self):
        """Under the breakdown it is a footnote; above it, it is the headline.

        A day with both a listing and a ceiling drop must not bury the drop
        beneath five rows of informational churn.
        """
        events = [listed() for _ in range(6)] + [event(notified=False) for _ in range(4)]
        summary = filter_and_aggregate_events(events)
        report = generate_markdown_report(summary, cluster_name="test-cluster")

        self.assertLess(
            report.index("withheld by the daily ceiling"),
            report.index("Forwarded"),
        )

    def test_an_unreadable_ledger_is_not_a_quiet_day_either(self):
        """An unopened table measures nothing, so it cannot clear the day.

        `problems=` by keyword, because the second positional parameter is
        `cluster_name`: passed there, the list never reaches the guard and this
        read as a test of a branch it did not enter.
        """
        summary = filter_and_aggregate_events(
            [event(notified=False) for _ in range(3)]
        )
        self.assertEqual(summary["cap_dropped"], 3)

        report = generate_markdown_report(
            summary,
            cluster_name="test-cluster",
            problems=["`/nope/session_kv.db` — no session KV database found"],
        )
        self.assertNotIn("Nothing was held back from chat in this window", report)
        self.assertIn("could not read the event ledger", report)
        self.assertIn("🔴", report)

    def test_two_clusters_do_not_merge_one_workload(self):
        """One session KV database serves every cluster profile in the pod.

        Keyed on namespace/workload/reason alone, `prod-api/payment-api` on two
        clusters is one line with the counts added together, and the recap says
        one service failed twice as often as it did while the other is invisible.
        """
        events = [listed(cluster="cluster-a"), listed(cluster="cluster-b")]
        summary = filter_and_aggregate_events(events)

        self.assertEqual(summary["unique_incidents"], 2)
        self.assertEqual([e["count"] for e in summary["entries"]], [1, 1])

        # The report runs on one cluster but reports rows from several, so a
        # foreign cluster is named and the local one is left as it always was.
        report = generate_markdown_report(summary, cluster_name="cluster-a")
        self.assertEqual(
            sorted(listed_labels(report)),
            ["cluster-b:prod-api/payment-api", "prod-api/payment-api"],
        )

    def test_excluded_namespace_leaves_the_headline_counts(self):
        """The summary must describe the same scope the breakdown does."""
        events = [event()] + [event(namespace="kube-system") for _ in range(500)]
        summary = filter_and_aggregate_events(events)

        self.assertEqual(summary["total_occurrences"], 1)
        self.assertEqual(summary["unique_incidents"], 1)

    def test_ledger_workload_names_are_used_as_stored(self):
        """The server already stripped the replica hash; a second pass merges services.

        `api-store` and `api-cache` both end in five alphanumerics, so a
        kind-agnostic strip turns both into `api` and the recap reports one
        workload that does not exist.
        """
        events = [
            listed(namespace="prod", workload="api-store", reason="CrashLoopBackOff"),
            listed(namespace="prod", workload="api-cache", reason="CrashLoopBackOff"),
        ]
        summary = filter_and_aggregate_events(events)

        self.assertEqual(
            sorted(e["workload"] for e in summary["entries"]),
            ["api-cache", "api-store"],
        )

    def test_rows_the_server_already_collapsed_group_together(self):
        events = [listed(workload="payment-api") for _ in range(2)]
        summary = filter_and_aggregate_events(events)

        self.assertEqual(len(summary["entries"]), 1)
        self.assertEqual(summary["entries"][0]["workload"], "payment-api")
        self.assertEqual(summary["entries"][0]["count"], 2)

    def test_report_uses_chat_markup_not_markdown(self):
        """stdout is delivered verbatim to Chat/Slack, which render neither."""
        _, report = recap([event()])

        self.assertNotIn("**", report)
        self.assertNotIn("###", report)

    def test_no_noise_reduction_claim_is_printed(self):
        """Every ledger row is one forwarded incident; there is no ratio to report."""
        summary, report = recap([event() for _ in range(3)])

        self.assertNotIn("noise reduction", report)
        self.assertNotIn("dedup_ratio", summary)




class TestReportWindow(unittest.TestCase):
    """Monday has to reach back over a weekend the cron never ran on.

    And the card has to say when it did. `report_date` is a single date, so a
    72-hour run headed by Monday's date attributes three days of churn to one
    unless the header discloses the window.
    """

    def test_monday_covers_the_weekend(self):
        # 2026-08-10 is a Monday.
        monday = datetime.datetime(2026, 8, 10, 17, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(eod_report_generator.default_window_hours(monday), 72)

    def test_other_weekdays_look_back_a_day(self):
        tuesday = datetime.datetime(2026, 8, 11, 17, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(eod_report_generator.default_window_hours(tuesday), 24)

    def _render(self, **kwargs):
        return generate_markdown_report(
            filter_and_aggregate_events([listed()]),
            cluster_name="c1",
            report_date="2026-08-10",
            **kwargs,
        )

    def test_a_weekend_catch_up_names_its_window_in_the_header(self):
        report = self._render(window_hours=72)

        self.assertIn("(2026-08-10, last 72h)", report.splitlines()[0])

    def test_an_ordinary_day_leaves_the_header_alone(self):
        """The control. Stating the default on every card is noise."""
        report = self._render(window_hours=24)

        self.assertIn("(2026-08-10)", report.splitlines()[0])
        self.assertNotIn("last 24h", report)

    def test_a_hand_run_window_is_named_too(self):
        """`--window-hours` is an operator override, and it moves the same scope."""
        report = self._render(window_hours=6)

        self.assertIn("(2026-08-10, last 6h)", report.splitlines()[0])

    def test_the_closing_total_does_not_call_the_window_today(self):
        """It is summed over `window_hours`, which on Mondays is three days.

        The ✅ line beside it is worded for the window, and this one has to
        agree: a card cannot label a weekend's count with a single day.
        """
        report = self._render(window_hours=72)

        self.assertIn("held back from chat in this window", report)
        self.assertNotIn("today", report)

    def test_main_hands_the_window_to_the_renderer(self):
        """The header can only disclose a window `main` bothers to pass down.

        Driven end to end because the renderer's default hides a broken wire:
        a `main` that resolves the window for the query and forgets to pass it
        on still prints a plausible card, and every unit test still passes.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "session_kv.db")
            conn = sqlite3.connect(path)
            with conn:
                conn.execute(
                    "CREATE TABLE intercepted_events ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT, cluster TEXT DEFAULT '',"
                    " namespace TEXT, workload TEXT, object_uid TEXT DEFAULT '',"
                    " object_kind TEXT, reason TEXT, message TEXT, severity TEXT,"
                    " occurrences INTEGER, notified INTEGER, created_at TIMESTAMP)"
                )
            conn.close()

            argv = ["eod_report_generator.py", "--db", path, "--cluster-name", "c1"]
            out = io.StringIO()
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.object(eod_report_generator, "default_window_hours",
                                      return_value=72), \
                    contextlib.redirect_stdout(out):
                eod_report_generator.main()

        self.assertIn(", last 72h)", out.getvalue().splitlines()[0])


class TestLedgerLoading(unittest.TestCase):
    """The generator reads the table session_kv_server actually writes."""

    def _db(self, rows, table_sql=None):
        path = os.path.join(self.tmp.name, "session_kv.db")
        conn = sqlite3.connect(path)
        with conn:
            conn.execute(
                table_sql
                or """
                CREATE TABLE intercepted_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    cluster     TEXT NOT NULL DEFAULT '',
                    namespace   TEXT NOT NULL DEFAULT '',
                    workload    TEXT NOT NULL DEFAULT '',
                    object_uid  TEXT NOT NULL DEFAULT '',
                    object_kind TEXT NOT NULL DEFAULT '',
                    reason      TEXT NOT NULL DEFAULT '',
                    message     TEXT NOT NULL DEFAULT '',
                    severity    TEXT NOT NULL DEFAULT '',
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    notified    INTEGER NOT NULL DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            for ns, workload, reason, notified, age_hours in rows:
                conn.execute(
                    "INSERT INTO intercepted_events "
                    "(namespace, workload, object_uid, object_kind, reason, message, "
                    " severity, occurrences, notified, created_at) "
                    "VALUES (?, ?, ?, 'Pod', ?, 'msg', 'Warning', 2, ?, "
                    "        datetime('now', ?))",
                    (ns, workload, f"uid-{workload}", reason, notified, f"-{age_hours} hours"),
                )
        conn.close()
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_window_excludes_older_rows(self):
        path = self._db(
            [
                ("prod", "api", "OOMKilled", 1, 2),
                ("prod", "worker", "BackOff", 0, 40),
            ]
        )
        rows = load_intercepted_events(path, window_hours=24)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workload"], "api")
        self.assertTrue(rows[0]["notified"])

    def test_the_cluster_column_is_read_when_present(self):
        """The column the recap groups and scopes by, read as stored."""
        path = self._db(
            [("prod", "api", "OOMKilled", 1, 2)],
            table_sql="""
            CREATE TABLE intercepted_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cluster     TEXT NOT NULL DEFAULT 'cluster-b',
                namespace   TEXT NOT NULL DEFAULT '',
                workload    TEXT NOT NULL DEFAULT '',
                object_uid  TEXT NOT NULL DEFAULT '',
                object_kind TEXT NOT NULL DEFAULT '',
                reason      TEXT NOT NULL DEFAULT '',
                message     TEXT NOT NULL DEFAULT '',
                severity    TEXT NOT NULL DEFAULT '',
                occurrences INTEGER NOT NULL DEFAULT 1,
                notified    INTEGER NOT NULL DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        self.assertEqual(
            load_intercepted_events(path, window_hours=24)[0]["cluster"], "cluster-b"
        )

    def test_a_table_without_the_cluster_column_is_a_read_failure(self):
        """A pre-release shape the writer cannot use, not an older one to tolerate.

        `record_intercepted_event` names `cluster` in its INSERT
        unconditionally and swallows the resulting `no such column` per event,
        so on this shape nothing is ever recorded. Reading it as an empty ledger
        would print a green all-clear every weekday over a table no event can
        reach. It is reported as a read failure, which the recap renders as 🔴
        with the path named.
        """
        err = io.StringIO()
        path = self._db(
            [("prod", "api", "OOMKilled", 1, 2)],
            table_sql="""
            CREATE TABLE intercepted_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace   TEXT NOT NULL DEFAULT '',
                workload    TEXT NOT NULL DEFAULT '',
                object_uid  TEXT NOT NULL DEFAULT '',
                object_kind TEXT NOT NULL DEFAULT '',
                reason      TEXT NOT NULL DEFAULT '',
                message     TEXT NOT NULL DEFAULT '',
                severity    TEXT NOT NULL DEFAULT '',
                occurrences INTEGER NOT NULL DEFAULT 1,
                notified    INTEGER NOT NULL DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        problems = []
        with mock.patch.object(eod_report_generator.sys, "stderr", err):
            rows = load_intercepted_events(path, window_hours=24, problems=problems)

        self.assertEqual(rows, [])
        self.assertEqual(len(problems), 1)
        self.assertIn(path, problems[0])
        self.assertIn("cluster", problems[0])
        self.assertIn("drop the table", problems[0])

        report = generate_markdown_report(
            filter_and_aggregate_events(rows),
            cluster_name="prod",
            report_date="2026-08-14",
            problems=problems,
        )
        self.assertTrue(report.startswith("🔴"), report.splitlines()[0])
        self.assertNotIn("Nothing was held back from chat in this window", report)

    def test_a_table_without_the_uid_column_is_a_read_failure_too(self):
        """`object_uid` is in the INSERT, so it fails the same way `cluster` does.

        A pre-release database carrying every other column still records
        nothing, and the empty table it leaves behind reads as a quiet fleet.
        The distinction worth keeping is against `delivery_error`, which the
        INSERT does not name and which is therefore tolerated as ''.
        """
        err = io.StringIO()
        # Seeded with no rows, because that is the production shape: the writer
        # names `object_uid`, so on this table every write raises and is
        # swallowed, and the recap meets an empty ledger.
        path = self._db(
            [],
            table_sql="""
            CREATE TABLE intercepted_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cluster     TEXT NOT NULL DEFAULT '',
                namespace   TEXT NOT NULL DEFAULT '',
                workload    TEXT NOT NULL DEFAULT '',
                object_kind TEXT NOT NULL DEFAULT '',
                reason      TEXT NOT NULL DEFAULT '',
                message     TEXT NOT NULL DEFAULT '',
                severity    TEXT NOT NULL DEFAULT '',
                occurrences INTEGER NOT NULL DEFAULT 1,
                notified    INTEGER NOT NULL DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        )
        problems = []
        with mock.patch.object(eod_report_generator.sys, "stderr", err):
            rows = load_intercepted_events(path, window_hours=24, problems=problems)

        self.assertEqual(rows, [])
        self.assertEqual(len(problems), 1)
        self.assertIn("object_uid", problems[0])
        self.assertIn("drop the table", problems[0])

    def test_missing_table_reports_rather_than_crashing(self):
        """A volume that predates the ledger has the DB but not the table."""
        path = os.path.join(self.tmp.name, "session_kv.db")
        sqlite3.connect(path).close()

        self.assertEqual(load_intercepted_events(path), [])

    def test_absent_database_says_so_on_stderr(self):
        """An unmounted volume must not read as a quiet fleet."""
        err = io.StringIO()
        with mock.patch.object(eod_report_generator.sys, "stderr", err):
            rows = load_intercepted_events(os.path.join(self.tmp.name, "nope.db"))

        self.assertEqual(rows, [])
        self.assertIn("no session KV database found", err.getvalue())

    def test_the_env_var_the_server_reads_is_searched_first(self):
        """SESSION_KV_DB_PATH is what session_kv_server.py and the operator use."""
        path = self._db([("prod", "api", "OOMKilled", 1, 2)])

        with mock.patch.dict(os.environ, {"SESSION_KV_DB_PATH": path}):
            rows = load_intercepted_events(window_hours=24)

        self.assertEqual([r["workload"] for r in rows], ["api"])



class TestTheRecapReadsOneDatabaseAndOnlyATrustedOne(unittest.TestCase):
    """A fallback that succeeds is as dangerous as one that fails loudly.

    `load_intercepted_events` stops at the first candidate that reads and
    discards the earlier failures, so a search that quietly moves on renders a
    normal-looking recap over whatever it landed on. What keeps that honest is
    the candidate list: nothing on it is writable by the workload.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.err = io.StringIO()
        patcher = mock.patch.object(eod_report_generator.sys, "stderr", self.err)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ledger(self, name, workload):
        path = os.path.join(self.tmp.name, name)
        conn = sqlite3.connect(path)
        with conn:
            conn.execute(
                "CREATE TABLE intercepted_events (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " cluster TEXT DEFAULT '', namespace TEXT, workload TEXT,"
                " object_uid TEXT DEFAULT '', object_kind TEXT,"
                " reason TEXT, message TEXT, severity TEXT, occurrences INTEGER,"
                " notified INTEGER, created_at TIMESTAMP, delivery_error TEXT DEFAULT '')"
            )
            conn.execute(
                "INSERT INTO intercepted_events (namespace, workload, object_kind, reason,"
                " message, severity, occurrences, notified, created_at)"
                " VALUES ('prod', ?, 'Pod', 'OOMKilled', 'm', 'Critical', 1, 1, datetime('now'))",
                (workload,),
            )
        conn.close()
        return path

    def test_no_built_in_candidate_path_is_writable_by_the_workload(self):
        """`/tmp` held a candidate, and anything the agent runs can write there.

        A stray file — a leftover scratch database from a debugging session, or
        one planted deliberately — would have supplied the whole recap.

        `SESSION_KV_DB_PATH` is cleared rather than asserted on. An operator who
        points it somewhere odd has made a choice; the built-in list is what
        answers when nobody chose, and that is what must not be world-writable.
        Clearing it also makes this deterministic: the variable is set by other
        tests in this suite, and to a tempdir under `/tmp`.
        """
        with mock.patch.dict(os.environ, {}, clear=True):
            candidates = eod_report_generator.default_db_paths()
        self.assertTrue(candidates)
        for path in candidates:
            self.assertFalse(
                path.startswith("/tmp/"),
                f"{path} is world-writable in the agent container",
            )

    def test_the_named_ledger_is_the_one_read(self):
        path = self._ledger("session_kv.db", "api")
        events = load_intercepted_events(path, window_hours=24)
        self.assertEqual([e["workload"] for e in events], ["api"])




class TheAllClearClaimsOnlyWhatWasMeasured(unittest.TestCase):
    """The recap never contacts the watcher, so it cannot vouch for it.

    Both tables it reads are written by the daemon's `/inject` path, so zero
    rows means no event arrived — which a dead, crash-looping or deliberately
    stopped watcher produces exactly as readily as a quiet fleet.
    `EVENT_WATCHER_ENABLED=false` is a documented emergency stop, and the old
    wording gave it a daily green light for as long as it stayed off.
    """

    def test_the_all_clear_does_not_assert_the_daemon_is_running(self):
        _, report = recap([])
        self.assertIn("✅", report)
        self.assertNotIn("Watcher daemon active", report)
        self.assertNotIn("streaming GKE events", report)

    def test_it_says_what_it_actually_read_instead(self):
        _, report = recap([])
        self.assertIn("reports the ledger, not the watcher", report)

    def test_a_day_whose_events_all_reached_chat_still_reads_true(self):
        """The branch fires here too, so it cannot claim an empty ledger.

        One Critical was forwarded and posted: nothing was held back, but
        something certainly reached the ledger.
        """
        events = [
            {
                "cluster": "", "namespace": "prod", "workload": "api", "object_kind": "Pod",
                "reason": "OOMKilled", "message": "m", "severity": "Critical",
                "occurrences": 1, "notified": True, "delivery_error": "",
                "created_at": "2026-08-14 10:00:00",
            }
        ]
        _, report = recap(events)
        self.assertIn("Nothing was held back from chat", report)
        self.assertNotIn("No events reached the ledger", report)


class TestAnUnreadableLedgerIsNotAQuietDay(unittest.TestCase):
    """A broken reporting path must not render as a clean bill of health.

    Every read failure returns `[]`, which aggregates to the same empty summary
    a quiet fleet produces. The warning goes to stderr, and this job runs
    `no_agent` — its stdout is the entire chat message, so stderr reaches the
    container log and nobody else. Left there, the one reader who could notice
    the watcher had stopped is told in green that it is streaming fine.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.err = io.StringIO()
        patcher = mock.patch.object(eod_report_generator.sys, "stderr", self.err)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _render(self, db_path):
        problems = []
        events = load_intercepted_events(db_path, window_hours=24, problems=problems)
        summary = filter_and_aggregate_events(events)
        return generate_markdown_report(
            summary,
            cluster_name="prod",
            report_date="2026-08-14",
            problems=problems,
        ), problems

    def _empty_ledger(self):
        path = os.path.join(self.tmp.name, "session_kv.db")
        conn = sqlite3.connect(path)
        with conn:
            conn.execute(
                "CREATE TABLE intercepted_events ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT, cluster TEXT DEFAULT '',"
                " namespace TEXT, workload TEXT, object_uid TEXT DEFAULT '',"
                " object_kind TEXT, reason TEXT, message TEXT, severity TEXT,"
                " occurrences INTEGER, notified INTEGER, created_at TIMESTAMP)"
            )
        conn.close()
        return path

    def test_an_absent_database_is_named_in_the_report_body(self):
        missing = os.path.join(self.tmp.name, "nope.db")

        report, problems = self._render(missing)

        self.assertEqual(len(problems), 1)
        self.assertIn(missing, report)
        self.assertIn("could not read the event ledger", report)
        self.assertNotIn("Nothing was held back from chat in this window", report)
        self.assertTrue(report.startswith("🔴"), report.splitlines()[0])
        # The zeroes are the absence of a measurement, not a measurement of
        # zero; printed bare they read as a quiet day.
        self.assertNotIn("Events Forwarded", report)
        self.assertNotIn("📉", report)

    def test_a_missing_table_is_named_in_the_report_body(self):
        """The rollback path: the volume predates the ledger, so the DB is there."""
        path = os.path.join(self.tmp.name, "session_kv.db")
        sqlite3.connect(path).close()

        report, problems = self._render(path)

        self.assertEqual(len(problems), 1)
        self.assertIn("intercepted_events", problems[0])
        self.assertIn(path, report)
        self.assertNotIn("Nothing was held back from chat in this window", report)
        self.assertTrue(report.startswith("🔴"), report.splitlines()[0])

    def test_a_missing_table_is_not_also_called_a_missing_database(self):
        """Two different faults pointing at two different things.

        Nothing on any path means the volume is not mounted; a database that
        was found and would not read means the schema is behind. Reporting both
        sends whoever is paged to check the wrong one.
        """
        path = os.path.join(self.tmp.name, "session_kv.db")
        sqlite3.connect(path).close()

        _, problems = self._render(path)

        self.assertNotIn("no session KV database found", self.err.getvalue())
        self.assertEqual([p for p in problems if "no session KV database" in p], [])

    def test_a_readable_path_discards_an_earlier_failure(self):
        """A fault another candidate made up for is not the recap's business."""
        broken = os.path.join(self.tmp.name, "broken.db")
        sqlite3.connect(broken).close()
        good = self._empty_ledger()

        with mock.patch.object(
            eod_report_generator, "default_db_paths", lambda: [broken, good]
        ):
            problems = []
            load_intercepted_events(window_hours=24, problems=problems)

        self.assertEqual(problems, [])

    def test_a_genuinely_quiet_day_is_still_green(self):
        """The negative control: the all-clear has to survive, or it means nothing."""
        report, problems = self._render(self._empty_ledger())

        self.assertEqual(problems, [])
        self.assertTrue(report.startswith("🟢"), report.splitlines()[0])
        self.assertIn("Nothing was held back from chat in this window", report)
        self.assertIn("*0 informational events* held back from chat in this window", report)
        self.assertNotIn("could not read the event ledger", report)


class TestAnUndeliveredAlertIsNotADeliveredOne(unittest.TestCase):
    """`notified` is written before the post is attempted, so it can be wrong.

    The session server writes the ledger row and only then hands the send to a
    background task; when that send fails it corrects the row with
    `mark_delivery_failed`. These tests are the recap's half of that contract:
    the corrected row must never be counted as an alert the on-call has already
    read, and — since the recap does not report undelivered alerts — must still
    stop the recap calling the day clean.
    """

    def undelivered(self, **overrides):
        row = event(notified=True, delivery_error="no message id from 'google_chat'")
        row.update(overrides)
        return row

    def test_an_undelivered_alert_is_not_counted_as_one_that_went_to_chat(self):
        """The false-reassurance case, stated at the counter that produces it.

        `alerts_posted` feeds "*N alerts* went to chat as it happened and are
        not repeated here", and under the Info-only default it is also the
        reason the workload is left out of the body. Counting an undelivered
        alert there tells the reader to go and find a message that was never
        sent.
        """
        summary = filter_and_aggregate_events([self.undelivered()])

        self.assertEqual(summary["alerts_posted"], 0)
        self.assertEqual(summary["delivery_failed"], 1)
        # Not the ceiling: that is the fleet's own policy working as intended,
        # and pointing the reader at ALERT_DAILY_LIMITS would send them to tune
        # a limit that is not the fault.
        self.assertEqual(summary["cap_dropped"], 0)
        # Not informational either, which is the bucket that would hide it.
        self.assertEqual(summary["suppressed_info"], 0)

    def test_the_undelivered_alert_is_not_named_either(self):
        """The accepted gap, pinned so it cannot be reintroduced by accident.

        An undelivered Critical is the one case where nothing else records the
        event at all — the alert is not in chat and no metric counts it, so the
        `delivery_error` column is the only trace. This recap still does not
        report it: its subject is informational events. The decision is
        deliberate and the gap is real; SOP, "What this recap does not report",
        is where it is written down.
        """
        report = generate_markdown_report(
            filter_and_aggregate_events([self.undelivered()]),
            cluster_name="test-cluster",
        )

        self.assertNotIn("Alerts That Never Reached Chat", report)
        self.assertNotIn("payment-api", report)
        self.assertNotIn("no message id from 'google_chat'", report)
        self.assertIn("*Alerts Raised:* 0", report)

    def test_a_failed_delivery_still_withholds_the_all_clear(self):
        """Not reported is not the same as did not happen.

        A report that ends "nothing was held back from chat in this window"
        over a day whose alerts never arrived is not silence about the failure,
        it is a denial of it — and 🟢 says the same thing in one character.
        """
        report = generate_markdown_report(
            filter_and_aggregate_events([self.undelivered()]),
            cluster_name="test-cluster",
        )

        self.assertNotIn("Nothing was held back from chat in this window", report)
        self.assertNotIn("🟢", report)
        self.assertIn("📊", report)

    def test_a_delivered_alert_is_unaffected(self):
        """The control. Without it every assertion above passes on a no-op."""
        summary = filter_and_aggregate_events([event(notified=True)])

        self.assertEqual(summary["alerts_posted"], 1)
        self.assertEqual(summary["delivery_failed"], 0)
        report = generate_markdown_report(summary, cluster_name="test-cluster")
        self.assertNotIn("Alerts That Never Reached Chat", report)
        self.assertIn("*Alerts Raised:* 1", report)
        # Still a green day: one delivered alert is the system working.
        self.assertIn("Nothing was held back from chat in this window", report)

    def test_a_ledger_without_the_column_still_reads(self):
        """A session server predating the write-back writes no such column.

        Naming it unconditionally in the SELECT would cost the whole day's
        ledger for nothing: the INSERT does not name `delivery_error`, so this
        shape is still recording events correctly and only the write-back
        fails. `cluster` is the opposite case and is a read failure — see
        `test_a_table_without_the_cluster_column_is_a_read_failure`.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "session_kv.db")
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE intercepted_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    cluster     TEXT NOT NULL DEFAULT '',
                    namespace   TEXT NOT NULL DEFAULT '',
                    workload    TEXT NOT NULL DEFAULT '',
                    object_uid  TEXT NOT NULL DEFAULT '',
                    object_kind TEXT NOT NULL DEFAULT '',
                    reason      TEXT NOT NULL DEFAULT '',
                    message     TEXT NOT NULL DEFAULT '',
                    severity    TEXT NOT NULL DEFAULT '',
                    occurrences INTEGER NOT NULL DEFAULT 1,
                    notified    INTEGER NOT NULL DEFAULT 0,
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                "INSERT INTO intercepted_events "
                "(namespace, workload, reason, severity, notified, created_at) "
                "VALUES ('prod', 'api', 'OOMKilled', 'Critical', 1, datetime('now'))"
            )
            conn.commit()
            conn.close()

            rows = load_intercepted_events(path, window_hours=24)

        self.assertEqual(len(rows), 1)
        # Absent reads as "no failure recorded", which is the truthful answer
        # for a row whose delivery nobody ever checked.
        self.assertEqual(rows[0]["delivery_error"], "")
        self.assertTrue(rows[0]["notified"])


class TestTheHeaderGradesTheDayAndNotTheListing(unittest.TestCase):
    """The header must carry the worst thing the ledger saw, busy day or not.

    The listing is fixed to Info, so a single informational event fills
    `entries` and almost every real day takes the listing arm. A header graded
    only on the quiet arm therefore opens 📊 on precisely the days that have
    something wrong.

    The recap does not report withheld or undelivered alerts, so the header has
    no colour left to signal them with — but 🟢 is an assertion that the day was
    clean, and it must not be spent on a day that was not. Those days fall to
    📊: neutral, which is honest, where green would not be.
    """

    def setUp(self):
        # The informational event that makes the day a listing day.
        self.routine = event(
            workload="log-shipper", reason="BackOff", severity="Info", notified=False
        )

    def header(self, events, expect_listing=True):
        summary, report = recap(events)
        if expect_listing:
            self.assertTrue(summary["entries"], "the day must take the listing arm")
        return report.splitlines()[0]

    def test_a_busy_day_with_an_undelivered_alert_is_not_green(self):
        undelivered = event(notified=True, delivery_error="no message id from 'google_chat'")

        self.assertTrue(self.header([self.routine, undelivered]).startswith("📊"))

    def test_a_busy_day_with_nothing_wrong_still_opens_with_the_chart(self):
        """The control. Without it every assertion here passes on a no-op."""
        self.assertTrue(self.header([self.routine]).startswith("📊"))

    def test_a_quiet_day_hiding_withheld_alerts_is_not_green(self):
        """The bug this gating exists to close.

        No informational events, so nothing to list and nothing to say — and 30
        Criticals the ceiling ate. Graded on `entry_count` alone the report
        opened 🟢 over them.
        """
        header = self.header(
            [event(notified=False) for _ in range(30)], expect_listing=False
        )

        self.assertTrue(header.startswith("📊"))

    def test_a_genuinely_empty_day_is_the_only_green_one(self):
        """The control for the control: 🟢 has to still be reachable."""
        self.assertTrue(self.header([], expect_listing=False).startswith("🟢"))

    def test_an_unreadable_ledger_outranks_everything(self):
        _, report = recap(
            [self.routine],
            problems=["`/nope/session_kv.db` — no session KV database found"],
        )

        self.assertTrue(report.splitlines()[0].startswith("🔴"))


class TestTheTalliesCountWhatTheyClaimToCount(unittest.TestCase):
    """The two alert tallies count rows, not the groups those rows landed in.

    Both are printed as a number of alerts and both veto the all-clear, so a
    group total standing in for either would deny a day over events that
    reached chat.
    """

    def test_the_undelivered_tally_counts_only_the_undelivered_rows(self):
        # One failed post among nine that arrived. Same workload, reason and
        # severity, so the ledger rows land in one group.
        events = [event(notified=True) for _ in range(9)]
        events.append(event(notified=True, delivery_error="no message id from 'google_chat'"))

        summary = filter_and_aggregate_events(events)

        self.assertEqual(summary["delivery_failed"], 1)

    def test_the_withheld_tally_counts_only_the_withheld_rows(self):
        """One withheld alert among nine that reached chat is one, not ten."""
        events = [event(notified=True) for _ in range(9)]
        events.append(event(notified=False))

        summary = filter_and_aggregate_events(events)

        self.assertEqual(summary["cap_dropped"], 1)
        self.assertEqual(summary["alerts_posted"], 9)


class TestTheListingIsFixedToInfo(unittest.TestCase):
    """Nothing widens the listing: it is a constant, not a knob.

    The recap's whole subject is the events chat was not told about, and those
    are the informational ones. A Critical or a Warning either reached chat or
    was withheld by the alert ceiling, and both of those are somebody else's
    report.
    """

    def test_the_listing_is_info_and_nothing_else(self):
        self.assertEqual(set(LISTED_SEVERITIES), {"Info"})

    def test_only_the_informational_event_is_listed(self):
        events = [
            event(reason="OOMKilled", severity="Critical", notified=True),
            event(reason="Unhealthy", severity="Warning", notified=True),
            event(reason="BackOff", severity="Info", notified=False),
        ]

        summary = filter_and_aggregate_events(events)

        self.assertEqual([e["reason"] for e in summary["entries"]], ["BackOff"])


class TestTheNamespaceFilterDoesNotReachTheAlertVeto(unittest.TestCase):
    """`EOD_EXCLUDE_NAMESPACES` is a noise filter, and stops where the noise does.

    `kube-system` ships excluded and the watcher forwards it anyway. The recap
    does not report withheld or undelivered alerts from anywhere — but it must
    still count them, from every namespace, because that count is what stops it
    printing an all-clear. A `continue` on the excluded row would let a
    control-plane delivery failure end the day green.

    The informational tally is the deliberate exception, and the class named
    below pins what pays for it. Counting excluded `Info` churn in the veto
    would leave every stock install permanently out of all-clear on `kube-system`
    `BackOff` noise, which is the one thing the filter exists to stop reporting.
    """

    report = staticmethod(recap)

    def test_an_undelivered_alert_in_an_excluded_namespace_still_vetoes(self):
        summary, report = self.report(
            [event(namespace="kube-system", workload="kube-apiserver",
                   delivery_error="no message id from 'google_chat'")]
        )

        self.assertEqual(summary["delivery_failed"], 1)
        self.assertNotIn("Nothing was held back from chat in this window", report)
        self.assertNotIn("🟢", report)
        # Not named, per this recap's scope — the veto is the whole effect.
        self.assertNotIn("kube-apiserver", report)

    def test_a_withheld_alert_in_an_excluded_namespace_still_vetoes(self):
        summary, report = self.report(
            [event(namespace="kube-system", workload="kube-apiserver", notified=False)]
        )

        self.assertEqual(summary["cap_dropped"], 1)
        self.assertNotIn("Nothing was held back from chat in this window", report)
        self.assertNotIn("🟢", report)
        self.assertNotIn("kube-apiserver", report)

    def test_an_excluded_alert_that_reached_chat_is_not_counted_as_withheld(self):
        """Excluding a namespace drops the row from the counts, not into another one.

        Folding `not excluded` into the `notified` branch instead of nesting it
        lets a delivered alert fall through to the `else` and be counted as one
        the daily ceiling ate — which would then veto a legitimate all-clear.
        """
        summary, report = self.report(
            [event(namespace="kube-system", workload="kube-apiserver", notified=True)]
        )

        self.assertEqual(summary["cap_dropped"], 0)
        self.assertEqual(summary["alerts_posted"], 0)
        self.assertIn("Nothing was held back from chat in this window", report)

    def test_informational_churn_in_an_excluded_namespace_stays_excluded(self):
        """The filter's actual job, and the reason it exists."""
        events = [
            event(namespace="kube-system", workload="kube-proxy", severity="Info",
                  notified=False)
            for _ in range(50)
        ]
        summary, report = self.report(events)

        self.assertEqual(summary["suppressed_info"], 0)
        self.assertEqual(summary["entries"], [])
        self.assertEqual(summary["unique_incidents"], 0)
        self.assertEqual(listing_of(report), [])
        self.assertNotIn("kube-proxy", report)

    def test_the_closing_lines_admit_the_filter_narrowed_them(self):
        """A count over part of the fleet must not read as a count over all of it."""
        events = [
            event(namespace="kube-system", workload="kube-proxy", severity="Info",
                  notified=False)
        ]
        _, excluded = self.report(events)
        _, clean = self.report([event(severity="Info", notified=False)])

        self.assertIn("outside this recap's scope", excluded)
        self.assertNotIn("outside this recap's scope", clean)

    def test_an_all_clear_over_excluded_churn_says_what_it_did_not_look_at(self):
        events = [
            event(namespace="kube-system", workload="kube-proxy", severity="Info",
                  notified=False)
        ]
        _, report = self.report(events)

        self.assertIn("Nothing was held back from chat in this window.", report)
        self.assertIn("outside this recap's scope", report)


class TestTheCountsSayWhatTheyCover(unittest.TestCase):
    """The numbers are summed over every severity. The listing under them is not.

    `LISTED_SEVERITIES` holds `Info` alone while `total_occurrences` and
    `unique_incidents` count every row in scope, so a day of ceiling-dropped
    Criticals reports events and groups no listed line accounts for. A
    delivered alert is already explained in words — "*N alerts* went to chat",
    "*Alerts Raised:* N" — which leaves the ceiling drop and the failed
    delivery as the residue nothing accounts for, and those two are the gate.

    Both qualifications share one line, printed once. Appended to each claim
    instead, the namespace note reached the headline, the ✅ and the 📉 of one
    small card, and a note printed three times is read none.
    """

    report = staticmethod(recap)

    SEVERITY_NOTE = "Counts cover every severity; only informational events are listed."
    SCOPE_NOTE = "Namespaces in `EOD_EXCLUDE_NAMESPACES` are outside this recap's scope."

    def test_a_ceiling_drop_in_the_counts_is_declared(self):
        """The headline counts a Critical the listing cannot show."""
        summary, report = self.report([listed(), event(notified=False)])

        self.assertEqual(summary["cap_dropped"], 1)
        self.assertEqual(summary["total_occurrences"], 2)
        self.assertEqual(len(listing_of(report)), 1)
        self.assertIn(self.SEVERITY_NOTE, report)

    def test_a_day_of_ceiling_drops_alone_still_declares_it(self):
        """The card with no listing at all — three bullets are the whole of it.

        30 events across 30 groups over a listing naming none of them is the
        shape the note exists for, and the one card where the reader has
        nothing else to reconcile the numbers against.
        """
        summary, report = self.report(
            [event(workload=f"svc-{i}", notified=False) for i in range(30)]
        )

        self.assertEqual(summary["cap_dropped"], 30)
        self.assertEqual(listing_of(report), [])
        self.assertIn("• *Events Forwarded:* 30", report)
        self.assertIn(self.SEVERITY_NOTE, report)

    def test_an_undelivered_alert_is_the_other_trigger(self):
        summary, report = self.report([event(delivery_error="chat 503")])

        self.assertEqual(summary["delivery_failed"], 1)
        self.assertIn(self.SEVERITY_NOTE, report)

    def test_an_informational_day_does_not_qualify_what_needs_no_qualifying(self):
        """The control. Every counted row is a listed row, so the numbers reconcile."""
        _, report = self.report([listed(), listed(workload="web")])

        self.assertNotIn(self.SEVERITY_NOTE, report)

    def test_a_delivered_alert_is_explained_by_the_sentence_it_is_already_in(self):
        """The second control, and the reason the gate is not `severity != Info`.

        A Critical that reached chat is counted in the headline and absent from
        the listing, exactly like a cap-dropped one — but "*1 alert* went to
        chat as it happened and is not repeated here" has already said so, and
        a second sentence saying it again is the repetition this line replaced.
        """
        _, report = self.report([listed(), event()])

        self.assertIn("*1 alert* went to chat as it happened", report)
        self.assertNotIn(self.SEVERITY_NOTE, report)

    def test_the_namespace_note_is_printed_once_and_not_three_times(self):
        """A kube-system-only day carries the bullet counts, the ✅ and the 📉.

        The note used to be appended to all three. One card, one statement of
        what it did not read.
        """
        _, report = self.report(
            [event(namespace="kube-system", workload="kube-proxy", severity="Info",
                   notified=False)]
        )

        self.assertEqual(report.count(self.SCOPE_NOTE), 1)
        # Still on the card, and still above both lines that used to carry it.
        self.assertIn("Nothing was held back from chat in this window.", report)
        self.assertIn("📉", report)
        self.assertLess(report.index(self.SCOPE_NOTE), report.index("✅"))

    def test_both_qualifications_share_one_line(self):
        """A day needing each of them prints two sentences, not two lines."""
        _, report = self.report([
            listed(),
            event(notified=False),
            event(namespace="kube-system", workload="kube-proxy", notified=False),
        ])

        self.assertEqual(report.count(self.SEVERITY_NOTE), 1)
        self.assertEqual(report.count(self.SCOPE_NOTE), 1)
        self.assertIn(f"_{self.SEVERITY_NOTE} {self.SCOPE_NOTE}_", report)


class TestAnExclusionDoesNotBarTheGreenHeader(unittest.TestCase):
    """A veto that fires every day is not a veto.

    Vetoing green on `excluded_occurrences` reads defensible in isolation: the
    recap did not look in the excluded namespaces, so green overclaims by a
    little. It does not survive the deployment. `kube-system` ships in
    `DEFAULT_EXCLUDE_NAMESPACES` and the watcher runs with no namespace filter
    of its own, so on any real cluster that count is non-zero every day of the
    year — the header would be pinned to 📊 permanently, 🟢 unreachable outside
    an empty ledger, and 📊 itself would stop marking the days it exists to
    mark. Three-state grading that can only emit one state grades nothing.

    What carries the caveat instead is the ✅, in words, one line below. That
    was always the better place for it, and it was the argument for the veto in
    the first place.

    The alert tallies are unfiltered and still veto, so this changes nothing
    about a day a ceiling drop or a failed delivery spoiled: those were never
    green.
    """

    report = staticmethod(recap)

    def excluded_churn(self):
        return [
            event(namespace="kube-system", workload="kube-proxy", severity="Info",
                  notified=False)
        ]

    def test_a_day_of_excluded_churn_is_still_green(self):
        _, report = self.report(self.excluded_churn())

        self.assertIn("🟢", report)
        self.assertNotIn("📊", report)
        # Green, and the sentence beside it still says what "in scope" left out.
        self.assertIn("Nothing was held back from chat in this window.", report)
        self.assertIn("outside this recap's scope", report)

    def test_a_ceiling_drop_still_bars_green(self):
        """The control that keeps the veto from being deleted wholesale.

        `excluded_occurrences` stopped vetoing; `cap_dropped` did not. Without
        this the change above is indistinguishable from removing the gate.
        """
        summary, report = self.report(
            [event(namespace="kube-system", workload="kube-proxy", notified=False)]
        )

        self.assertEqual(summary["cap_dropped"], 1)
        self.assertNotIn("🟢", report)
        self.assertIn("📊", report)

    def test_in_scope_churn_is_neutral_not_green(self):
        """The other control: green tracks `all_clear`, not the exclusion list."""
        with exclude(""):
            _, report = self.report(self.excluded_churn())

        # Nothing excluded now, so the same rows are ordinary listed churn.
        self.assertNotIn("🟢", report)
        self.assertIn("📊", report)

        _, empty = self.report([])
        self.assertIn("🟢", empty)
        self.assertIn("Nothing was held back from chat in this window.", empty)
        self.assertNotIn("outside this recap's scope", empty)

    def test_an_excluded_delivered_alert_does_not_bar_green(self):
        """A delivered alert in an excluded namespace spoils nothing.

        It is not withheld, so the ✅ is correct, and after this change the
        header agrees with it instead of contradicting it.
        """
        summary, report = self.report(
            [event(namespace="kube-system", workload="kube-apiserver", notified=True)]
        )

        self.assertEqual(summary["cap_dropped"], 0)
        self.assertIn("🟢", report)
        self.assertIn("Nothing was held back from chat in this window.", report)



class TestTheHeaderSaysWhichClustersItCounted(unittest.TestCase):
    """The counts are fleet-wide; the header names one cluster.

    `start-services.sh` starts the watcher with `--profiles-dir`, so every
    Cluster Agent profile in the pod fans into one ledger, and the reader
    deliberately does not scope its query by cluster — filtering to the host
    would throw most of the fleet's events away. What that leaves is a header
    reading "— `platform-agent-host`" over totals that are nothing of the sort,
    and an on-call who reads three OOMKills as three on the cluster named.
    """

    report = staticmethod(recap)

    def test_a_foreign_cluster_is_named_above_the_counts(self):
        _, report = self.report(
            [listed(cluster="test-cluster"), listed(cluster="cluster-b")]
        )

        header = report.splitlines()[0]
        self.assertIn("`test-cluster` +1 cluster", header)
        self.assertIn("every count below covers `cluster-b`", report)

    def test_a_single_cluster_recap_is_unchanged(self):
        """The control. The ordinary install watches one cluster and says so once."""
        _, report = self.report([listed(cluster="test-cluster")])

        self.assertEqual(
            report.splitlines()[0].split("— ")[1],
            "`test-cluster` (2026-08-14)",
        )
        self.assertNotIn("every count below covers", report)

    def test_rows_with_no_cluster_belong_to_the_host(self):
        """A ledger row written before the column had a value is not a fan-in.

        `_workload_label` already reads an empty cluster as this report's own,
        so counting it as a second cluster would put a scope line on every
        single-cluster recap that ever saw one.
        """
        _, report = self.report([listed(cluster=""), listed(workload="other")])

        self.assertNotIn("every count below covers", report)
        self.assertNotIn("+1 cluster", report.splitlines()[0])

    def test_the_scope_line_counts_the_clusters_it_does_not_name(self):
        """A fan-in install can watch more clusters than a chat card can carry."""
        _, report = self.report(
            [listed(cluster=f"cluster-{i}", workload=f"w{i}") for i in range(8)]
        )

        header = report.splitlines()[0]
        self.assertIn("`test-cluster` +8 clusters", header)
        # Five named, three counted — and nothing dropped silently.
        self.assertIn("and 3 others", report)
        self.assertIn("`cluster-4`", report)
        self.assertNotIn("`cluster-5`", report)

    def test_an_excluded_namespace_is_still_a_cluster_the_recap_read(self):
        """The scope line reports where it looked, not where it found noise.

        Keyed off the counted rows instead, a fan-in whose only foreign traffic
        was `kube-system` churn would report fleet-wide totals under one
        cluster's name again.
        """
        _, report = self.report(
            [listed(), listed(cluster="cluster-b", namespace="kube-system")]
        )

        self.assertIn("every count below covers `cluster-b`", report)


class TestTheListingHoldsOnlyRowsHeldBackFromChat(unittest.TestCase):
    """The heading, the headline and the closing total describe one set of rows.

    Selecting the listing on severity alone let an Info row that *did* reach
    chat into it, while `suppressed_info` — the 📉 total — counted only the
    rows that did not. One set of rows was then announced as "not repeated
    here", repeated here under a heading saying they never arrived, and
    totalled as zero. Only a session server predating the Info gate writes such
    a row, which is what made it easy to miss.
    """

    report = staticmethod(recap)

    def test_an_informational_row_that_reached_chat_is_not_listed(self):
        summary, report = self.report([event(severity="Info", notified=True) for _ in range(3)])

        self.assertEqual(summary["entries"], [])
        self.assertEqual(summary["suppressed_info"], 0)
        self.assertEqual(summary["alerts_posted"], 3)
        self.assertEqual(listing_of(report), [])
        self.assertIn("*0 informational events* held back", report)

    def test_the_listing_and_the_closing_total_report_the_same_rows(self):
        """The mixed group: one delivered and one withheld under one key.

        `notified` is part of the group key, so the two do not merge. Without
        that the pair is one group and the listing prints `2 events` under a
        heading that says none of them arrived, while the total says 1.
        """
        summary, report = self.report(
            [event(severity="Info", notified=True), event(severity="Info", notified=False)]
        )

        self.assertEqual(len(summary["entries"]), 1)
        self.assertEqual(summary["entries"][0]["count"], summary["suppressed_info"])
        self.assertEqual([e["count"] for e in listing_of(report)], [1])
        self.assertIn("*1 informational event* held back", report)

    def test_the_total_is_in_the_same_unit_the_listing_prints(self):
        """Occurrences, not rows.

        The invariant above holds trivially while every row carries
        `occurrences = 1`, which is all production writes: the watcher's
        payload count comes from `Observe`'s new-incident branch and a
        duplicate never reaches the dispatch. Counted per row, a ledger whose
        rows stood for several sightings each would print `7 events` and
        `3 events` in the listing and total them as 2 — one card, two units.
        """
        summary, report = self.report([
            listed(workload="api", occurrences=7),
            listed(workload="web", occurrences=3),
        ])

        self.assertEqual([e["count"] for e in listing_of(report)], [7, 3])
        self.assertEqual(summary["suppressed_info"], 10)
        self.assertEqual(summary["suppressed_info"], sum(e["count"] for e in summary["entries"]))
        self.assertIn("*10 informational events* held back", report)

    def test_the_ordinary_day_is_unchanged(self):
        """The control. Every row this version writes is notified=0."""
        summary, report = self.report([event(severity="Info", notified=False) for _ in range(3)])

        self.assertEqual(len(summary["entries"]), 1)
        self.assertEqual(summary["entries"][0]["count"], 3)
        self.assertEqual([e["count"] for e in listing_of(report)], [3])
        self.assertIn("*3 informational events* held back", report)


class TestTheNamespaceFilterReadsItsValue(unittest.TestCase):
    """Unset, empty and padded are three different answers, not one.

    `excluded_namespaces()` is a `getenv`, a `split(",")` and a `strip()`, so no
    value can raise and no typo can cost the fleet its recap. The hazard these
    pin is the quieter one: absent takes the three system defaults, while
    set-but-empty excludes nothing, so clearing the variable widens the recap
    instead of restoring the default.
    """

    def test_the_defaults_are_the_three_system_namespaces(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                eod_report_generator.excluded_namespaces(),
                frozenset({"kube-system", "kube-public", "kube-node-lease"}),
            )

    def test_an_empty_exclusion_list_excludes_nothing(self):
        """`""` means "exclude nothing", which is not the same as saying nothing."""
        with exclude(""):
            self.assertEqual(eod_report_generator.excluded_namespaces(), frozenset())

    def test_whitespace_around_a_namespace_is_ignored(self):
        with mock.patch.dict(os.environ, {"EOD_EXCLUDE_NAMESPACES": " prod , , staging "}):
            self.assertEqual(
                eod_report_generator.excluded_namespaces(), frozenset({"prod", "staging"})
            )


if __name__ == "__main__":
    unittest.main()
