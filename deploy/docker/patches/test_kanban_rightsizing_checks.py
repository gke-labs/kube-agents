"""Unit tests for the rightsizing rule in the kanban_complete gate.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import copy
import json
import os
import pathlib
import sqlite3
import tempfile
import unittest

import kanban_rightsizing_checks as krc
import kanban_verified_claims as kvc
from apply_kanban_rightsizing_checks import ANCHOR, KANBAN_RELATIVE, MARKER, apply

PARENT = "t_platform"
CHILD = "t_seeded_a"
SKILL_BODY = "Lower the requests of api-server. Run the `gke-rightsizing-checks` skill first."
CHECKS = {
    "hpa": "HPA api-server targets 60% CPU (kubectl get hpa -n web)",
    "qos": "Burstable: req 400m, lim 500m",
    "sharing": "PDB api-server selects it; no PVC",
    "source": "meta.helm.sh/release-name=web",
    "packing": "940m allocatable per node; nodes needed = 2",
}
HPA_ENTRY = {"workload": "api-server", "dependency_kind": "hpa",
             "dependency": "A lower request adds replicas.", "checks": CHECKS}
QOS_ENTRY = {"workload": "queue-worker", "dependency_kind": "guaranteed_qos",
             "dependency": "A lower request is also a lower limit.", "checks": CHECKS}


class RuleTestCase(unittest.TestCase):
    def setUp(self):
        kvc._calls.clear()
        kvc._refused_at.clear()
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, result TEXT, body TEXT, assignee TEXT, title TEXT)")
        conn.execute("CREATE TABLE task_runs (id INTEGER PRIMARY KEY, task_id TEXT, outcome TEXT, metadata TEXT)")
        conn.execute(
            "CREATE TABLE kanban_worker_children (child_id TEXT PRIMARY KEY, creator_id TEXT NOT NULL, created_at INTEGER NOT NULL)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.path)

    def card(self, task_id, body="", creator=None, stored=None, outcome="completed", assignee="cluster-a", title=""):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?)",
                     (task_id, "done" if stored else "running", "", body, assignee, title))
        if creator:
            conn.execute("INSERT INTO kanban_worker_children VALUES (?, ?, 0)", (task_id, creator))
        if stored is not None:
            conn.execute("INSERT INTO task_runs (task_id, outcome, metadata) VALUES (?, ?, ?)",
                         (task_id, outcome, json.dumps({krc.METADATA_KEY: stored})))
        conn.commit()
        conn.close()

    def connect(self):
        return sqlite3.connect(self.path)

    def rule(self, task_id, entries=None, result="**Answer.**", retry=False, connect="default", metadata=None):
        args = {} if entries is None else {krc.PARAM: entries}
        return krc.rightsizing_rule(
            task_id=task_id, args=args, result=result, metadata=metadata, calls=[], retry=retry,
            redact=lambda value: value, connect=self.connect if connect == "default" else connect,
        )


class RegistrationTest(unittest.TestCase):
    def test_importing_the_module_registers_the_rule_once_and_first(self):
        self.assertEqual(kvc.RULES.count(krc.rightsizing_rule), 1)
        self.assertIs(kvc.RULES[0], krc.rightsizing_rule)

    def test_the_schema_gets_the_parameter_once(self):
        schema = {"parameters": {"properties": {}}}
        krc.apply_schema(schema)
        self.assertIn(krc.PARAM, schema["parameters"]["properties"])
        with self.assertRaises(RuntimeError):
            krc.apply_schema(schema)


class UntriggeredTest(RuleTestCase):
    def test_a_card_the_skill_was_not_asked_for_passes_unchanged(self):
        self.card(CHILD, body="Why is payments-api crashlooping?")
        self.assertEqual(self.rule(CHILD, result="It is OOMKilled."), ([], "It is OOMKilled.", None))

    def test_no_board_connection_passes(self):
        self.assertEqual(self.rule(CHILD, connect=None), ([], "**Answer.**", None))

    def test_a_board_that_cannot_be_read_passes_and_keeps_own_entries(self):
        def broken():
            raise sqlite3.OperationalError("database is locked")

        with self.assertLogs(krc.logger, "WARNING"):
            self.assertEqual(self.rule(CHILD, connect=broken)[0], [])
        with self.assertLogs(krc.logger, "WARNING"):
            _, result, metadata = self.rule(CHILD, [HPA_ENTRY], connect=broken)
        self.assertIn("- `api-server`: dependency: HPA.", result)
        self.assertEqual(metadata[krc.METADATA_KEY][0]["workload"], "api-server")

    def test_a_model_written_metadata_key_is_dropped(self):
        self.card(CHILD, body="Check pods.")
        _, _, metadata = self.rule(CHILD, metadata={krc.METADATA_KEY: [HPA_ENTRY], "a": 1})
        self.assertEqual(metadata, {"a": 1})


class OwnEntriesTest(RuleTestCase):
    def setUp(self):
        super().setUp()
        self.card(CHILD, body=SKILL_BODY)

    def test_no_entries_is_refused(self):
        [reason] = self.rule(CHILD)[0]
        self.assertIn(f"pass `{krc.PARAM}`", reason)

    def test_valid_entries_are_stored_and_rendered(self):
        reasons, result, metadata = self.rule(CHILD, [HPA_ENTRY, QOS_ENTRY], metadata={"a": 1})
        self.assertEqual(reasons, [])
        self.assertEqual([e["workload"] for e in metadata[krc.METADATA_KEY]], ["api-server", "queue-worker"])
        self.assertEqual(metadata["a"], 1)
        self.assertTrue(result.startswith("**Answer.**\n\n" + krc.HEADING))
        self.assertIn("- `api-server`: dependency: HPA. A lower request adds replicas.", result)
        self.assertIn("- `queue-worker`: dependency: Guaranteed QoS. A lower request is also a lower limit.", result)
        self.assertIn("  - Per-node packing: 940m allocatable per node; nodes needed = 2", result)

    def test_an_unknown_kind_or_a_missing_check_is_refused(self):
        bad_kind = {**HPA_ENTRY, "dependency_kind": "autoscaler"}
        no_packing = copy.deepcopy(QOS_ENTRY)
        del no_packing["checks"]["packing"]
        reasons = self.rule(CHILD, [bad_kind, no_packing])[0]
        self.assertEqual(len(reasons), 2)
        self.assertIn("dependency_kind 'autoscaler'", reasons[0])
        self.assertIn("no finding for packing", reasons[1])

    def test_the_retry_keeps_the_valid_entries_and_drops_the_rest(self):
        bad = {**HPA_ENTRY, "dependency": " "}
        with self.assertLogs(krc.logger, "WARNING"):
            reasons, result, metadata = self.rule(CHILD, [bad, QOS_ENTRY], retry=True)
        self.assertEqual(reasons, [])
        self.assertEqual([e["workload"] for e in metadata[krc.METADATA_KEY]], ["queue-worker"])
        self.assertNotIn("`api-server`", result)

    def test_not_on_cluster_needs_no_checks(self):
        gone = {"workload": "api-server", "dependency_kind": "not_on_cluster", "dependency": "No such deployment here."}
        reasons, result, _ = self.rule(CHILD, [gone])
        self.assertEqual(reasons, [])
        self.assertIn("dependency: not on this cluster", result)

    def test_a_field_is_one_line_and_holds_no_fence(self):
        spread = {**HPA_ENTRY, "dependency": "A lower request\n\n  adds replicas."}
        fenced = copy.deepcopy(QOS_ENTRY)
        fenced["checks"]["packing"] = "~~~\nnodes needed = 2"
        ticked = {**HPA_ENTRY, "workload": "api`-server"}
        reasons = self.rule(CHILD, [spread, fenced, ticked])[0]
        self.assertEqual(len(reasons), 2)
        self.assertIn("code fence", reasons[0])
        self.assertIn("backtick", reasons[1])
        result = self.rule(CHILD, [spread])[1]
        self.assertIn("dependency: HPA. A lower request adds replicas.", result)

    def test_stored_fields_are_redacted(self):
        _, result, metadata = krc.rightsizing_rule(
            task_id=CHILD, args={krc.PARAM: [HPA_ENTRY]}, result="**Answer.**", metadata=None, calls=[],
            retry=False, redact=lambda value: value.replace("api-server", "[x]"), connect=self.connect,
        )
        entry = metadata[krc.METADATA_KEY][0]
        self.assertEqual((entry["workload"], entry["checks"]["hpa"]), ("[x]", "HPA [x] targets 60% CPU (kubectl get hpa -n web)"))
        self.assertNotIn("api-server", result)

    def test_the_section_goes_before_the_evidence_section(self):
        kvc._calls[CHILD] = [{"command": "kubectl get hpa -n web", "exit_code": 0, "output": "api-server 60%", "ran_at": "t"}]
        claim = {"claim": "An HPA targets it.", "command": "kubectl get hpa -n web", "excerpt": "api-server 60%"}
        err, result, _ = kvc.apply_completion_rules(
            CHILD, {krc.PARAM: [HPA_ENTRY], kvc.PARAM: [claim]}, "**Answer.**", {}, connect=self.connect)
        self.assertIsNone(err)
        self.assertLess(result.index(krc.HEADING), result.index(kvc.EVIDENCE_HEADING))

    def test_a_field_past_the_cap_is_refused(self):
        long = {**HPA_ENTRY, "dependency": "x" * (krc.MAX_FIELD_CHARS + 1)}
        [reason] = self.rule(CHILD, [long])[0]
        self.assertIn("longer than", reason)


class InheritedEntriesTest(RuleTestCase):
    def setUp(self):
        super().setUp()
        self.card(PARENT, body="Lower the requests of api-server and queue-worker.")
        self.card(CHILD, body=SKILL_BODY, creator=PARENT, stored=[HPA_ENTRY, QOS_ENTRY])

    def test_a_parent_gets_its_childrens_entries_rendered(self):
        reasons, result, metadata = self.rule(PARENT, result="**No cut is safe.**")
        self.assertEqual(reasons, [])
        self.assertIn(f"- `api-server` (card {CHILD}): dependency: HPA.", result)
        self.assertIn(f"- `queue-worker` (card {CHILD}): dependency: Guaranteed QoS.", result)
        self.assertIsNone(metadata)

    def test_a_grandchild_counts_too(self):
        self.card("t_grandchild", body=SKILL_BODY, creator=CHILD,
                  stored=[{**HPA_ENTRY, "workload": "db", "dependency_kind": "none", "dependency": "Nothing blocks it."}])
        result = self.rule(PARENT)[1]
        self.assertIn("- `db` (card t_grandchild): dependency: none.", result)

    def test_a_run_that_did_not_complete_is_skipped(self):
        self.card("t_crashed", body=SKILL_BODY, creator=PARENT, stored=[{**HPA_ENTRY, "workload": "crashed"}], outcome="crashed")
        self.assertNotIn("crashed", self.rule(PARENT)[1])

    def test_a_card_that_already_quotes_the_section_gets_no_second_copy(self):
        quoted = "Result of delegated task:\n\n" + krc.render([{**HPA_ENTRY, "card": CHILD}, {**QOS_ENTRY, "card": CHILD}])
        reasons, result, _ = self.rule(PARENT, result=quoted)
        self.assertEqual((reasons, result), ([], quoted))

    def test_a_copy_that_drops_an_entry_gets_the_dropped_one_rendered(self):
        copied = "**No cut is safe.**\n\n" + krc.render([HPA_ENTRY])
        result = self.rule(PARENT, result=copied)[1]
        self.assertEqual(result.count("`api-server`"), 1)
        self.assertIn(f"- `queue-worker` (card {CHILD}): dependency: Guaranteed QoS.", result)

    def test_only_the_latest_completed_run_counts(self):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO task_runs (task_id, outcome, metadata) VALUES (?, ?, ?)",
                     (CHILD, "completed", json.dumps({krc.METADATA_KEY: [{**HPA_ENTRY, "dependency_kind": "none"}]})))
        conn.commit()
        conn.close()
        result = self.rule(PARENT)[1]
        self.assertIn("- `api-server` (card t_seeded_a): dependency: none.", result)
        self.assertNotIn("queue-worker", result)

    def test_own_entries_and_the_same_inherited_ones_render_once(self):
        result = self.rule(PARENT, [HPA_ENTRY])[1]
        self.assertEqual(result.count("`api-server`"), 1)
        self.assertIn("- `api-server`: dependency: HPA.", result)
        self.assertIn(f"- `queue-worker` (card {CHILD})", result)

    def test_the_cap_drops_not_on_cluster_and_none_first_and_says_so(self):
        gone = {"workload": "x", "dependency_kind": "not_on_cluster", "dependency": "Not here."}
        legs = [{**gone, "workload": f"gone{i}"} for i in range(krc.MAX_SHOWN)]
        self.card("t_aaaa", body=SKILL_BODY, creator=PARENT, stored=legs)
        self.card("t_zzzz", body=SKILL_BODY, creator=PARENT, stored=[{**HPA_ENTRY, "workload": "late-hpa"}])
        with self.assertLogs(krc.logger, "WARNING"):
            result = self.rule(PARENT)[1]
        self.assertIn("`late-hpa` (card t_zzzz): dependency: HPA.", result)
        self.assertIn("`queue-worker`", result)
        self.assertEqual(result.count(": dependency: "), krc.MAX_SHOWN)
        self.assertIn("- 3 more entries not shown;", result)

    def test_the_shown_entries_are_capped(self):
        many = [{**HPA_ENTRY, "workload": f"w{i}"} for i in range(krc.MAX_SHOWN + 2)]
        self.card("t_many", body=SKILL_BODY, creator=PARENT, stored=many)
        with self.assertLogs(krc.logger, "WARNING"):
            self.assertEqual(self.rule(PARENT)[1].count(": dependency: "), krc.MAX_SHOWN)

    def test_an_invalid_stored_entry_is_not_rendered(self):
        self.card("t_odd", body=SKILL_BODY, creator=PARENT, stored=[{"workload": "odd", "dependency_kind": "maybe", "dependency": "?"}])
        self.assertNotIn("odd", self.rule(PARENT)[1])


CUT_ASK = (
    "Find the `inference-server` deployment and the `ledger-writer` deployment across the fleet.\n\n"
    "- Recommend new lower CPU and memory requests for each.\n\n"
    f"{krc.FORMAT_MARKER}\n\nPut the full answer in `result`."
)


class PlatformCutRequestTest(RuleTestCase):
    def test_the_marker_matches_the_report_format_patch(self):
        import kanban_report_format

        self.assertEqual(krc.FORMAT_MARKER, kanban_report_format.FORMAT_MARKER)

    def test_requests_that_ask_for_a_cut(self):
        for ask in (
            "Lower the CPU and memory requests of the inference-server deployment.",
            "Recommend new lower CPU and memory requests for each.",
            "Propose resource request reductions for inference-server.",
            "Can we reduce the memory limit on api-server?",
            "Shrink the pool's machine type to e2-standard-2.",
        ):
            self.assertTrue(krc.asks_for_cut("", ask), ask)
        self.assertTrue(krc.asks_for_cut("Calculate resource request reductions for api-server",
                                         "Propose new CPU and memory requests for each."))

    def test_requests_that_do_not(self):
        for ask in (
            "Perform a cost optimization analysis of the fleet. Don't change anything.",
            "Raise the memory limit of payments-api; it is OOMKilled.",
            "Why is checkout throttled? Its CPU limit looks too low.",
            "Reduce cost where you can.",
            "Cut a pull request that bumps the chart.",
            "Investigate why checkout sees reduced requests since Monday.",
            "We are hitting reduced API limits on the ingress.",
            "Lower the request latency of checkout.",
            f"Why is payments-api crashlooping?\n\n{krc.FORMAT_MARKER}\n\nLower the requests if needed.",
            "A report of cost opportunities (e.g., underutilized nodes, right-sizing recommendations, idle resources).",
        ):
            self.assertFalse(krc.asks_for_cut("Fleet-wide cost optimization analysis", ask), ask)

    def test_a_platform_card_that_checked_nothing_is_refused_once(self):
        self.card(PARENT, body=CUT_ASK, assignee="platform")
        [reason] = self.rule(PARENT, result="**No cut is safe.**")[0]
        self.assertIn("asks to cut requests or limits", reason)
        self.assertIn("not_on_cluster", reason)
        with self.assertLogs(krc.logger, "WARNING"):
            self.assertEqual(self.rule(PARENT, result="**No cut is safe.**", retry=True)[0], [])

    def test_a_platform_card_with_its_own_entries_passes(self):
        self.card(PARENT, body=CUT_ASK, assignee="platform")
        reasons, result, _ = self.rule(PARENT, [HPA_ENTRY])
        self.assertEqual(reasons, [])
        self.assertIn("- `api-server`: dependency: HPA.", result)

    def test_a_platform_card_with_a_childs_entries_passes(self):
        self.card(PARENT, body=CUT_ASK, assignee="Platform")
        self.card(CHILD, body=SKILL_BODY, creator=PARENT, stored=[HPA_ENTRY])
        self.assertEqual(self.rule(PARENT)[0], [])

    def test_a_platform_card_that_delegated_is_not_refused_without_entries(self):
        self.card(PARENT, body=CUT_ASK, assignee="platform")
        self.card("t_whereis", body="Is inference-server on this cluster?", creator=PARENT, stored=[])
        self.assertEqual(self.rule(PARENT, result="**inference-server runs on no cluster.**")[0], [])

    def test_a_card_naming_the_skill_with_a_childs_entries_is_not_refused(self):
        self.card(PARENT, body=f"Run {krc.SKILL_NAME} on api-server.", assignee="platform")
        self.card(CHILD, body=SKILL_BODY, creator=PARENT, stored=[HPA_ENTRY])
        reasons, result, _ = self.rule(PARENT)
        self.assertEqual(reasons, [])
        self.assertIn(f"- `api-server` (card {CHILD}): dependency: HPA.", result)

    def test_a_cluster_card_with_the_same_words_is_not_refused(self):
        self.card(CHILD, body=CUT_ASK)
        self.assertEqual(self.rule(CHILD)[0], [])


class SharedRefusalTest(RuleTestCase):
    def test_the_gate_refuses_once_then_accepts(self):
        self.card(CHILD, body=SKILL_BODY)
        err, result, _ = kvc.apply_completion_rules(CHILD, {}, "No figures.", None, connect=self.connect)
        self.assertIn(krc.PARAM, err)
        self.assertEqual(result, "No figures.")
        with self.assertLogs(krc.logger, "WARNING"):
            err, result, _ = kvc.apply_completion_rules(CHILD, {}, "No figures.", None, connect=self.connect)
        self.assertIsNone(err)
        self.assertEqual(result, "No figures.")

    def test_a_valid_completion_passes_the_whole_gate(self):
        self.card(CHILD, body=SKILL_BODY)
        err, result, metadata = kvc.apply_completion_rules(CHILD, {krc.PARAM: [HPA_ENTRY]}, "**Hold.**", {}, connect=self.connect)
        self.assertIsNone(err)
        self.assertIn(krc.HEADING, result)
        self.assertEqual(metadata[krc.METADATA_KEY][0]["dependency_kind"], "hpa")


class ApplierTest(unittest.TestCase):
    def kanban_tools(self, text):
        root = pathlib.Path(tempfile.mkdtemp())
        path = root / KANBAN_RELATIVE
        path.parent.mkdir(parents=True)
        path.write_text(text)
        return root, path

    def test_the_import_and_schema_call_follow_the_verified_claims_preamble(self):
        root, path = self.kanban_tools(f"KANBAN_COMPLETE_SCHEMA = {{}}\n{ANCHOR}_TOOLS = []\n")
        apply(root)
        patched = path.read_text()
        self.assertLess(patched.index(ANCHOR), patched.index("from tools import kanban_rightsizing_checks"))
        self.assertIn("_kanban_rightsizing_checks.apply_schema(KANBAN_COMPLETE_SCHEMA)", patched)
        self.assertIn(MARKER, patched)

    def test_a_second_run_is_refused(self):
        root, _ = self.kanban_tools(f"KANBAN_COMPLETE_SCHEMA = {{}}\n{ANCHOR}_TOOLS = []\n")
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_file_without_the_verified_claims_patch_is_refused(self):
        root, _ = self.kanban_tools("_TOOLS = []\n")
        with self.assertRaises(SystemExit):
            apply(root)


if __name__ == "__main__":
    unittest.main()
