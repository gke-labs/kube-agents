"""Tests for slack_status, the plan and session renderer."""

import ast
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

import slack_status as s

#: Slack's cap on a message's text, and how long a title of one that size may take.
SLACK_MESSAGE_MAX = 40_000
LINEAR_BUDGET_SECONDS = 1.0


def _row(task_id="t_a", title="check payments", lines=(), status=s.TASK_RUNNING, **extra):
    # As the runtime keeps a row: every line a note unless said otherwise, the last one current.
    extra.setdefault("note", lines[-1] if lines else "")
    extra.setdefault("steps", len(lines))
    return SimpleNamespace(task_id=task_id, title=title, lines=list(lines), status=status, **extra)


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
        self.assertEqual(s.task_status("unblocked"), "in_progress")
        self.assertEqual(s.task_status("gave_up"), "error")
        self.assertIsNone(s.task_status("archived"))
        self.assertIsNone(s.task_status("status"))

    def test_retried_kinds_leave_the_row_and_a_block_loop_waits(self):
        # As slack_presenter reads them: the dispatcher retries a crash or a timeout.
        self.assertIsNone(s.task_status("crashed"))
        self.assertIsNone(s.task_status("timed_out"))
        self.assertEqual(s.task_status("block_loop_detected"), "pending")


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


class RowTitleTest(unittest.TestCase):
    def test_a_running_card_shows_its_latest_note(self):
        self.assertEqual(s.row_title(_row(lines=["reading pod state in seeded-a"])), "reading pod state in seeded-a")

    def test_a_step_count_once_there_is_more_than_one(self):
        row = _row(lines=["found it on seeded-a", "reading pod state in seeded-a"])
        self.assertEqual(s.row_title(row), "reading pod state in seeded-a · step 2 ▸")

    def test_the_count_goes_past_the_steps_kept(self):
        row = _row(lines=[f"n{i}" for i in range(s.STEPS_MAX)], steps=9)
        self.assertEqual(s.row_title(row), "n5 · step 9 ▸")

    def test_a_completed_card_shows_its_result(self):
        row = _row(lines=["reading version"], status=s.TASK_COMPLETE, result="seeded-a · 1.33.4 = default")
        self.assertEqual(s.row_title(row), "seeded-a · 1.33.4 = default")

    def test_a_completed_card_with_no_result_shows_its_title(self):
        # Not its last note: "reading version" beside a tick would claim the reading was the result.
        self.assertEqual(s.row_title(_row(lines=["reading version"], status=s.TASK_COMPLETE)), "check payments")

    def test_a_pending_card_is_waiting_on_you(self):
        self.assertEqual(s.row_title(_row(lines=["found two"], status=s.TASK_PENDING)), "waiting on you")

    def test_a_failed_card_shows_where_it_stopped_with_no_count(self):
        self.assertEqual(s.row_title(_row(lines=["a", "Archived"], status=s.TASK_ERROR)), "Archived")

    def test_the_rows_note_not_its_last_line_is_where_the_card_is(self):
        # The runtime says which line is the note, so neither a move nor a note's wording decides it.
        row = _row(lines=["reading logs", "→ todo"], steps=1, note="reading logs")
        self.assertEqual(s.row_title(row), "reading logs")
        self.assertEqual(s.row_title(_row(lines=["→ ready"], status=s.TASK_ERROR, note="")), "check payments")
        row = _row(lines=["draining nodes", "→ rolling back"], steps=2, note="→ rolling back")
        self.assertEqual(s.row_title(row), "→ rolling back · step 2 ▸")

    def test_no_notes_shows_the_title_or_the_id(self):
        self.assertEqual(s.row_title(_row()), "check payments")
        self.assertEqual(s.row_title(_row(title=" ")), "t_a")

    def test_several_rows_lead_with_the_cards_title(self):
        rows = [
            _row("t_a", "seeded-a", status=s.TASK_COMPLETE, result="1.33.4 = default"),
            _row("t_b", "seeded-b", status=s.TASK_PENDING),
            _row("t_c", "seeded-c", lines=["reading version"]),
        ]
        tasks = s.plan_blocks(None, rows)[0]["tasks"]
        self.assertEqual(
            [t["title"] for t in tasks],
            ["seeded-a · 1.33.4 = default", "seeded-b · waiting on you", "seeded-c · reading version"],
        )

    def test_a_clipped_note_keeps_its_count(self):
        title = s.row_title(_row(lines=["x", "word " * 100]))
        self.assertLessEqual(len(title), s.ROW_TITLE_MAX)
        self.assertTrue(title.endswith(s.ELLIPSIS + " · step 2 ▸"), title)

    def test_a_long_note_that_reads_like_a_count_clips_as_a_note(self):
        row = _row(lines=["step 2 of the rollout: " + "x" * 300], steps=1, note="step 2 of the rollout: " + "x" * 300)
        title = s.row_title(row, several=True)
        self.assertLessEqual(len(title), s.ROW_TITLE_MAX)
        self.assertTrue(title.startswith("check payments · step 2 of the rollout"), title)

    def test_a_long_result_is_clipped(self):
        title = s.row_title(_row(status=s.TASK_COMPLETE, result="word " * 100), several=True)
        self.assertLessEqual(len(title), s.ROW_TITLE_MAX)
        self.assertTrue(title.startswith("check payments · word"))

    def test_the_details_still_carry_the_trail(self):
        task = s.plan_blocks(None, [_row(lines=["a", "b"])])[0]["tasks"][0]
        items = task["details"]["elements"][0]["elements"]
        self.assertEqual([i["elements"][0]["text"] for i in items], ["✓ a", "◌ b"])


class PlanTest(unittest.TestCase):
    def test_the_plan_is_one_block_with_no_stop(self):
        # Stop is deferred: /stop would end the turn and leave the cards running.
        for status in (s.TASK_RUNNING, s.TASK_PENDING):
            blocks = s.plan_blocks(None, [_row(status=status)])
            self.assertEqual([b["type"] for b in blocks], ["plan"])

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
        self.assertEqual(s.plan_text("is it up?", rows), "is it up?\n◌ check payments · b · step 2 ▸\n✗ check seeded-b")

    def test_text_escapes_slack_markup(self):
        # The text field is parsed: an unescaped "<!here>" broadcasts and a "<url|label>" relabels a link.
        rows = [_row(title="R&D: roll back", lines=["see <!here> <https://evil.example|docs>"])]
        self.assertEqual(
            s.plan_text(None, rows),
            "R&amp;D: roll back\n◌ see &lt;!here&gt; &lt;https://evil.example|docs&gt;",
        )
        blocks = s.plan_blocks(None, rows)
        self.assertEqual(blocks[0]["title"], "R&D: roll back", "blocks carry rich text, which Slack does not parse")


class SessionTest(unittest.TestCase):
    def test_status(self):
        self.assertEqual(s.session_status("is thinking..."), s.SESSION_PROCESSING)
        self.assertEqual(s.session_status(""), s.SESSION_CLOSED)
        self.assertEqual(s.session_status("closed"), s.SESSION_CLOSED)

    def test_title(self):
        self.assertEqual(s.session_title("<@U1> is <#C1|prod> ok: <https://a.b/c>"), "is #prod ok")
        self.assertLessEqual(len(s.session_title("x" * 200)), s.TITLE_MAX)

    def test_a_long_word_after_a_short_one_is_cut_not_dropped(self):
        self.assertEqual(s.session_title("Restart " + "n" * 90), "Restart " + "n" * 71 + s.ELLIPSIS)
        self.assertEqual(s.session_title("word " * 40), ("word " * 15).rstrip() + s.ELLIPSIS)

    def test_title_keeps_what_real_markup_gives(self):
        cases = {
            "is <@U123> ok with <#C1|ops> and <https://x.example/a|the doc> or <https://y.example>?": (
                "is ok with #ops and the doc or ?"
            ),
            "check <!here> <!subteam^S1|@oncall> kube-system/coredns: now": "check kube-system\u2215coredns, now",
        }
        for ask, title in cases.items():
            with self.subTest(ask=ask):
                self.assertEqual(s.session_title(ask), title)

    def test_title_stays_linear_on_unclosed_markup(self):
        # It runs on the gateway's event loop: a pattern that rescans to the end from every
        # "<" took 29 s on a 40 KB ask of bare "<", stalling every thread.
        for unit in ("<", "<!", "<@", "<#A|", "<a|"):
            with self.subTest(unit=unit):
                ask = unit * (SLACK_MESSAGE_MAX // len(unit))
                start = time.monotonic()
                s.session_title(ask)
                self.assertLess(time.monotonic() - start, LINEAR_BUDGET_SECONDS)


if __name__ == "__main__":
    unittest.main()
