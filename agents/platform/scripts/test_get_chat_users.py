"""Unit tests for the observability skill's active-chat-users listing.

Run: python3 -m unittest agents/platform/scripts/test_get_chat_users.py

The script mines Cloud Logging entries for `User=<email>` markers and counts
messages per user. The tests feed recorded entry shapes — textPayload, the
jsonPayload fallback, and payloads with no marker — and pin the exact counts.
"""

import json
import sys
import unittest
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from observability_script_harness import run_script  # noqa: E402

SCRIPT = "get_chat_users.py"
ARGS = ["--project-id", "proj-1"]
ENTRIES_URL = "entries:list"


def text_entry(email):
    return {"textPayload": f"Logging incoming GChat event User={email} space=..."}


class TestGetChatUsers(unittest.TestCase):
    def test_a_recorded_payload_counts_messages_per_user(self):
        payload = {"entries": [
            text_entry("alice@example.com"),
            text_entry("bob@example.com"),
            text_entry("alice@example.com"),
        ]}
        code, out = run_script(SCRIPT, ARGS, [(ENTRIES_URL, payload)])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["active_chat_users"], {
            "alice@example.com": 2,
            "bob@example.com": 1,
        })
        self.assertEqual(result["time_window_hours"], 24)

    def test_users_are_sorted_by_email_for_a_stable_report(self):
        payload = {"entries": [
            text_entry("zoe@example.com"),
            text_entry("ann@example.com"),
        ]}
        code, out = run_script(SCRIPT, ARGS, [(ENTRIES_URL, payload)])
        self.assertEqual(code, 0)
        emails = list(json.loads(out)["active_chat_users"].keys())
        self.assertEqual(emails, ["ann@example.com", "zoe@example.com"])

    def test_the_json_payload_fallback_still_finds_the_user(self):
        payload = {"entries": [
            {"jsonPayload": {"log": "Logging incoming GChat event User=carol@x.io"}},
        ]}
        code, out = run_script(SCRIPT, ARGS, [(ENTRIES_URL, payload)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["active_chat_users"], {"carol@x.io": 1})

    def test_entries_without_a_user_marker_are_not_counted(self):
        payload = {"entries": [
            {"textPayload": "Logging incoming GChat event with no user field"},
            text_entry("dan@example.com"),
        ]}
        code, out = run_script(SCRIPT, ARGS, [(ENTRIES_URL, payload)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["active_chat_users"], {"dan@example.com": 1})

    def test_no_entries_reports_an_empty_mapping_not_a_fabricated_zero(self):
        code, out = run_script(SCRIPT, ARGS, [(ENTRIES_URL, {})])
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["active_chat_users"], {})
        self.assertEqual(result["time_window_hours"], 24)

    def test_a_logging_api_error_exits_nonzero_rather_than_reporting_nobody(self):
        err = urllib.error.HTTPError("url", 403, "denied", {}, None)
        err.read = lambda: b"permission denied"
        code, out = run_script(SCRIPT, ARGS, [(ENTRIES_URL, err)])
        self.assertEqual(code, 1)
        self.assertIn("HTTP Error 403", out)
        self.assertNotIn("active_chat_users", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
