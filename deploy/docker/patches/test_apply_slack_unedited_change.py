"""Unit tests for apply_slack_unedited_change.py and its verifier.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The applier's contract against a miniature adapter.py carrying upstream's
_normalize_changed_message, and the verifier against the same stub patched and
unpatched. The unpatched stub is the regression: with an empty processed-ts map
(a restarted gateway) it routes a thread parent's metadata change as the
parent's @mention again, and the verifier refuses it.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verify_slack_unedited_change as verify
from apply_slack_unedited_change import GUARD_ANCHOR, MARKER, RELATIVE, apply

# _normalize_changed_message as it stands at v2026.9.14, verbatim.
ADAPTER_STUB = '''from typing import Optional


class SlackAdapter:
    def _normalize_changed_message(self, event: dict) -> Optional[dict]:
        """Turn a ``message_changed`` envelope into a plain message event.
        None if malformed or the original was already routed to the agent. The edit's own ts rides
        along as ``_slack_changed_event_ts`` for dedup."""
        updated_message = event.get("message")
        if not isinstance(updated_message, dict):
            return None
        original_message_ts = str(updated_message.get("ts") or "")
        if original_message_ts and original_message_ts in self._processed_message_ts:
            return None
        edited = updated_message.get("edited")
        edited_ts = str(edited.get("ts") or "") if isinstance(edited, dict) else ""
        outer_event_ts = str(event.get("ts") or "")
        changed_event_ts = (
            str(event.get("event_ts") or edited_ts or "")
            or (outer_event_ts if outer_event_ts != original_message_ts else "")
            or (f"{original_message_ts}:changed" if original_message_ts else ""))
        normalized_event = dict(updated_message)
        for key in ("channel", "channel_type", "team", "team_id"):
            if not normalized_event.get(key) and event.get(key):
                normalized_event[key] = event.get(key)
        if changed_event_ts:
            normalized_event["_slack_changed_event_ts"] = changed_event_ts
        return normalized_event
'''


def stage(source=ADAPTER_STUB):
    root = Path(tempfile.mkdtemp())
    path = root / RELATIVE
    path.parent.mkdir(parents=True)
    path.write_text(source)
    return root


def run_verifier(root):
    with mock.patch.object(verify, "HERMES", root), mock.patch.object(verify, "FAILURES", []):
        rc = verify.main()
        return rc, list(verify.FAILURES)


class ApplierTest(unittest.TestCase):
    def test_the_guard_lands_after_the_processed_ts_check(self):
        root = stage()
        apply(root)
        patched = (root / RELATIVE).read_text()
        self.assertIn(GUARD_ANCHOR + "        # " + MARKER, patched)
        self.assertIn('_kage_before.get("ts") == _kage_edited.get("ts")', patched)

    def test_a_second_apply_is_refused(self):
        root = stage()
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_moved_anchor_is_refused(self):
        root = stage(ADAPTER_STUB.replace("in self._processed_message_ts:", "in self._seen:"))
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertNotIn(MARKER, (root / RELATIVE).read_text())


class VerifierTest(unittest.TestCase):
    def test_a_patched_adapter_drops_the_metadata_change_and_keeps_the_edit(self):
        root = stage()
        apply(root)
        self.assertEqual(run_verifier(root), (0, []))

    def test_the_unpatched_adapter_replays_the_opening_ask(self):
        rc, failures = run_verifier(stage())
        self.assertEqual(rc, 1)
        self.assertTrue(any("metadata change" in f for f in failures), failures)

    def test_a_presence_only_guard_is_refused_for_an_earlier_edit(self):
        # Slack keeps `edited` once set, so presence alone lets a once-edited parent replay.
        root = stage()
        apply(root)
        path = root / RELATIVE
        path.write_text(path.read_text().replace(
            'isinstance(_kage_before, dict) and _kage_before.get("ts") == _kage_edited.get("ts")', "False"))
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertTrue(any("parent edited earlier" in f for f in failures), failures)

    def test_a_moved_method_is_reported_not_raised(self):
        root = stage(ADAPTER_STUB.replace("def _normalize_changed_message", "def _normalize_change"))
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertTrue(any("the method moved" in f for f in failures), failures)


if __name__ == "__main__":
    unittest.main()
