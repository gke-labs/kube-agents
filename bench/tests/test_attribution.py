"""Tokens by capability from the session records: the join rules, the overview and the two checks."""

import json
import unittest

from kube_agents_bench.scale import attribution as at

CAPS = ("compliance-audit", "stall-watch", "bootstrap-inventory-scan", "oobe") + at.EXTRA_ROWS


def session(sid, source="cron", started=100.0, inp=10, cached=80, out=10, parent=None, profile="platform"):
    return {"id": sid, "source": source, "started_at": started, "parent_session_id": parent, "profile": profile,
            "input_tokens": inp, "cache_read_tokens": cached, "output_tokens": out, "reasoning_tokens": 0}


def stores():
    return {
        "sessions": [
            session("cron_compliance-audit_20261111_200909"),
            session("cron_oobe_20261111_190000"),
            session("k8s-evt-3f9c88ce", source="api_server"),
            session("834e1e91", source="api_server"),
            session("w-audit", source="kanban"),
            session("w-triage", source="kanban", started=130.0),
            session("w-first", source="kanban"),
            session("w-lost", source="kanban", started=900.0),
            session("child", source="subagent", parent="cron_compliance-audit_20261111_200909"),
            session("cron_retired-job_20261111_200909"),
        ],
        "cards": [
            {"id": "t1", "title": "Run the compliance-audit procedure", "body": "", "session_id": None,
             "idempotency_key": None, "origin": "chat"},
            {"id": "t2", "title": "Triage a FailedScheduling", "body": "", "session_id": "k8s-evt-3f9c88ce",
             "idempotency_key": None, "origin": "event"},
            {"id": "t3", "title": "Write the onboarding inventory report", "body": "", "session_id": None,
             "idempotency_key": "bootstrap-inventory-scan", "origin": None},
        ],
        "runs": [
            {"task_id": "t1", "profile": "platform", "started_at": 90.0,
             "metadata": json.dumps({"worker_session_id": "w-audit"})},
            # A blocked run keeps no worker session id: joined by its start.
            {"task_id": "t2", "profile": "platform", "started_at": 100.0, "metadata": None},
            {"task_id": "t3", "profile": "platform", "started_at": 90.0,
             "metadata": json.dumps({"worker_session_id": "w-first"})},
        ],
        "errors": [],
    }


class JoinTest(unittest.TestCase):
    def setUp(self):
        self.joined = at.join(stores(), CAPS).capability

    def test_a_cron_session_counts_under_its_job_and_oobe_under_the_first_run_flow(self):
        self.assertEqual(self.joined["cron_compliance-audit_20261111_200909"], "compliance-audit")
        self.assertEqual(self.joined["cron_oobe_20261111_190000"], at.FIRST_RUN)

    def test_a_card_that_names_an_audit_counts_under_the_audit_whatever_its_origin(self):
        self.assertEqual(self.joined["w-audit"], "compliance-audit")

    def test_a_card_counts_under_the_session_that_created_it_joined_by_start_when_the_run_names_none(self):
        self.assertEqual(self.joined["w-triage"], at.INCIDENT_TRIAGE)

    def test_a_first_run_card_counts_under_the_first_run_flow(self):
        self.assertEqual(self.joined["w-first"], at.FIRST_RUN)

    def test_events_chat_and_subagents(self):
        self.assertEqual(self.joined["k8s-evt-3f9c88ce"], at.INCIDENT_TRIAGE)
        self.assertEqual(self.joined["834e1e91"], at.CHAT)
        self.assertEqual(self.joined["child"], "compliance-audit")

    def test_what_no_rule_reaches_is_unattributed(self):
        self.assertEqual(self.joined["w-lost"], at.UNATTRIBUTED)
        self.assertEqual(self.joined["cron_retired-job_20261111_200909"], at.UNATTRIBUTED)

    def test_the_capability_list_holds_both_rosters_and_the_fixed_rows(self):
        caps = at.capabilities()
        self.assertIn("stall-watch", caps)
        self.assertIn("profile-cron-tick", caps)
        self.assertEqual(caps[-len(at.EXTRA_ROWS):], at.EXTRA_ROWS)


class AttributeTest(unittest.TestCase):
    def test_only_sessions_started_in_the_window_count(self):
        record = at.attribute(stores(), 0, 500, caps=CAPS)
        # w-lost started at 900, outside the window; only the retired job's session is left unattributed.
        self.assertEqual(record["capabilities"][at.UNATTRIBUTED]["sessions"], 1)
        self.assertEqual(record["capabilities"]["compliance-audit"]["sessions"], 3)

    def test_the_overview_adds_up_to_the_total(self):
        record = at.attribute(stores(), 0, 1000, caps=CAPS)
        self.assertEqual(sum(r["tokens"] for r in record["overview"]), record["total_tokens"])
        self.assertEqual([r["capability"] for r in record["overview"]][-2:], [at.OTHER, at.UNATTRIBUTED])

    def test_the_sum_check_counts_cached_input_once_against_litellm(self):
        # 9 sessions in [0, 500): input 90 + cached 720 = 810, output 90.
        litellm = {"tokens_input": 810, "tokens_cached": 720, "tokens_output": 90}
        sound = at.attribute(stores(), 0, 500, litellm=litellm, caps=CAPS)["checks"]["sum"]
        self.assertTrue(sound["passed"], sound)
        off = at.attribute(stores(), 0, 500, litellm={**litellm, "tokens_input": 2000}, caps=CAPS)
        self.assertFalse(off["checks"]["sum"]["passed"])

    def test_a_window_with_no_traffic_on_either_side_is_sound(self):
        empty = {"tokens_input": 0, "tokens_cached": 0, "tokens_output": 0}
        self.assertTrue(at.attribute(stores(), 2000, 3000, litellm=empty, caps=CAPS)["checks"]["sum"]["passed"])
        self.assertFalse(at.attribute(stores(), 0, 500, litellm=empty, caps=CAPS)["checks"]["sum"]["passed"])

    def test_tokens_litellm_counted_outside_any_session_are_unattributed(self):
        # The sessions in [0, 500) hold input 90, cached 720, output 90; LiteLLM saw 100 more of each.
        litellm = {"tokens_input": 1010, "tokens_cached": 820, "tokens_output": 190}
        record = at.attribute(stores(), 0, 500, litellm=litellm, caps=CAPS)
        self.assertEqual(record["outside_sessions"], {"input": 100, "cached": 100, "output": 100})
        self.assertEqual(record["total_tokens"], record["session_tokens"] + 300)
        self.assertEqual(sum(r["tokens"] for r in record["overview"]), record["total_tokens"])
        self.assertIn("outside any session", record["unattributed_reasons"])

    def test_no_counters_fail_the_sum_check_rather_than_pass_it(self):
        self.assertFalse(at.attribute(stores(), 0, 500, caps=CAPS)["checks"]["sum"]["passed"])

    def test_an_unattributed_share_above_five_percent_fails(self):
        check = at.attribute(stores(), 0, 1000, caps=CAPS)["checks"]["unattributed"]
        self.assertFalse(check["passed"])  # 2 of 10 equal sessions

    def test_the_report_has_a_table_for_each_window(self):
        record = {"burst": at.attribute(stores(), 0, 500, caps=CAPS)}
        text = at.markdown(record)
        self.assertIn("## burst window (unsound)", text)
        self.assertIn("| compliance-audit | 3 |", text)


if __name__ == "__main__":
    unittest.main()


class ReadScriptTest(unittest.TestCase):
    """The in-pod read script itself, run here on stores of the live schema's shape."""

    def make_stores(self, root):
        import os
        import sqlite3
        os.makedirs(os.path.join(root, "profiles", "platform"))
        for path in (os.path.join(root, "state.db"), os.path.join(root, "profiles", "platform", "state.db")):
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE sessions (id TEXT, source TEXT, started_at REAL, parent_session_id TEXT, "
                         "input_tokens INT, output_tokens INT, cache_read_tokens INT, reasoning_tokens INT)")
            conn.executemany("INSERT INTO sessions VALUES (?, 'cron', ?, NULL, 1, 2, 3, 0)",
                             [("old", 10.0)] + [(f"cron_stall-watch_20261112_{i:06d}", 100000.0 + i) for i in range(3000)])
            conn.commit()
        conn = sqlite3.connect(os.path.join(root, "kanban.db"))
        conn.execute("CREATE TABLE tasks (id TEXT, title TEXT, body TEXT, session_id TEXT, idempotency_key TEXT, "
                     "created_at REAL)")
        conn.execute("CREATE TABLE task_runs (task_id TEXT, profile TEXT, started_at REAL, metadata TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('t1', 'x', ?, NULL, NULL, 100000.0)", ("b" * 100000,))
        conn.commit()

    def test_a_read_past_64_kib_comes_back_whole_and_starts_at_since(self):
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            self.make_stores(root)
            out = subprocess.run([sys.executable, "-c", at.READ_SCRIPT, root, "1000"], capture_output=True,
                                 text=True, check=True).stdout
        self.assertLess(len(out), 65536)
        stores = at.decode(out)
        self.assertEqual(len(stores["sessions"]), 6000)
        self.assertNotIn("old", {s["id"] for s in stores["sessions"]})
        self.assertEqual(len(stores["cards"][0]["body"]), 100000)

    def test_a_cut_off_read_is_an_error_that_says_so(self):
        with self.assertRaises(at.kube.CommandError):
            at.decode("eJzLSM3JyQcABiwCFQ"[:10])
