"""Regression tests for shared admin-console UI helpers."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from admin_console import ui


class PaginatedSelectableTableTest(unittest.TestCase):
    def test_selected_row_wins_over_a_stale_page_query(self):
        rows = [{"id": index} for index in range(30)]
        row_ids = [f"id-{index}" for index in range(30)]
        status = MagicMock()
        previous = MagicMock()
        following = MagicMock()
        previous.button.return_value = False
        following.button.return_value = False
        streamlit = SimpleNamespace(
            query_params={"page": "2"},
            columns=MagicMock(return_value=(status, previous, following)),
        )

        with (
            patch.object(ui, "st", streamlit),
            patch.object(
                ui,
                "selectable_table",
                return_value=("id-0", object()),
            ) as selectable,
        ):
            selected, _ = ui.paginated_selectable_table(
                rows,
                row_ids,
                "id-0",
                key_prefix="test",
                page_query="page",
                selection_query="selected",
            )

        self.assertEqual(selected, "id-0")
        self.assertEqual(streamlit.query_params["page"], "1")
        self.assertEqual(selectable.call_args.args[0], rows[:25])
        self.assertEqual(selectable.call_args.args[1], row_ids[:25])


if __name__ == "__main__":
    unittest.main()
