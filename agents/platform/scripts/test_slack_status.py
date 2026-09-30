"""Tests for slack_status, the plan and session renderer."""

import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_status as s


def _row(task_id="t_a", title="check payments", lines=(), status=s.TASK_RUNNING):
    return SimpleNamespace(task_id=task_id, title=title, lines=list(lines), status=status)


class StandaloneTest(unittest.TestCase):
    def test_imports_nothing_from_the_gateway_or_slack(self):
        tree = ast.parse(Path(s.__file__).read_text())
        roots = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        self.assertEqual(roots - {"__future__", "re", "collections", "typing"}, set())


class TaskStatusTest(unittest.TestCase):
    def test_every_mapped_kind_is_a_block_kit_status(self):
        for kind in s.TASK_STATUS_BY_KIND:
            self.assertIn(s.task_status(kind), s.TASK_STATUSES, kind)

    def test_mapping(self):
        self.assertEqual(s.task_status("heartbeat"), "in_progress")
        self.assertEqual(s.task_status("completed"), "complete")
        self.assertEqual(s.task_status("blocked"), "pending")
        self.assertEqual(s.task_status("gave_up"), "error")
        self.assertIsNone(s.task_status("archived"))


class TaskCardTest(unittest.TestCase):
    def test_shape(self):
        card = s.task_card("t_a", "check payments", ["reading logs", "reading metrics"], s.TASK_RUNNING)
        self.assertEqual(
            {k: card[k] for k in ("type", "task_id", "title", "status")},
            {"type": "task_card", "task_id": "t_a", "title": "check payments", "status": "in_progress"},
        )
        self.assertEqual(card["details"]["type"], "rich_text")
        items = card["details"]["elements"][0]["elements"]
        self.assertEqual([i["elements"][0]["text"] for i in items], ["✓ reading logs", "◌ reading metrics"])

    def test_a_settled_card_has_no_open_step(self):
        card = s.task_card("t_a", "x", ["a", "b"], s.TASK_COMPLETE)
        texts = [i["elements"][0]["text"] for i in card["details"]["elements"][0]["elements"]]
        self.assertTrue(all(t.startswith(s.STEP_DONE) for t in texts))

    def test_no_notes_no_details(self):
        self.assertNotIn("details", s.task_card("t_a", "x", ["", "  "], s.TASK_RUNNING))

    def test_blank_title_falls_back_to_the_id(self):
        self.assertEqual(s.task_card("t_a", " ", [], s.TASK_RUNNING)["title"], "t_a")

    def test_unknown_status_reads_as_running(self):
        self.assertEqual(s.task_card("t_a", "x", [], "weird")["status"], s.TASK_RUNNING)

    def test_only_the_last_steps_are_kept(self):
        card = s.task_card("t_a", "x", [f"n{i}" for i in range(10)], s.TASK_RUNNING)
        items = card["details"]["elements"][0]["elements"]
        self.assertEqual(len(items), s.STEPS_MAX)
        self.assertEqual(items[-1]["elements"][0]["text"], "◌ n9")

    def test_long_text_is_clipped(self):
        card = s.task_card("t_a", "word " * 100, ["word " * 100], s.TASK_RUNNING)
        self.assertLessEqual(len(card["title"]), s.ROW_TITLE_MAX)
        step = card["details"]["elements"][0]["elements"][0]["elements"][0]["text"]
        self.assertLessEqual(len(step), s.STEP_TEXT_MAX + 2)


class PlanTest(unittest.TestCase):
    def test_stop_rides_while_a_row_runs(self):
        blocks = s.plan_blocks(None, [_row()])
        self.assertEqual([b["type"] for b in blocks], ["plan", "actions"])
        self.assertEqual(blocks[1], s.stop_block())
        self.assertEqual(blocks[1]["elements"][0]["action_id"], "kage_stop")

    def test_no_stop_when_nothing_runs_or_stop_is_off(self):
        self.assertEqual(len(s.plan_blocks(None, [_row(status=s.TASK_PENDING)])), 1)
        self.assertEqual(len(s.plan_blocks(None, [_row()], stop=False)), 1)

    def test_title(self):
        self.assertEqual(s.plan_title("is it up?", [_row(), _row("t_b")]), "is it up?")
        self.assertEqual(s.plan_title(None, [_row()]), "check payments")
        self.assertEqual(s.plan_title("", [_row(), _row("t_b")]), "2 cards")

    def test_rows_are_capped_oldest_first(self):
        rows = [_row(f"t_{i}") for i in range(s.ROWS_MAX + 5)]
        tasks = s.plan_blocks(None, rows)[0]["tasks"]
        self.assertEqual(len(tasks), s.ROWS_MAX)
        self.assertEqual(tasks[-1]["task_id"], rows[-1].task_id)

    def test_text(self):
        rows = [_row(lines=["a", "b"]), _row("t_b", "check seeded-b", status=s.TASK_ERROR)]
        self.assertEqual(s.plan_text("is it up?", rows), "is it up?\n◌ check payments · b\n✗ check seeded-b")


class SessionTest(unittest.TestCase):
    def test_status(self):
        self.assertEqual(s.session_status("is thinking..."), s.SESSION_PROCESSING)
        self.assertEqual(s.session_status(""), s.SESSION_CLOSED)
        self.assertEqual(s.session_status("closed"), s.SESSION_CLOSED)

    def test_title(self):
        self.assertEqual(s.session_title("<@U1> is <#C1|prod> ok: <https://a.b/c>"), "is #prod ok")
        self.assertLessEqual(len(s.session_title("x" * 200)), s.TITLE_MAX)


if __name__ == "__main__":
    unittest.main()
