from __future__ import annotations

import unittest

from admin_console.tests.activity_fixtures import FixtureTelemetryProvider
from admin_console.domain import AttributionLevel, TriggerKind


class FixtureTelemetryProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.events = FixtureTelemetryProvider().list_activity()

    def test_contains_human_and_autonomous_activity(self) -> None:
        triggers = {event.trigger_kind for event in self.events}
        self.assertIn(TriggerKind.HUMAN, triggers)
        self.assertIn(TriggerKind.CRON, triggers)
        self.assertIn(TriggerKind.EVENT, triggers)

    def test_human_chain_retains_one_interaction(self) -> None:
        chain = [event for event in self.events if event.interaction_id == "int-7f21"]
        self.assertGreaterEqual(len(chain), 5)
        self.assertEqual({"int-7f21"}, {event.interaction_id for event in chain})
        self.assertEqual({"alice@example.com"}, {event.user_id for event in chain})
        self.assertTrue(all(event.trace_id == "7f21a001" for event in chain))

    def test_fixture_exercises_every_attribution_level(self) -> None:
        levels = {event.attribution for event in self.events}
        self.assertEqual(set(AttributionLevel), levels)
if __name__ == "__main__":
    unittest.main()
