"""Unit tests for the rightsizing rule in the kanban_complete gate.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import os
import pathlib
import sqlite3
import tempfile
import unittest

from apply_kanban_rightsizing_checks import ANCHOR, KANBAN_RELATIVE, MARKER, apply

import kanban_rightsizing_checks as krc
import kanban_verified_claims as kvc

PARENT = "t_platform"
CHILD = "t_seeded_a"
SKILL_BODY = "Lower the requests of api-server. Run the `gke-rightsizing-checks` skill first."

GOOD_CARD_RESULT = """**No cut is safe for either workload.**

api-server: an HPA scales it on CPU, so a lower request adds replicas
queue-worker: Guaranteed QoS, so a lower request is also a lower limit

- HPA: `kubectl get hpa -n web` shows api-server at 60% CPU.
- QoS: queue-worker is Guaranteed, requests equal limits.
- Shared: one PDB selects api-server; no PVC.
- Source: meta.helm.sh/release-name=web, so a Helm values file may render other clusters.
- Packing: 940m allocatable per node; nodes needed = max(ceil(1.2/0.94), ceil(2.1/2.8)) = 2.
"""


class RuleTestCase(unittest.TestCase):
    def setUp(self):
        kvc._calls.clear()
        kvc._refused_at.clear()
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT, result TEXT, body TEXT)")
        conn.execute(
            "CREATE TABLE kanban_worker_children (child_id TEXT PRIMARY KEY, creator_id TEXT NOT NULL, created_at INTEGER NOT NULL)"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.path)

    def card(self, task_id, body="", status="running", result=None, creator=None):
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO tasks VALUES (?, ?, ?, ?)", (task_id, status, result, body))
        if creator:
            conn.execute("INSERT INTO kanban_worker_children VALUES (?, ?, 0)", (task_id, creator))
        conn.commit()
        conn.close()

    def connect(self):
        return sqlite3.connect(self.path)

    def rule(self, task_id, result, retry=False, connect="default"):
        return krc.rightsizing_rule(
            task_id=task_id,
            args={},
            result=result,
            metadata={"a": 1},
            calls=[],
            retry=retry,
            redact=lambda value: value,
            connect=self.connect if connect == "default" else connect,
        )


class RegistrationTest(unittest.TestCase):
    def test_importing_the_module_registers_the_rule_once(self):
        self.assertEqual(kvc.RULES.count(krc.rightsizing_rule), 1)


class UntriggeredTest(RuleTestCase):
    def test_a_card_the_skill_was_not_asked_for_passes(self):
        self.card(CHILD, body="Why is payments-api crashlooping?")
        self.assertEqual(self.rule(CHILD, "It is OOMKilled."), ([], "It is OOMKilled.", {"a": 1}))

    def test_no_board_connection_passes(self):
        self.assertEqual(self.rule(CHILD, "anything", connect=None)[0], [])

    def test_a_board_that_cannot_be_read_passes(self):
        def broken():
            raise sqlite3.OperationalError("database is locked")

        with self.assertLogs(krc.logger, "WARNING"):
            self.assertEqual(self.rule(CHILD, "anything", connect=broken)[0], [])


class OwnResultTest(RuleTestCase):
    def setUp(self):
        super().setUp()
        self.card(CHILD, body=SKILL_BODY)

    def test_a_result_with_the_lines_and_the_checks_passes(self):
        self.assertEqual(self.rule(CHILD, GOOD_CARD_RESULT)[0], [])

    def test_no_dependency_line_is_refused(self):
        result = GOOD_CARD_RESULT.replace("api-server: an HPA", "The HPA").replace("queue-worker: Guaranteed", "Guaranteed")
        [reason] = self.rule(CHILD, result)[0]
        self.assertIn("<workload>: <dependency>", reason)

    def test_no_dependency_found_counts_as_a_line(self):
        result = "batch-worker: no dependency found\n\nHPA none. QoS Burstable. No PDB. Helm. 2 nodes needed."
        self.assertEqual(self.rule(CHILD, result)[0], [])

    def test_a_line_below_the_opening_does_not_count(self):
        filler = "\n".join(f"Paragraph {i}." for i in range(krc.OPENING_LINES))
        result = f"{filler}\napi-server: an HPA scales it\nQoS Guaranteed. PDB. Helm. nodes needed."
        [reason] = self.rule(CHILD, result)[0]
        self.assertIn("open `result`", reason)

    def test_a_missing_check_is_named(self):
        result = GOOD_CARD_RESULT.replace("- Packing: 940m allocatable per node; nodes needed = max(ceil(1.2/0.94), ceil(2.1/2.8)) = 2.\n", "")
        [reason] = self.rule(CHILD, result)[0]
        self.assertIn("per-node packing", reason)
        self.assertNotIn("HPA or KEDA", reason)

    def test_markdown_around_the_line_still_counts(self):
        result = GOOD_CARD_RESULT.replace("api-server: an HPA", "- **`api-server`**: an HPA")
        self.assertEqual(self.rule(CHILD, result)[0], [])

    def test_the_retry_is_accepted_unchanged(self):
        with self.assertLogs(krc.logger, "WARNING"):
            self.assertEqual(self.rule(CHILD, "No figures.", retry=True), ([], "No figures.", {"a": 1}))


class ChildLinesTest(RuleTestCase):
    def setUp(self):
        super().setUp()
        self.card(PARENT, body="Lower the requests of api-server and queue-worker.")
        self.card(CHILD, body=SKILL_BODY, status="done", result=GOOD_CARD_RESULT, creator=PARENT)

    def test_a_summary_that_carries_every_line_passes(self):
        summary = (
            "**No cut is safe.**\n\n"
            "- **api-server**: an HPA scales it on CPU, so a lower request adds replicas\n"
            "- `queue-worker: Guaranteed QoS, so a lower request is also a lower limit`\n"
        )
        self.assertEqual(self.rule(PARENT, summary)[0], [])

    def test_a_summary_that_drops_a_line_is_refused_with_the_line(self):
        summary = "**No cut is safe.** api-server: an HPA scales it on CPU, so a lower request adds replicas"
        [reason] = self.rule(PARENT, summary)[0]
        self.assertIn(CHILD, reason)
        self.assertIn("queue-worker: guaranteed qos", reason)

    def test_a_grandchild_counts_too(self):
        self.card("t_grandchild", body=SKILL_BODY, status="done", result="db: no dependency found\nHPA QoS PDB Helm nodes needed", creator=CHILD)
        summary = "api-server: an HPA scales it on CPU, so a lower request adds replicas\nqueue-worker: Guaranteed QoS, so a lower request is also a lower limit"
        [reason] = self.rule(PARENT, summary)[0]
        self.assertIn("t_grandchild", reason)

    def test_a_child_still_running_or_without_the_skill_is_skipped(self):
        self.card("t_running", body=SKILL_BODY, status="running", result="x: y", creator=PARENT)
        self.card("t_other", body="Check pods.", status="done", result="pods: all running", creator=PARENT)
        summary = "api-server: an HPA scales it on CPU, so a lower request adds replicas\nqueue-worker: Guaranteed QoS, so a lower request is also a lower limit"
        self.assertEqual(self.rule(PARENT, summary)[0], [])


class ReviewShapesTest(RuleTestCase):
    """The shapes a correct answer takes that the first matcher refused."""

    def setUp(self):
        super().setUp()
        self.card(PARENT, body="Lower the requests of api-server and queue-worker.")

    def child(self, result, task_id=CHILD):
        self.card(task_id, body=SKILL_BODY, status="done", result=result, creator=PARENT)

    def test_a_bulleted_child_line_carried_without_the_bullet_passes(self):
        self.child("- api-server: an HPA scales it on CPU\n\nHPA QoS PDB Helm nodes needed")
        self.assertEqual(self.rule(PARENT, "**No cut.**\n\napi-server: an HPA scales it on CPU")[0], [])

    def test_a_child_line_carried_in_a_table_row_passes(self):
        self.child("api-server: an HPA scales it on CPU\n\nHPA QoS PDB Helm nodes needed")
        summary = "| Cluster | Finding |\n| --- | --- |\n| seeded-a | api-server: an HPA scales it on CPU |"
        self.assertEqual(self.rule(PARENT, summary)[0], [])

    def test_check_lines_right_under_the_block_are_not_dependency_lines(self):
        self.child("api-server: an HPA scales it on CPU\n- PDB: none selects it\n- Helm: release web\n- Limits: cpu=10m\nQoS nodes needed")
        self.assertEqual(self.rule(PARENT, "api-server: an HPA scales it on CPU")[0], [])

    def test_linked_namespaced_numbered_and_kind_openings_count(self):
        self.card("t_own", body=SKILL_BODY)
        tail = "\n\nHPA: yes. QoS Burstable. PDB none. Helm none. 2 nodes needed."
        for opening in (
            "[inference-server](https://console.example/x): an HPA scales it on CPU",
            "seeded-capacity/inference-server: an HPA scales it on CPU",
            "1. inference-server: an HPA scales it on CPU",
            "+ inference-server: an HPA scales it on CPU",
            "Deployment inference-server: an HPA scales it on CPU",
        ):
            with self.subTest(opening=opening):
                self.assertEqual(self.rule("t_own", opening + tail)[0], [])

    def test_a_status_line_is_not_a_dependency_line(self):
        self.card("t_own", body=SKILL_BODY)
        reasons = self.rule("t_own", "Status: done\n\nHPA QoS PDB Helm nodes needed")[0]
        self.assertTrue(any("open `result`" in r for r in reasons), reasons)

    def test_a_not_found_leg_is_not_refused_and_not_carried(self):
        self.card("t_own", body=SKILL_BODY)
        self.assertEqual(self.rule("t_own", "Neither workload is on seeded-b: the namespaces do not exist.")[0], [])
        self.child("inference-server: not on this cluster")
        self.assertEqual(self.rule(PARENT, "**Found on seeded-a only.**")[0], [])

    def test_checks_named_only_in_the_evidence_section_do_not_count(self):
        self.card("t_own", body=SKILL_BODY)
        result = "api-server: no dependency found\n\n" + kvc.EVIDENCE_HEADING + "\n$ kubectl get hpa,pdb\nqos helm allocatable"
        reasons = self.rule("t_own", result)[0]
        self.assertTrue(any("does not show these checks" in r for r in reasons), reasons)


class BotRoundOneShapesTest(RuleTestCase):
    """The shapes the first bot review of #3507 named."""

    CHECKS_TAIL = "\n\n- HPA or KEDA: see above\n- QoS: shown\n- PDB: none\n- Helm: none\n- Packing: 2 nodes needed"

    def setUp(self):
        super().setUp()
        self.card(PARENT, body="Lower the requests of api-server and queue-worker.")

    def child(self, result, task_id=CHILD):
        self.card(task_id, body=SKILL_BODY, status="done", result=result, creator=PARENT)

    def test_one_workload_per_paragraph_is_one_block(self):
        self.child("api-server: an HPA scales it on CPU\n\nqueue-worker: Guaranteed QoS, so a lower request is a lower limit" + self.CHECKS_TAIL)
        [reason] = self.rule(PARENT, "api-server: an HPA scales it on CPU")[0]
        self.assertIn("queue-worker: guaranteed qos", reason)

    def test_a_bold_opener_shaped_like_a_line_is_skipped(self):
        self.child("**ledger-writer: no safe cut.**\n\nledger-writer: Guaranteed QoS, so a lower request is a lower limit" + self.CHECKS_TAIL)
        self.assertEqual(self.rule(PARENT, "ledger-writer: Guaranteed QoS, so a lower request is a lower limit")[0], [])
        [reason] = self.rule(PARENT, "**ledger-writer: no safe cut.**")[0]
        self.assertIn("ledger-writer: guaranteed qos", reason)

    def test_figures_and_metadata_lines_are_not_dependency_lines(self):
        self.card("t_own", body=SKILL_BODY)
        reasons = self.rule("t_own", "cpu: 400m\nmemory: 64Mi\nseeded-a: both workloads found" + self.CHECKS_TAIL)[0]
        self.assertTrue(any("open `result`" in r for r in reasons), reasons)
        self.child("Namespace: seeded-capacity\nDeployment: inference-server\n\ninference-server: an HPA scales it on CPU" + self.CHECKS_TAIL)
        self.assertEqual(self.rule(PARENT, "inference-server: an HPA scales it on CPU")[0], [])

    def test_a_check_saying_not_found_does_not_exempt_the_card(self):
        self.card("t_own", body=SKILL_BODY)
        reasons = self.rule("t_own", "- HPA: not found\n- KEDA: no such resource type on this cluster")[0]
        self.assertTrue(any("open `result`" in r for r in reasons), reasons)

    def test_a_real_line_with_a_negation_is_carried(self):
        self.child("inference-server: an HPA scales it on CPU; the replicas it wants are not running" + self.CHECKS_TAIL)
        [reason] = self.rule(PARENT, "**No cut.**")[0]
        self.assertIn("inference-server: an hpa", reason)

    def test_a_two_column_table_row_carries_the_line(self):
        self.child("ledger-writer: Guaranteed QoS, so a lower request is a lower limit" + self.CHECKS_TAIL)
        summary = "| Workload | Dependency |\n| --- | --- |\n| ledger-writer | Guaranteed QoS, so a lower request is a lower limit |"
        self.assertEqual(self.rule(PARENT, summary)[0], [])

    def test_plural_check_words_count(self):
        self.card("t_own", body=SKILL_BODY)
        result = ("api-server: no dependency found\n\nNo HPAs or ScaledObjects target either workload. "
                  "QoS Burstable. No PDB. Helm none. Both machine types fit.")
        self.assertEqual(self.rule("t_own", result)[0], [])


class SharedRefusalTest(RuleTestCase):
    def test_the_gate_refuses_once_then_accepts(self):
        self.card(CHILD, body=SKILL_BODY)
        err, result, _ = kvc.apply_completion_rules(CHILD, {}, "No figures.", None, connect=self.connect)
        self.assertIn("gke-rightsizing-checks", err)
        self.assertEqual(result, "No figures.")
        with self.assertLogs(krc.logger, "WARNING"):
            err, result, _ = kvc.apply_completion_rules(CHILD, {}, "No figures.", None, connect=self.connect)
        self.assertIsNone(err)
        self.assertEqual(result, "No figures.")


class ApplierTest(unittest.TestCase):
    def kanban_tools(self, text):
        root = pathlib.Path(tempfile.mkdtemp())
        path = root / KANBAN_RELATIVE
        path.parent.mkdir(parents=True)
        path.write_text(text)
        return root, path

    def test_the_import_follows_the_verified_claims_preamble(self):
        root, path = self.kanban_tools(f"KANBAN_COMPLETE_SCHEMA = {{}}\n{ANCHOR}_TOOLS = []\n")
        apply(root)
        patched = path.read_text()
        self.assertLess(patched.index(ANCHOR), patched.index("import tools.kanban_rightsizing_checks"))
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
