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

    def test_the_inject_rows_come_back_by_reason(self):
        import os
        import sqlite3
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            self.make_stores(root)
            kv = os.path.join(root, "session_kv.db")
            conn = sqlite3.connect(kv)
            conn.execute("CREATE TABLE intercepted_events (session_id TEXT, object_uid TEXT, reason TEXT)")
            conn.executemany("INSERT INTO intercepted_events VALUES (?, ?, ?)",
                             [("", "k8s-evt-1", "ControllerStall"), ("k8s-evt-2", "u", "OutOfBandChange"),
                              ("k8s-evt-3", "u", "BackOff")])
            conn.commit()
            out = subprocess.run([sys.executable, "-c", at.READ_SCRIPT, root, "1000", kv, *at.INJECT_REASONS],
                                 capture_output=True, text=True, check=True, env={}).stdout
        injects = at.decode(out)["injects"]
        self.assertEqual(sorted(i["reason"] for i in injects), ["ControllerStall", "OutOfBandChange"])

    def test_a_cut_off_read_is_an_error_that_says_so(self):
        with self.assertRaises(at.kube.CommandError):
            at.decode("eJzLSM3JyQcABiwCFQ"[:10])


class BusDelegationTest(unittest.TestCase):
    def stores(self, chats):
        sessions = [session("w", source="kanban", started=150.0)]
        for i, (start, end) in enumerate(chats):
            s = session(f"chat{i}", source="api_server", started=start)
            s["ended_at"] = end
            sessions.append(s)
        return {"sessions": sessions, "cards": [], "runs": []}

    def test_a_cardless_worker_joins_the_one_chat_turn_open_when_it_started(self):
        joined = at.join(self.stores([(100.0, 300.0)]), CAPS)
        self.assertEqual(joined.capability["w"], at.CHAT)
        self.assertEqual(joined.rules["w"], "bus delegation from the one open chat turn")

    def test_two_open_chat_turns_or_none_leave_it_unattributed(self):
        self.assertEqual(at.join(self.stores([(100.0, 300.0), (120.0, 400.0)]), CAPS).capability["w"],
                         at.UNATTRIBUTED)
        self.assertEqual(at.join(self.stores([(10.0, 20.0)]), CAPS).capability["w"], at.UNATTRIBUTED)


class ProfileSplitTest(unittest.TestCase):
    def test_each_row_splits_its_tokens_by_front_door_platform_and_cluster_agents(self):
        data = {"sessions": [session("cron_compliance-audit_20261111_200909", profile="platform"),
                             session("child", source="subagent", parent="cron_compliance-audit_20261111_200909",
                                     profile="cluster-p-c1-us-central1-f")],
                "cards": [], "runs": []}
        row = at.attribute(data, 0, 500, caps=CAPS)["capabilities"]["compliance-audit"]
        self.assertEqual(row["by_profile"], {"platform": 100, at.CLUSTER_AGENTS: 100})
        self.assertEqual(at.profile_kind("default"), "default")


class InjectJoinTest(unittest.TestCase):
    def test_a_stall_or_drift_inject_counts_under_its_origin_and_an_event_stays_triage(self):
        data = {"sessions": [session("k8s-evt-1", source="api_server"), session("k8s-evt-2", source="api_server"),
                             session("k8s-evt-3", source="api_server")],
                "cards": [], "runs": [],
                "injects": [{"session_id": "", "object_uid": "k8s-evt-1", "reason": "ControllerStall"},
                            {"session_id": "k8s-evt-2", "object_uid": "x", "reason": "OutOfBandChange"}]}
        joined = at.join(data, CAPS + ("stall-watch",)).capability
        self.assertEqual(joined["k8s-evt-1"], "stall-watch")
        self.assertEqual(joined["k8s-evt-2"], at.GITOPS_DRIFT)
        self.assertEqual(joined["k8s-evt-3"], at.INCIDENT_TRIAGE)


class FallbackTest(unittest.TestCase):
    def test_a_card_whose_creator_has_no_row_falls_back_to_its_origin(self):
        data = {"sessions": [session("w", source="kanban")],
                "cards": [{"id": "t", "title": "x", "body": "", "session_id": "pruned", "idempotency_key": None,
                           "origin": "chat"}],
                "runs": [{"task_id": "t", "profile": "platform", "started_at": 90.0,
                          "metadata": json.dumps({"worker_session_id": "w"})}]}
        self.assertEqual(at.join(data, CAPS).capability["w"], at.CHAT)

    def test_a_child_whose_parent_has_no_row_falls_back_to_its_own_source(self):
        data = {"sessions": [session("c", source="api_server", parent="pruned")], "cards": [], "runs": []}
        self.assertEqual(at.join(data, CAPS).capability["c"], at.CHAT)


class ChunkedReadTest(unittest.TestCase):
    def test_a_long_read_comes_back_in_chunks_and_the_pod_file_is_removed(self):
        import base64
        import zlib
        from unittest import mock
        blob = base64.b64encode(zlib.compress(json.dumps({"sessions": [{"id": __import__("os").urandom(8).hex()} for _ in range(20000)]}).encode())).decode()
        calls = []

        def kubectl(context, *argv, timeout=None):
            last = len(argv) - 1 - argv[::-1].index("-c")
            script, args = argv[last + 1], argv[last + 2:]
            calls.append(script)
            if script == at.READ_SCRIPT:
                return f"{len(blob)}\n"
            if args[1] == at.REMOVE:
                return ""
            offset, length = int(args[1]), int(args[2])
            return blob[offset:offset + length]

        with mock.patch.object(at.operations, "_gateway_pod", return_value="pod/g"), \
                mock.patch.object(at.kube, "kubectl", side_effect=kubectl):
            stores = at.read_stores("ctx", "ns")
        self.assertEqual(len(stores["sessions"]), 20000)
        self.assertGreater(calls.count(at.CHUNK_SCRIPT), 2)

    def test_an_archive_round_trips(self):
        import pathlib
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "s.json.gz"
            at.archive(stores(), path)
            self.assertEqual(at.load_archive(path), stores())


class MemoryTest(unittest.TestCase):
    def data(self):
        data = stores()
        data["activity"] = [["cron_compliance-audit_20261111_200909", 2, 3, 0], ["k8s-evt-3f9c88ce", 2, 1, 0]]
        for s in data["sessions"]:
            s["ended_at"] = s["started_at"] + 1000
        return data

    def test_the_excess_is_split_by_the_minute_s_messages_and_the_rest_is_the_base(self):
        # One container, idle at 1 GiB, at 2 GiB for the minute starting at 120 s.
        series = {"agent": [(60.0, at.GIB), (120.0, 2 * at.GIB), (180.0, at.GIB)]}
        mem = at.memory_by_capability(series, self.data(), 60, 240, caps=CAPS)
        rows = {r["capability"]: r for r in mem["rows"]}
        excess = 1 / 60  # 1 GiB for one minute, in GiB-hours
        self.assertAlmostEqual(rows["compliance-audit"]["gib_hours"], excess * 3 / 4, places=4)
        self.assertAlmostEqual(rows[at.INCIDENT_TRIAGE]["gib_hours"], excess / 4, places=4)
        self.assertAlmostEqual(sum(r["share"] for r in mem["rows"]), 1.0, places=3)
        self.assertEqual(mem["rows"][-1]["capability"], at.PLATFORM_BASE)

    def test_live_sessions_with_no_message_share_equally_and_no_session_is_unattributed(self):
        data = self.data()
        data["activity"] = []
        series = {"agent": [(100.0, 2 * at.GIB)]}
        rows = {r["capability"]: r for r in at.memory_by_capability(series, data, 100, 160, base={"agent": at.GIB},
                                                                     caps=CAPS)["rows"]}
        # Equal shares among the live sessions: the compliance audit has 3 of them (its cron session,
        # its card's worker and a subagent), chat 1.
        self.assertAlmostEqual(rows["compliance-audit"]["gib_hours"] / rows[at.CHAT]["gib_hours"], 3.0, places=3)
        empty = {"sessions": [], "cards": [], "runs": []}
        rows = {r["capability"]: r for r in at.memory_by_capability(series, empty, 100, 160, base={"agent": at.GIB},
                                                                     caps=CAPS)["rows"]}
        self.assertIn(at.UNATTRIBUTED, rows)


class CallsTest(unittest.TestCase):
    def data(self):
        data = stores()
        # w-ghost is a card's worker whose session never reached state.db; its calls are in the span store.
        data["runs"].append({"task_id": "t1", "profile": "cluster-p-c2-us-central1-f", "started_at": 90.0,
                             "metadata": json.dumps({"worker_session_id": "w-ghost"})})
        data["calls"] = [["platform", "cron_compliance-audit_20261111_200909", 2, 3, 300, 200, 30],
                         ["cluster-p-c2-us-central1-f", "w-ghost", 3, 2, 100, 50, 10],
                         ["platform", "cron_compliance-audit_20261111_200909", 9, 1, 50, 0, 5]]
        return data

    def test_tokens_come_from_the_calls_in_their_minute_and_a_ghost_session_joins_through_its_card(self):
        record = at.attribute(self.data(), 120, 240, caps=CAPS)
        row = record["capabilities"]["compliance-audit"]
        # The cron session's minute-2 call and w-ghost's minute-3 calls; the minute-9 call is outside.
        self.assertEqual((row["input"], row["cached"], row["output"]), (100 + 50, 200 + 50, 40))
        self.assertEqual(row["calls"], 5)
        self.assertEqual(row["sessions"], 2)
        # w-triage has no call in the span store, so its row (started at 130 s) counts as well.
        self.assertEqual(record["recorded"], {"input": 400 + 90, "output": 40 + 10, "source": "calls and sessions"})

    def test_a_session_whose_calls_the_span_store_lost_keeps_its_row(self):
        data = self.data()
        # The cron session's row holds more than its calls: the rolling span store dropped the older ones.
        data["sessions"][0]["input_tokens"] = 1000
        self.assertEqual(at.call_sessions(data), {"w-ghost"})
        row = at.attribute(data, 60, 240, caps=CAPS)["capabilities"]["compliance-audit"]
        # The cron session, w-audit and child by their rows; w-ghost, which has no row, by its calls.
        self.assertEqual(row["input"], 1000 + 10 + 10 + (100 - 50))

    def test_memory_follows_the_minute_s_calls(self):
        series = {"agent": [(120.0, 2 * at.GIB)]}
        mem = at.memory_by_capability(series, self.data(), 120, 180, base={"agent": at.GIB}, caps=CAPS)
        rows = {r["capability"]: r for r in mem["rows"]}
        self.assertAlmostEqual(rows["compliance-audit"]["gib_hours"], 1 / 60, places=4)

    def test_memory_weighs_a_session_the_span_store_lost_by_its_messages(self):
        data = self.data()
        data["sessions"][0]["input_tokens"] = 1000
        data["activity"] = [["k8s-evt-3f9c88ce", 2, 1, 0], ["cron_compliance-audit_20261111_200909", 2, 4, 0]]
        series = {"agent": [(120.0, 2 * at.GIB)]}
        rows = {r["capability"]: r for r in at.memory_by_capability(series, data, 120, 180, base={"agent": at.GIB},
                                                                     caps=CAPS)["rows"]}
        # Minute 2 holds the cron session's 4 messages, not its 3 calls, beside the event session's 1 message.
        self.assertAlmostEqual(rows[at.INCIDENT_TRIAGE]["gib_hours"], 1 / 60 / 5, places=4)
        self.assertAlmostEqual(rows["compliance-audit"]["gib_hours"], 1 / 60 * 4 / 5, places=4)

    def test_the_read_script_takes_only_model_call_spans(self):
        import os
        import sqlite3
        import subprocess
        import sys
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            ReadScriptTest().make_stores(root)
            conn = sqlite3.connect(os.path.join(root, "profiles", "platform", "hermes_otel_live.db"))
            conn.execute("CREATE TABLE events (seq INTEGER PRIMARY KEY, kind TEXT, ts REAL, data TEXT)")
            usage = {"session.id": "s1", "gen_ai.usage.input_tokens": 100, "gen_ai.usage.cache_read_input_tokens": 80,
                     "gen_ai.usage.output_tokens": 7}
            for name in ("api.model-default", "agent"):
                conn.execute("INSERT INTO events (kind, ts, data) VALUES ('span', 0, ?)",
                             (json.dumps({"name": name, "start_time_unix_nano": 120 * 10 ** 9, "attributes": usage}),))
            conn.commit()
            out = subprocess.run([sys.executable, "-c", at.READ_SCRIPT, root, "0"], capture_output=True, text=True,
                                 check=True, env={}).stdout
        self.assertEqual(at.decode(out)["calls"], [["platform", "s1", 2, 1, 100, 80, 7]])


if __name__ == "__main__":
    unittest.main()
