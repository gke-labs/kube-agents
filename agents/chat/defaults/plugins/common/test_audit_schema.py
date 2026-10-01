"""The audit envelope: self-describing, stable keys, one timestamp format."""

import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import audit_schema  # noqa: E402

_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


class EnvelopeTest(unittest.TestCase):
    def test_the_envelope_names_the_event_twice_and_stamps_it(self):
        record = audit_schema.envelope("tool_call_end", {"tool": "Bash", "duration_ms": 3})
        self.assertEqual(record["audit_event"], "tool_call_end")
        self.assertEqual(record["event_type"], "tool_call_end")
        self.assertEqual(record["severity"], "INFO")
        self.assertRegex(record["timestamp"], _TIMESTAMP)
        self.assertEqual(record["tool"], "Bash")
        self.assertEqual(record["duration_ms"], 3)

    def test_the_envelope_keys_come_first(self):
        # Sorted output is the emitters' choice; the envelope itself puts its
        # keys ahead of the emitter's so a reader of the raw line sees the kind
        # before the payload.
        record = audit_schema.envelope("x", {"a": 1})
        self.assertEqual(list(record)[:4], ["audit_event", "event_type", "severity", "timestamp"])

    def test_an_emitter_cannot_override_the_envelope(self):
        for key in ("audit_event", "event_type", "severity", "timestamp"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    audit_schema.envelope("x", {key: "y"})

    def test_the_timestamp_is_utc_to_the_millisecond(self):
        moment = datetime(2026, 9, 27, 21, 0, 0, 123456, tzinfo=timezone(timedelta(hours=2)))
        self.assertEqual(audit_schema.iso_timestamp(moment), "2026-09-27T19:00:00.123Z")


if __name__ == "__main__":
    unittest.main()
