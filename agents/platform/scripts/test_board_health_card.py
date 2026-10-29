"""Tests for board_health_card: the stuck-task card with Retry and Drop on each row.

Run: python3 -m pytest agents/platform/scripts/test_board_health_card.py
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import board_health_card as card_mod
import kanban_board_health as kbh

TEXT = (
    "**2 tasks are stuck and need someone to decide.**\n\n"
    "**Investigate payments-api crash-loop on seeded-a**\nStopped 3 days ago before finishing.\n\n"
    "**Upgrade seeded-b node pool to 1.33**\nPaused 6 days ago."
)


def mock_card(**overrides):
    card = {
        "version": 1,
        "text": TEXT,
        "headline": "2 tasks are stuck and need someone to decide.",
        "rows": [
            {"title": "Investigate payments-api crash-loop on seeded-a", "count": 1,
             "status": "Stopped 3 days ago before finishing.", "held": "", "tasks": ["t_7c2e91ab"]},
            {"title": "Upgrade seeded-b node pool to 1.33", "count": 1, "status": "Paused 6 days ago.",
             "held": "", "tasks": ["t_19d04f3c"]},
        ],
        "more": "",
    }
    card.update(overrides)
    return card


class LoadTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = {"KANBAN_HEALTH_STATE": str(Path(tmp.name) / "state.json")}
        self.path = Path(self.env["KANBAN_HEALTH_STATE"]).with_suffix(".card.json")

    def write(self, card):
        self.path.write_text(json.dumps(card))

    def test_the_card_for_the_report_being_relayed(self):
        self.write(mock_card())
        self.assertEqual(card_mod.load(Path("/unused"), TEXT + "\n", self.env), mock_card())

    def test_a_card_said_with_other_text_is_stale(self):
        self.write(mock_card())
        self.assertIsNone(card_mod.load(Path("/unused"), "**1 task is stuck and needs someone to decide.**", self.env))

    def test_no_file_an_unreadable_one_or_another_version_is_none(self):
        self.assertIsNone(card_mod.load(Path("/unused"), TEXT, self.env))
        self.path.write_text("{not json")
        self.assertIsNone(card_mod.load(Path("/unused"), TEXT, self.env))
        self.write(mock_card(version=2))
        self.assertIsNone(card_mod.load(Path("/unused"), TEXT, self.env))

    def test_a_card_with_no_rows_or_an_untitled_row_is_none(self):
        for bad in (mock_card(rows=[]), mock_card(rows=[{"title": "", "tasks": ["t"]}]), mock_card(headline="")):
            with self.subTest(card=bad):
                self.write(bad)
                self.assertIsNone(card_mod.load(Path("/unused"), TEXT, self.env))

    def test_a_row_the_builders_cannot_lay_out_is_none(self):
        base = mock_card()["rows"][0]
        for bad in ({**base, "count": "two"}, {**base, "count": True}, {**base, "tasks": "t_7c2e91ab"},
                    {**base, "tasks": [1]}, {**base, "status": None}):
            with self.subTest(row=bad):
                self.write(mock_card(rows=[bad]))
                self.assertIsNone(card_mod.load(Path("/unused"), TEXT, self.env))
        self.write(mock_card(more=None))
        self.assertIsNone(card_mod.load(Path("/unused"), TEXT, self.env))

    def test_a_home_given_as_a_string_is_read(self):
        self.write(mock_card())
        self.assertEqual(card_mod.load("/unused", TEXT, self.env), mock_card())
    def test_what_kanban_board_health_writes_is_what_load_reads(self):
        tasks = [
            {"task_id": "t_7c2e91ab", "title": "Investigate payments-api crash-loop on seeded-a", "person": False,
             "age_hours": 77.0, "held": []},
            {"task_id": "t_19d04f3c", "title": "Upgrade seeded-b node pool to 1.33", "person": True,
             "age_hours": 146.0, "held": []},
        ]
        text = "\n".join(kbh.render_stuck_tasks(tasks))
        kbh.save_card(kbh.card_path(Path("/unused"), self.env), kbh.stuck_card(tasks, text))
        self.assertEqual(text, TEXT)
        self.assertEqual(card_mod.load(Path("/unused"), text, self.env), mock_card())


class BlocksTest(unittest.TestCase):
    def test_the_mock_s_card(self):
        blocks, text = card_mod.blocks_from_card(mock_card())
        self.assertEqual(text, TEXT)
        self.assertEqual([b["type"] for b in blocks], ["rich_text", "rich_text", "actions", "rich_text", "actions"])
        self.assertEqual(blocks[0]["elements"][0]["elements"],
                         [{"type": "text", "text": "2 tasks are stuck and need someone to decide.", "style": {"bold": True}}])
        self.assertEqual(blocks[1]["block_id"], "kage_board.text.0")
        self.assertEqual(blocks[1]["elements"][0]["elements"], [
            {"type": "text", "text": "Investigate payments-api crash-loop on seeded-a", "style": {"bold": True}},
            {"type": "text", "text": "\nStopped 3 days ago before finishing."},
        ])
        self.assertEqual(blocks[2], {
            "type": "actions",
            "block_id": "kage_board.row.0",
            "elements": [
                {"type": "button", "text": {"type": "plain_text", "text": "Retry", "emoji": True},
                 "action_id": "kage_board.retry", "value": "t_7c2e91ab"},
                {"type": "button", "text": {"type": "plain_text", "text": "Drop", "emoji": True},
                 "action_id": "kage_board.drop", "value": "t_7c2e91ab"},
            ],
        })
        self.assertEqual(blocks[4]["block_id"], "kage_board.row.1")

    def test_a_grouped_row_holds_every_id_and_its_count(self):
        row = {"title": "Triage x", "count": 2, "status": "Stopped 1 day ago before finishing.",
               "held": "Holding back: Deploy y.", "tasks": ["t_1", "t_2"]}
        blocks, _text = card_mod.blocks_from_card(mock_card(rows=[row], more="And 3 more."))
        self.assertEqual([e["text"] for e in blocks[1]["elements"][0]["elements"]],
                         ["Triage x", " (2 tasks)", "\nStopped 1 day ago before finishing.", "\nHolding back: Deploy y."])
        self.assertEqual(blocks[2]["elements"][0]["value"], "t_1,t_2")
        self.assertEqual(blocks[3]["elements"][0]["elements"], [{"type": "text", "text": "And 3 more."}])

    def test_a_row_past_the_value_cap_gets_no_buttons(self):
        row = {"title": "Triage x", "count": 51, "status": "s", "held": "", "tasks": [f"t_{i}" for i in range(51)]}
        blocks, _text = card_mod.blocks_from_card(mock_card(rows=[row]))
        self.assertEqual([b["type"] for b in blocks], ["rich_text", "rich_text"])

    def test_a_title_is_shown_as_written(self):
        row = {"title": "<!here> fix *it*", "count": 1, "status": "s", "held": "", "tasks": ["t_1"]}
        blocks, _text = card_mod.blocks_from_card(mock_card(rows=[row]))
        self.assertEqual(blocks[1]["type"], "rich_text")
        self.assertEqual(blocks[1]["elements"][0]["elements"][0]["text"], "<!here> fix *it*")


class ChatTest(unittest.TestCase):
    def test_the_mock_s_card_as_a_chat_message(self):
        chat, text = card_mod.chat_from_card(mock_card())
        self.assertEqual(text, TEXT)
        self.assertEqual(chat, {
            "schema": "kube-agents.chat/v1",
            "headline": "2 tasks are stuck and need someone to decide.",
            "rows": [
                {"text": "**Investigate payments-api crash-loop on seeded-a**",
                 "detail": "Stopped 3 days ago before finishing.",
                 "tasks": ["t_7c2e91ab"], "actions": ["retry", "drop"]},
                {"text": "**Upgrade seeded-b node pool to 1.33**", "detail": "Paused 6 days ago.",
                 "tasks": ["t_19d04f3c"], "actions": ["retry", "drop"]},
            ],
        })

    def test_the_count_held_line_and_more(self):
        row = {"title": "Triage x", "count": 2, "status": "Stopped.", "held": "Holding back: Deploy y.",
               "tasks": ["t_1", "t_2"]}
        chat, _text = card_mod.chat_from_card(mock_card(rows=[row], more="And 3 more."))
        self.assertEqual(chat["rows"], [{"text": "**Triage x** (2 tasks)", "detail": "Stopped. Holding back: Deploy y.",
                                         "tasks": ["t_1", "t_2"], "actions": ["retry", "drop"]}])
        self.assertEqual(chat["after"], ["And 3 more."])

    def test_a_row_past_the_cap_carries_no_actions(self):
        row = {"title": "Triage x", "count": 51, "status": "s", "held": "", "tasks": [f"t_{i}" for i in range(51)]}
        chat, _text = card_mod.chat_from_card(mock_card(rows=[row]))
        self.assertNotIn("actions", chat["rows"][0])
        self.assertNotIn("tasks", chat["rows"][0])


if __name__ == "__main__":
    unittest.main()
