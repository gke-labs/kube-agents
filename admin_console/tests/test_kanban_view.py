from __future__ import annotations

import unittest
from datetime import UTC, datetime

from admin_console.agent_runtime import KanbanTaskSummary
from admin_console.kanban_view import (
    display_status,
    matches_status,
    status_filter_options,
    waits_on_person,
)


def card(
    status: str, *, block_kind: str = "", gave_up: bool = False, has_pr: bool = False
) -> KanbanTaskSummary:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    return KanbanTaskSummary(
        "t_12345678",
        "Card",
        "platform",
        status,
        0,
        now,
        now,
        "",
        1,
        0,
        0,
        "",
        "",
        block_kind=block_kind,
        gave_up=gave_up,
        has_pr=has_pr,
    )


class DisplayStatusTest(unittest.TestCase):
    def test_each_card_state_has_its_own_label(self):
        cases = [
            (card("blocked", gave_up=True), "failed after all attempts"),
            (card("blocked", block_kind="needs_input"), "blocked · needs input"),
            (card("blocked", block_kind="capability"), "blocked · missing capability"),
            (card("blocked", block_kind="transient"), "blocked · temporary failure"),
            (card("blocked"), "blocked"),
            (card("blocked", block_kind="future_kind"), "blocked"),
            (card("triage"), "triage"),
            (card("done", has_pr=True), "done · with a pull request"),
            (card("done"), "done"),
            (card("running"), "running"),
        ]
        for task, label in cases:
            with self.subTest(label=label, block_kind=task.block_kind):
                self.assertEqual(display_status(task), label)

    def test_gave_up_wins_over_a_leftover_block_kind(self):
        for kind in ("needs_input", "capability", "transient"):
            with self.subTest(kind=kind):
                task = card("blocked", block_kind=kind, gave_up=True)
                self.assertEqual(display_status(task), "failed after all attempts")

    def test_only_blocked_and_triage_cards_wait_on_a_person(self):
        self.assertTrue(waits_on_person(card("blocked", block_kind="needs_input")))
        self.assertTrue(waits_on_person(card("triage")))
        self.assertFalse(
            waits_on_person(card("blocked", block_kind="needs_input", gave_up=True))
        )
        self.assertFalse(waits_on_person(card("done")))
        self.assertFalse(waits_on_person(card("running")))


class StatusFilterTest(unittest.TestCase):
    def test_a_raw_status_selects_all_its_labels(self):
        tasks = [
            card("done"),
            card("done", has_pr=True),
            card("blocked", block_kind="needs_input"),
            card("blocked", block_kind="capability", gave_up=True),
            card("running"),
        ]

        self.assertEqual(
            status_filter_options(tasks),
            [
                "blocked",
                "blocked · needs input",
                "done",
                "done · with a pull request",
                "failed after all attempts",
                "running",
            ],
        )
        selected = {
            option: [
                index
                for index, task in enumerate(tasks)
                if matches_status(task, option)
            ]
            for option in status_filter_options(tasks)
        }
        self.assertEqual(selected["done"], [0, 1])
        self.assertEqual(selected["done · with a pull request"], [1])
        self.assertEqual(selected["blocked"], [2, 3])
        self.assertEqual(selected["blocked · needs input"], [2])
        self.assertEqual(selected["failed after all attempts"], [3])
        self.assertEqual(selected["running"], [4])


if __name__ == "__main__":
    unittest.main()
