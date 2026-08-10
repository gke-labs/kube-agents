"""Regression tests for admin-console visualizations."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime

from admin_console.charts import activity_over_time
from admin_console.domain import ActivityEvent, TriggerKind


def _event(event_id: str, occurred_at: datetime) -> ActivityEvent:
    return ActivityEvent(
        event_id=event_id,
        occurred_at=occurred_at,
        interaction_id="interaction-1",
        trigger_kind=TriggerKind.HUMAN,
        action_type="message",
        action_name="Investigate",
        status="completed",
        summary="Investigate the cluster.",
        agent_name="platform",
    )


class ActivityOverTimeTest(unittest.TestCase):
    def test_groups_events_into_exact_fifteen_minute_buckets(self):
        figure = activity_over_time(
            [
                _event("event-1", datetime(2026, 8, 10, 10, 7, 23, 451000, UTC)),
                _event("event-2", datetime(2026, 8, 10, 10, 12, 47, 2000, UTC)),
                _event("event-3", datetime(2026, 8, 10, 10, 15, 1, 3, UTC)),
            ]
        )

        self.assertEqual(len(figure.data), 1)
        self.assertEqual(list(figure.data[0].y), [2, 1])
        self.assertEqual(
            list(figure.data[0].x),
            [
                datetime(2026, 8, 10, 10, 0, tzinfo=UTC),
                datetime(2026, 8, 10, 10, 15, tzinfo=UTC),
            ],
        )


if __name__ == "__main__":
    unittest.main()
