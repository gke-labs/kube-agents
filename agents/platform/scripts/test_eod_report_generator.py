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

import datetime
import importlib
import io
import os
import pathlib
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
    load_config,
    load_intercepted_events,
)





def event(**overrides):
    """One ledger row, with the fields a test does not care about filled in.

    `occurrences` is 1 because that is the only value production writes: the
    watcher drops duplicates before /inject and the payload's `count` comes
    from the dedupNewIncident branch, which hardcodes 1. Tests that assert on
    richer values were asserting a shape the system cannot produce.
    """
    row = {
        "namespace": "prod-api",
        "workload": "payment-api",
        "object_kind": "Pod",
        "reason": "OOMKilled",
        "message": "Memory cgroup out of memory",
        "severity": "Critical",
        "occurrences": 1,
        "notified": True,
        "created_at": "2026-08-10 12:00:00",
    }
    row.update(overrides)
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

    def setUp(self):
        # Mirrors the shipped eod_report_config.yaml, which carries no severity
        # key: the listing is fixed to Info in `LISTED_SEVERITIES` and no config
        # can widen it. A test about grouping or formatting therefore seeds
        # `listed()` rows rather than reaching for a config that would let
        # `event()`'s default Critical through.
        self.config = {
            "version": "v1",
            "filters": {
                "min_event_count": 1,
                "exclude_namespaces": ["kube-system"],
            },
            "sections": {
                "telemetry_summary": True,
                "workload_breakdown": True,
                "suppressed_summary": True,
            },
        }
    def sectioned(self, **sections):
        """`self.config` with section toggles overridden. Severity is not overridable."""
        return {**self.config, "sections": {**self.config["sections"], **sections}}


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
        summary = filter_and_aggregate_events(events, self.config)

        self.assertEqual(summary["alerts_posted"], 1)
        self.assertEqual(summary["suppressed_info"], 1)
        self.assertEqual(summary["total_occurrences"], 2)
        # Both counted in the telemetry; only the suppressed one is listed.
        self.assertEqual(summary["unique_incidents"], 2)
        self.assertEqual([e["reason"] for e in summary["entries"]], ["BackOff"])

        report = generate_markdown_report(summary, self.config)
        self.assertIn("BackOff", report)
        self.assertNotIn("OOMKilled", report)
        # The alert is still accounted for as a number, so a digest of nothing
        # but routine cannot be mistaken for a fleet that had a quiet day.
        self.assertIn("*1 alert* went to chat as it happened", report)
        self.assertIn("*1 informational event* held back from chat today", report)

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
        summary = filter_and_aggregate_events(events, self.config)

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
        summary = filter_and_aggregate_events(events, self.config)

        self.assertEqual(
            sorted(e["workload"] for e in summary["entries"]),
            ["noisy-0", "noisy-1", "noisy-2"],
        )
        report = generate_markdown_report(summary, self.config)
        self.assertIn("noisy-0", report)
        self.assertNotIn("payment-api", report)
        self.assertNotIn("checkout", report)

    def test_the_entry_list_is_cut_at_ten_and_says_how_many_it_dropped(self):
        """Info churn is high-cardinality, so silent truncation loses most of it."""
        events = [
            event(workload=f"svc-{i:02d}", reason="BackOff", severity="Info", notified=False)
            for i in range(14)
        ]
        summary = filter_and_aggregate_events(events, self.config)
        report = generate_markdown_report(summary, self.config)

        self.assertEqual(len(summary["entries"]), 14)
        self.assertEqual(report.count("`BackOff`"), 10)
        self.assertIn("…and 4 further groups not listed.", report)


    def test_an_all_suppressed_day_is_the_populated_report_now(self):
        """Nine informational events used to be a blank recap; they are its body."""
        summary = filter_and_aggregate_events(
            [event(severity="Info", notified=False) for _ in range(9)], self.config
        )
        report = generate_markdown_report(summary, self.config)

        self.assertEqual(len(summary["entries"]), 1)
        self.assertEqual(summary["entries"][0]["count"], 9)
        self.assertIn("Informational Events Held Back from Chat", report)
        self.assertIn("*0 alerts* went to chat", report)
        self.assertIn("*9 informational events* held back from chat today", report)

    def test_the_held_back_total_survives_a_day_with_nothing_listable(self):
        """Zero listed groups must not mean zero accounting for what was held back."""
        summary = filter_and_aggregate_events(
            [event(severity="Info", notified=False)], {
                **self.config,
                "filters": {**self.config["filters"], "min_event_count": 999},
            }
        )
        report = generate_markdown_report(summary, self.config)
        self.assertIn("*1 informational event* held back from chat today", report)
        # And the all-clear must not appear two lines above that total, which
        # is the contradiction the `suppressed` term in its condition prevents.
        self.assertNotIn("Nothing was held back from chat in this window", report)

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
        summary = filter_and_aggregate_events(events, self.config)

        self.assertEqual(summary["alerts_posted"], 10)
        self.assertEqual(summary["cap_dropped"], 30)
        self.assertEqual(summary["suppressed_info"], 0)

        report = generate_markdown_report(
            summary, self.config, cluster_name="test-cluster"
        )
        self.assertIn("*0 informational events* held back from chat today", report)

    def test_a_withheld_alert_is_reported_nowhere(self):
        """This recap's subject is informational events, and only those.

        A ceiling-withheld alert is graded Critical or Warning. It is not named,
        not counted and not alluded to: the quota counter and `GET
        /v1/alert-quota` are where that signal lives. SOP: "What this recap does
        not report".
        """
        events = [
            event(workload="checkout", reason="CrashLoopBackOff", notified=False)
            for _ in range(12)
        ]
        summary = filter_and_aggregate_events(events, self.config)
        report = generate_markdown_report(
            summary, self.config, cluster_name="test-cluster"
        )

        self.assertEqual(summary["cap_dropped"], 12)
        self.assertNotIn("checkout", report)
        self.assertNotIn("CrashLoopBackOff", report)
        self.assertNotIn("withheld", report)
        self.assertNotIn("ceiling", report)

    def test_not_reporting_a_withheld_alert_is_not_denying_it(self):
        """The regression this file exists to prevent, at its sharpest.

        Thirty Critical alerts the ceiling ate: chat never saw them and no triage
        session was opened. Staying silent about them is the choice; printing
        "nothing was held back" over them is a different thing, and printing a
        green header over them is the same lie in one character.
        """
        events = [event(notified=False) for _ in range(30)]
        summary = filter_and_aggregate_events(events, self.config)
        report = generate_markdown_report(
            summary, self.config, cluster_name="test-cluster"
        )

        self.assertEqual(summary["cap_dropped"], 30)
        self.assertNotIn("Nothing was held back from chat in this window", report)
        self.assertNotIn("🟢", report)
        self.assertIn("📊", report)

    def test_the_veto_survives_a_threshold_that_lists_nothing(self):
        """`min_event_count` filters the listing, and must not filter the veto.

        Gating the ✅ on `cap_dropped_entries` rather than the count would move
        the same denial behind the threshold.
        """
        config = {**self.config, "filters": {**self.config["filters"], "min_event_count": 5}}
        events = [event(notified=False) for _ in range(2)]
        summary = filter_and_aggregate_events(events, config)
        report = generate_markdown_report(
            summary, config, cluster_name="test-cluster"
        )

        self.assertEqual(summary["cap_dropped"], 2)
        self.assertEqual(summary["cap_dropped_entries"], [])
        self.assertNotIn("Nothing was held back from chat in this window", report)
        self.assertNotIn("🟢", report)

    def test_an_unreadable_ledger_is_not_a_quiet_day_either(self):
        """An unopened table measures nothing, so it cannot clear the day.

        `problems=` by keyword, because the second positional parameter is
        `incidents`: passed there, the list never reaches the guard and this
        read as a test of a branch it did not enter.
        """
        summary = filter_and_aggregate_events(
            [event(notified=False) for _ in range(3)], self.config
        )
        self.assertEqual(summary["cap_dropped"], 3)

        report = generate_markdown_report(
            summary,
            self.config,
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
        cfg = self.config
        events = [listed(cluster="cluster-a"), listed(cluster="cluster-b")]
        summary = filter_and_aggregate_events(events, cfg)

        self.assertEqual(summary["unique_incidents"], 2)
        self.assertEqual([e["count"] for e in summary["entries"]], [1, 1])

        # The report runs on one cluster but reports rows from several, so a
        # foreign cluster is named and the local one is left as it always was.
        report = generate_markdown_report(
            summary, cfg, cluster_name="cluster-a"
        )
        self.assertIn("cluster-b:prod-api/payment-api", report)
        self.assertNotIn("cluster-a:prod-api/payment-api", report)

    def test_excluded_namespace_leaves_the_headline_counts(self):
        """The summary must describe the same scope the breakdown does."""
        events = [event()] + [event(namespace="kube-system") for _ in range(500)]
        summary = filter_and_aggregate_events(events, self.config)

        self.assertEqual(summary["total_occurrences"], 1)
        self.assertEqual(summary["unique_incidents"], 1)
        self.assertEqual(summary["forwarded"], 1)

    def test_min_event_count_applies_to_the_grouped_total(self):
        """Twelve forwarded events for one workload clear a threshold of ten."""
        cfg = {**self.config, "filters": {**self.config["filters"], "min_event_count": 10}}
        events = [listed() for _ in range(12)]
        summary = filter_and_aggregate_events(events, cfg)

        self.assertEqual(len(summary["entries"]), 1)
        self.assertEqual(summary["entries"][0]["count"], 12)

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
        summary = filter_and_aggregate_events(events, self.config)

        self.assertEqual(
            sorted(e["workload"] for e in summary["entries"]),
            ["api-cache", "api-store"],
        )

    def test_rows_the_server_already_collapsed_group_together(self):
        events = [listed(workload="payment-api") for _ in range(2)]
        summary = filter_and_aggregate_events(events, self.config)

        self.assertEqual(len(summary["entries"]), 1)
        self.assertEqual(summary["entries"][0]["workload"], "payment-api")
        self.assertEqual(summary["entries"][0]["count"], 2)

    def test_report_uses_chat_markup_not_markdown(self):
        """stdout is delivered verbatim to Chat/Slack, which render neither."""
        summary = filter_and_aggregate_events(
            [event()], self.config
        )
        report = generate_markdown_report(summary, self.config)

        self.assertNotIn("**", report)
        self.assertNotIn("###", report)

    def test_telemetry_summary_toggle_is_honoured(self):
        cfg = {**self.config, "sections": {**self.config["sections"], "telemetry_summary": False}}
        summary = filter_and_aggregate_events([event()], cfg)

        self.assertNotIn("Forwarded", generate_markdown_report(summary, cfg))
        self.assertIn("Forwarded", generate_markdown_report(summary, self.config))

    def test_no_noise_reduction_claim_is_printed(self):
        """Every ledger row is one forwarded incident; there is no ratio to report."""
        summary = filter_and_aggregate_events([event() for _ in range(3)], self.config)
        report = generate_markdown_report(summary, self.config)

        self.assertNotIn("noise reduction", report)
        self.assertNotIn("dedup_ratio", summary)




class TestReportWindow(unittest.TestCase):
    """Monday has to reach back over a weekend the cron never ran on."""

    def test_monday_covers_the_weekend(self):
        # 2026-08-10 is a Monday.
        monday = datetime.datetime(2026, 8, 10, 17, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(eod_report_generator.default_window_hours(monday), 72)

    def test_other_weekdays_look_back_a_day(self):
        tuesday = datetime.datetime(2026, 8, 11, 17, 0, tzinfo=datetime.timezone.utc)
        self.assertEqual(eod_report_generator.default_window_hours(tuesday), 24)


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
                """
            )
            for ns, workload, reason, notified, age_hours in rows:
                conn.execute(
                    "INSERT INTO intercepted_events "
                    "(namespace, workload, object_kind, reason, message, severity, "
                    " occurrences, notified, created_at) "
                    "VALUES (?, ?, 'Pod', ?, 'msg', 'Warning', 2, ?, "
                    "        datetime('now', ?))",
                    (ns, workload, reason, notified, f"-{age_hours} hours"),
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

    def test_the_cluster_column_is_read_when_present_and_missed_gracefully(self):
        """`cluster` postdates the rest of the table, so both shapes are live.

        Naming it unconditionally in the SELECT turns a database written by an
        older session server into an OperationalError, and that path already
        gives up on the file — so a column added to improve the recap would have
        silently cost a day of it.
        """
        # The old shape, which _db builds: every row reads back unattributed
        # rather than the query failing.
        path = self._db([("prod", "api", "OOMKilled", 1, 2)])
        self.assertEqual(load_intercepted_events(path, window_hours=24)[0]["cluster"], "")

        os.remove(path)  # _db always writes session_kv.db into the same tmpdir
        path = self._db(
            [("prod", "api", "OOMKilled", 1, 2)],
            table_sql="""
            CREATE TABLE intercepted_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cluster     TEXT NOT NULL DEFAULT 'cluster-b',
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
        self.assertEqual(
            load_intercepted_events(path, window_hours=24)[0]["cluster"], "cluster-b"
        )

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
                " cluster TEXT DEFAULT '', namespace TEXT, workload TEXT, object_kind TEXT,"
                " reason TEXT, message TEXT, severity TEXT, occurrences INTEGER,"
                " notified INTEGER, created_at TIMESTAMP, delivery_error TEXT DEFAULT '')"
            )
            conn.execute(
                "INSERT INTO intercepted_events (namespace, workload, object_kind, reason,"
                " message, severity, occurrences, notified, created_at)"
                " VALUES ('prod', ?, 'Pod', 'OOMKilled', 'm', 'Critical', 1, 1, datetime('now'))",
                (workload,),
            )
            conn.execute(
                "CREATE TABLE incidents (chat_id TEXT, thread_id TEXT, report TEXT,"
                " created_at TIMESTAMP)"
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

    CONFIG = {"version": "v1", "filters": {}, "sections": {}}

    def _render(self, summary):
        return generate_markdown_report(
            summary, self.CONFIG, cluster_name="prod", report_date="2026-08-14"
        )

    def test_the_all_clear_does_not_assert_the_daemon_is_running(self):
        report = self._render(filter_and_aggregate_events([], self.CONFIG))
        self.assertIn("✅", report)
        self.assertNotIn("Watcher daemon active", report)
        self.assertNotIn("streaming GKE events", report)

    def test_it_says_what_it_actually_read_instead(self):
        report = self._render(filter_and_aggregate_events([], self.CONFIG))
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
        report = self._render(filter_and_aggregate_events(events, self.CONFIG))
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

    CONFIG = {"version": "v1", "filters": {}, "sections": {}}

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
        summary = filter_and_aggregate_events(events, self.CONFIG)
        return generate_markdown_report(
            summary,
            self.CONFIG,
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
                " id INTEGER PRIMARY KEY AUTOINCREMENT, namespace TEXT, workload TEXT,"
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
        self.assertNotIn("held back from chat today", report)

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
        self.assertIn("*0 informational events* held back from chat today", report)
        self.assertNotIn("could not read the event ledger", report)


class TestConfigResolution(unittest.TestCase):
    """The default search path must be where the image actually puts the file.

    `agents/platform/governance/` is copied to /opt/platform-template/governance/
    and scaffolded into $TARGET_DIR/profiles/platform by docker-entrypoint.sh, so
    the deployed file lands under $PLATFORM_AGENT_HOME/profiles/platform. A path
    that does not exist in the pod fails silently, which is the worst kind.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _reload_with_home(self, home):
        # HERMES_HOME is cleared, not left ambient: it takes precedence over
        # PLATFORM_AGENT_HOME and a value inherited from the developer's shell
        # would decide what these tests are actually measuring.
        with mock.patch.dict(os.environ, {"PLATFORM_AGENT_HOME": home}):
            os.environ.pop("HERMES_HOME", None)
            return importlib.reload(eod_report_generator)

    def _reload_with_hermes_home(self, home):
        with mock.patch.dict(os.environ, {"HERMES_HOME": home}):
            return importlib.reload(eod_report_generator)

    def test_profile_home_governance_is_searched_first(self):
        """The job ticks on the platform roster, so $HERMES_HOME is its profile home.

        `profile_cron_tick.py` sets it to the home it is ticking, which makes
        `$HERMES_HOME/governance` the scaffolded directory itself — and unlike
        the working directory, which the poller points at the credential
        proxy's workspace root, it is not something the pod's layout can move.
        """
        mod = self._reload_with_hermes_home(self.tmp.name)

        self.assertEqual(
            mod.DEFAULT_CONFIG_PATHS[0],
            os.path.join(self.tmp.name, "governance", "eod_report_config.yaml"),
        )
        # The absolute path stays reachable behind it: a hand-run has no
        # HERMES_HOME pointing here and must still find the same file.
        self.assertIn(
            "profiles/platform/governance/eod_report_config.yaml",
            "\n".join(mod.DEFAULT_CONFIG_PATHS[1:]),
        )

    @unittest.skipIf(eod_report_generator.yaml is None, "PyYAML is not installed")
    def test_profile_home_governance_is_loaded(self):
        gov = os.path.join(self.tmp.name, "governance")
        os.makedirs(gov)
        with open(os.path.join(gov, "eod_report_config.yaml"), "w") as fh:
            fh.write("filters:\n  exclude_namespaces:\n    - from-hermes-home\n")

        config = self._reload_with_hermes_home(self.tmp.name).load_config()

        self.assertEqual(config["filters"]["exclude_namespaces"], ["from-hermes-home"])

    def tearDown(self):
        # Leave the module as the rest of the suite imported it.
        importlib.reload(eod_report_generator)

    def test_scaffolded_governance_path_is_searched(self):
        gov = os.path.join(self.tmp.name, "profiles", "platform", "governance")
        os.makedirs(gov)
        with open(os.path.join(gov, "eod_report_config.yaml"), "w") as fh:
            fh.write("filters:\n  exclude_namespaces:\n    - only-this-one\n")

        mod = self._reload_with_home(self.tmp.name)
        config = mod.load_config()

        self.assertEqual(config["filters"]["exclude_namespaces"], ["only-this-one"])

    def test_a_missing_config_says_so_on_stderr(self):
        """Silence here reads as 'your settings applied'. It must not be silent."""
        mod = self._reload_with_home(os.path.join(self.tmp.name, "empty"))
        # Out of the checkout, so the repo-relative fallback cannot resolve
        # either — a cron tick's working directory is not the repository root.
        cwd = os.getcwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, cwd)

        err = io.StringIO()
        with mock.patch.object(mod.sys, "stderr", err):
            config = mod.load_config()

        self.assertIn("no eod_report_config.yaml found", err.getvalue())
        # stdout is the chat message for this no_agent job — warnings stay off it.
        self.assertEqual(config["filters"]["min_event_count"], 1)


class TestAnUndeliveredAlertIsNotADeliveredOne(unittest.TestCase):
    """`notified` is written before the post is attempted, so it can be wrong.

    The session server writes the ledger row and only then hands the send to a
    background task; when that send fails it corrects the row with
    `mark_delivery_failed`. These tests are the recap's half of that contract:
    the corrected row must never be counted as an alert the on-call has already
    read, and — since the recap does not report undelivered alerts — must still
    stop the recap calling the day clean.
    """

    def setUp(self):
        # The shipped config, Info-only.
        self.config = {
            "version": "v1",
            "filters": {
                "min_event_count": 1,
                "exclude_namespaces": ["kube-system"],
            },
            "sections": {
                "telemetry_summary": True,
                "workload_breakdown": True,
                "suppressed_summary": True,
            },
        }

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
        summary = filter_and_aggregate_events([self.undelivered()], self.config)

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
            filter_and_aggregate_events([self.undelivered()], self.config),
            self.config,
            cluster_name="test-cluster",
        )

        self.assertNotIn("Alerts That Never Reached Chat", report)
        self.assertNotIn("payment-api", report)
        self.assertNotIn("no message id from 'google_chat'", report)
        self.assertIn("*Alerts Raised:* 0", report)

    def test_a_failed_delivery_still_withholds_the_all_clear(self):
        """Not reported is not the same as did not happen.

        A report that ends "nothing was held back from chat today" over a day
        whose alerts never arrived is not silence about the failure, it is a
        denial of it — and 🟢 says the same thing in one character.
        """
        report = generate_markdown_report(
            filter_and_aggregate_events([self.undelivered()], self.config),
            self.config,
            cluster_name="test-cluster",
        )

        self.assertNotIn("Nothing was held back from chat in this window", report)
        self.assertNotIn("🟢", report)
        self.assertIn("📊", report)

    def test_the_undelivered_tally_ignores_the_noise_threshold(self):
        """`min_event_count` is a threshold on routine churn.

        One undelivered Critical is not churn. The aggregate keeps it whatever
        the threshold says, because it is what vetoes the all-clear.
        """
        config = {
            **self.config,
            "filters": {**self.config["filters"], "min_event_count": 5},
        }
        summary = filter_and_aggregate_events([self.undelivered()], config)

        self.assertEqual(summary["delivery_failed"], 1)
        self.assertEqual(len(summary["delivery_failed_entries"]), 1)
        report = generate_markdown_report(summary, config, cluster_name="test-cluster")
        self.assertNotIn("Nothing was held back from chat in this window", report)

    def test_a_delivered_alert_is_unaffected(self):
        """The control. Without it every assertion above passes on a no-op."""
        summary = filter_and_aggregate_events([event(notified=True)], self.config)

        self.assertEqual(summary["alerts_posted"], 1)
        self.assertEqual(summary["delivery_failed"], 0)
        self.assertEqual(summary["delivery_failed_entries"], [])
        report = generate_markdown_report(summary, self.config, cluster_name="test-cluster")
        self.assertNotIn("Alerts That Never Reached Chat", report)
        self.assertIn("*Alerts Raised:* 1", report)
        # Still a green day: one delivered alert is the system working.
        self.assertIn("Nothing was held back from chat in this window", report)

    def test_a_ledger_without_the_column_still_reads(self):
        """A session server predating the write-back writes no such column.

        Naming it unconditionally in the SELECT would cost the whole day's
        ledger, the same way `cluster` would have.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "session_kv.db")
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE intercepted_events (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
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

    Under the shipped Info-only config a single informational event fills
    `entries`, so almost every real day takes the listing arm. A header graded
    only on the quiet arm therefore opens 📊 on precisely the days that have
    something wrong.

    The recap does not report withheld or undelivered alerts, so the header has
    no colour left to signal them with — but 🟢 is an assertion that the day was
    clean, and it must not be spent on a day that was not. Those days fall to
    📊: neutral, which is honest, where green would not be.
    """

    def setUp(self):
        self.config = {
            "version": "v1",
            "filters": {
                "min_event_count": 1,
                "exclude_namespaces": ["kube-system"],
            },
            "sections": {
                "telemetry_summary": True,
                "workload_breakdown": True,
                "suppressed_summary": True,
            },
        }
        # The informational event that makes the day a listing day.
        self.routine = event(
            workload="log-shipper", reason="BackOff", severity="Info", notified=False
        )

    def header(self, events, config=None, expect_listing=True):
        cfg = config or self.config
        summary = filter_and_aggregate_events(events, cfg)
        report = generate_markdown_report(summary, cfg, cluster_name="test-cluster")
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
        summary = filter_and_aggregate_events([self.routine], self.config)
        report = generate_markdown_report(
            summary, self.config, cluster_name="test-cluster",
            problems=["`/nope/session_kv.db` — no session KV database found"],
        )

        self.assertTrue(report.splitlines()[0].startswith("🔴"))


class TestTheTalliesCountWhatTheyClaimToCount(unittest.TestCase):
    """A group's `count` is a sum over every row under the key.

    `cap_dropped` and `delivery_failed` are ORs over the same rows, so one
    withheld alert in a group of ten delivered ones marks the whole group. The
    recap no longer renders these, but the per-group counts are still what
    `min_event_count` is measured against, so a group total standing in for the
    withheld subtotal still admits rows on the strength of events that were
    delivered.
    """

    def setUp(self):
        self.config = {
            "version": "v1",
            "filters": {
                "min_event_count": 1,
                "exclude_namespaces": ["kube-system"],
            },
            "sections": {
                "telemetry_summary": True,
                "workload_breakdown": True,
                "suppressed_summary": True,
            },
        }

    def test_the_undelivered_subtotal_counts_only_the_undelivered_rows(self):
        # One failed post among nine that arrived. Same workload, reason and
        # severity, so the ledger rows land in one group.
        events = [event(notified=True) for _ in range(9)]
        events.append(event(notified=True, delivery_error="no message id from 'google_chat'"))

        summary = filter_and_aggregate_events(events, self.config)

        self.assertEqual(summary["delivery_failed"], 1)
        self.assertEqual(len(summary["delivery_failed_entries"]), 1)
        self.assertEqual(summary["delivery_failed_entries"][0]["delivery_failed_count"], 1)
        self.assertEqual(summary["delivery_failed_entries"][0]["count"], 10)

    def test_the_withheld_subtotal_counts_only_the_withheld_rows(self):
        """Two mechanisms keep the nine out, and this asserts they agree.

        `notified` is part of the group key, so the withheld row does not share
        a group with the nine that alerted and `count` is 1, not 10. The
        subtotal is asserted alongside it rather than replaced by it: it is
        what the threshold reads, and a key change that merged these rows again
        would surface here as the two numbers parting.
        """
        events = [event(notified=True) for _ in range(9)]
        events.append(event(notified=False))

        summary = filter_and_aggregate_events(events, self.config)

        self.assertEqual(summary["cap_dropped"], 1)
        self.assertEqual(len(summary["cap_dropped_entries"]), 1)
        self.assertEqual(summary["cap_dropped_entries"][0]["cap_dropped_count"], 1)
        self.assertEqual(summary["cap_dropped_entries"][0]["count"], 1)

    def test_the_threshold_measures_the_withheld_rows_too(self):
        """`min_event_count` asks how much of the measured thing there was.

        Against the group total, one withheld alert rides in on nine delivered
        events that are not withheld.
        """
        config = {**self.config, "filters": {**self.config["filters"], "min_event_count": 5}}
        events = [event(notified=True) for _ in range(9)]
        events.append(event(notified=False))

        summary = filter_and_aggregate_events(events, config)

        self.assertEqual(summary["cap_dropped"], 1)
        self.assertEqual(summary["cap_dropped_entries"], [])

    def test_a_group_that_is_wholly_withheld_still_clears_the_threshold(self):
        """The control: the threshold must still admit a genuinely noisy group."""
        config = {**self.config, "filters": {**self.config["filters"], "min_event_count": 5}}

        summary = filter_and_aggregate_events(
            [event(notified=False) for _ in range(6)], config
        )

        self.assertEqual(len(summary["cap_dropped_entries"]), 1)
        self.assertEqual(summary["cap_dropped_entries"][0]["cap_dropped_count"], 6)


class TestTheListingIsFixedToInfo(unittest.TestCase):
    """No configuration widens the listing, and a config that tries is told so.

    `include_severities` was a key and is not one now. Removing a key without
    saying so is the quiet half of this change: an operator who widened it is
    reading a narrower report than the one they wrote, and nothing on the page
    tells them which of the two they are looking at.
    """

    def setUp(self):
        self.events = [
            event(reason="OOMKilled", severity="Critical", notified=True),
            event(reason="Unhealthy", severity="Warning", notified=True),
            event(reason="BackOff", severity="Info", notified=False),
        ]
        self.sections = {
            "telemetry_summary": True,
            "workload_breakdown": True,
            "suppressed_summary": True,
        }

    def aggregate(self, filters):
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            summary = filter_and_aggregate_events(
                self.events, {"filters": filters, "sections": self.sections}
            )
        return summary, stderr.getvalue()

    def test_a_widened_config_still_lists_only_the_informational_event(self):
        summary, _ = self.aggregate(
            {"min_event_count": 1, "include_severities": ["Info", "Warning", "Critical"]}
        )

        self.assertEqual([e["reason"] for e in summary["entries"]], ["BackOff"])

    def test_a_widened_config_says_it_was_not_honoured(self):
        """The finding an operator can act on: what they asked for, and what they got."""
        _, warning = self.aggregate(
            {"min_event_count": 1, "include_severities": ["Info", "Warning", "Critical"]}
        )

        self.assertIn("no longer honoured", warning)
        self.assertIn("'Critical'", warning)
        self.assertIn("'Warning'", warning)
        self.assertIn("informational events only", warning)

    def test_a_leftover_info_only_key_is_called_obsolete_and_not_disobeyed(self):
        """Same listing either way, so the wording must not imply a lost setting."""
        summary, warning = self.aggregate(
            {"min_event_count": 1, "include_severities": ["Info"]}
        )

        self.assertEqual([e["reason"] for e in summary["entries"]], ["BackOff"])
        self.assertIn("obsolete", warning)
        self.assertNotIn("no longer honoured", warning)

    def test_a_config_without_the_key_says_nothing(self):
        """The control. The shipped config has no such key and must run silent."""
        summary, warning = self.aggregate({"min_event_count": 1})

        self.assertEqual([e["reason"] for e in summary["entries"]], ["BackOff"])
        self.assertEqual(warning, "")

    def test_the_listing_is_info_and_nothing_else(self):
        self.assertEqual(set(LISTED_SEVERITIES), {"Info"})


class TestTheNamespaceFilterDoesNotReachTheVeto(unittest.TestCase):
    """`exclude_namespaces` is a noise filter, and stops where the noise does.

    `kube-system` ships excluded and the watcher forwards it anyway. The recap
    does not report withheld or undelivered alerts from anywhere — but it must
    still count them, from every namespace, because that count is what stops it
    printing an all-clear. A `continue` on the excluded row would let a
    control-plane delivery failure end the day green.
    """

    def setUp(self):
        self.config = {
            "version": "v1",
            "filters": {
                "min_event_count": 1,
                "exclude_namespaces": ["kube-system"],
            },
            "sections": {
                "telemetry_summary": True,
                "workload_breakdown": True,
                "suppressed_summary": True,
            },
        }

    def report(self, events, config=None):
        config = config or self.config
        summary = filter_and_aggregate_events(events, config)
        return summary, generate_markdown_report(
            summary, config, cluster_name="test-cluster"
        )

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
        self.assertEqual(summary["cap_dropped_entries"], [])
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



class TestTheListingHoldsOnlyRowsHeldBackFromChat(unittest.TestCase):
    """The heading, the headline and the closing total describe one set of rows.

    Selecting the listing on severity alone let an Info row that *did* reach
    chat into it, while `suppressed_info` — the 📉 total — counted only the
    rows that did not. One set of rows was then announced as "not repeated
    here", repeated here under a heading saying they never arrived, and
    totalled as zero. Only a session server predating the Info gate writes such
    a row, which is what made it easy to miss.
    """

    CONFIG = {
        "filters": {"min_event_count": 1, "exclude_namespaces": []},
        "sections": {
            "telemetry_summary": True,
            "workload_breakdown": True,
            "suppressed_summary": True,
        },
    }

    def report(self, events):
        summary = filter_and_aggregate_events(events, self.CONFIG)
        return summary, generate_markdown_report(
            summary, self.CONFIG, cluster_name="prod", report_date="2026-08-17"
        )

    def test_an_informational_row_that_reached_chat_is_not_listed(self):
        summary, report = self.report([event(severity="Info", notified=True) for _ in range(3)])

        self.assertEqual(summary["entries"], [])
        self.assertEqual(summary["suppressed_info"], 0)
        self.assertEqual(summary["alerts_posted"], 3)
        self.assertNotIn("Held Back from Chat*", report)
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
        self.assertIn("• 1 event)", report)
        self.assertIn("*1 informational event* held back", report)

    def test_the_ordinary_day_is_unchanged(self):
        """The control. Every row this version writes is notified=0."""
        summary, report = self.report([event(severity="Info", notified=False) for _ in range(3)])

        self.assertEqual(len(summary["entries"]), 1)
        self.assertEqual(summary["entries"][0]["count"], 3)
        self.assertIn("Held Back from Chat*", report)
        self.assertIn("*3 informational events* held back", report)


class TestAKeyWithNoValueKeepsItsDefault(unittest.TestCase):
    """A well-formed edit must not stop the recap arriving.

    YAML maps a key written with no value to None, and the merge in
    `load_config` assigned that over the built-in default. `set(None)` then
    raised in the aggregator, `main()` caught nothing, and this job's stdout
    *is* the chat message — so the recap vanished every weekday with the
    traceback going only to the container log. The trigger is the obvious way
    to stop excluding namespaces: comment the three list items out.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def config_from(self, text):
        path = os.path.join(self.tmp.name, "eod_report_config.yaml")
        pathlib.Path(path).write_text(text, encoding="utf-8")
        stderr = io.StringIO()
        with mock.patch.object(sys, "stderr", stderr):
            cfg = load_config(path)
        return cfg, stderr.getvalue()

    def render(self, cfg):
        summary = filter_and_aggregate_events(
            [event(severity="Info", notified=False)], cfg
        )
        return generate_markdown_report(summary, cfg, cluster_name="prod")

    def test_a_bare_exclude_namespaces_keeps_the_default_list(self):
        cfg, _ = self.config_from(
            "version: v1\nfilters:\n  min_event_count: 1\n  exclude_namespaces:\n"
        )

        self.assertEqual(
            cfg["filters"]["exclude_namespaces"],
            ["kube-system", "kube-public", "kube-node-lease"],
        )
        self.assertIn("Daily Activity Recap", self.render(cfg))

    def test_every_emptied_key_still_renders(self):
        """Each of these was a different traceback before, and all reached chat as silence."""
        for label, text in (
            ("filters", "version: v1\nfilters:\n"),
            ("sections", "version: v1\nsections:\n"),
            ("min_event_count", "version: v1\nfilters:\n  min_event_count:\n"),
        ):
            with self.subTest(emptied=label):
                cfg, _ = self.config_from(text)
                self.assertIn("Daily Activity Recap", self.render(cfg))

    def test_an_explicit_empty_list_still_overrides_the_default(self):
        """`None` means "said nothing"; `[]` means "exclude nothing". Not the same."""
        cfg, _ = self.config_from(
            "version: v1\nfilters:\n  exclude_namespaces: []\n"
        )

        self.assertEqual(cfg["filters"]["exclude_namespaces"], [])

    def test_an_empty_file_is_named_rather_than_reported_missing(self):
        """The fall-through warning lists every path searched, including this one."""
        _, warnings = self.config_from("")

        self.assertIn("is empty or is not a YAML mapping", warnings)


if __name__ == "__main__":
    unittest.main()
