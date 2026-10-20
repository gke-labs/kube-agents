"""The outcome copy ``verify_chat_relay.py`` checks Hermes against is the one the first report reads.

The verifier runs in the ``agent-base`` stage, before ``agents/chat/scripts`` is in the image, so it
cannot import ``bootstrap_delivery``. It keeps a copy of ``DELIVERY_OUTCOME_CLASS``, of the ledger path,
and of the run status and two of the ledger columns the note reads; this keeps them equal.
"""

from __future__ import annotations

import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
# bootstrap_delivery imports chat_notify and sandbox_exec, which the image copies beside it from
# agents/platform/scripts.
for directory in (
    REPO / "deploy" / "docker" / "plugins",
    REPO / "agents" / "chat" / "scripts",
    REPO / "agents" / "platform" / "scripts",
):
    if str(directory) not in sys.path:
        sys.path.append(str(directory))

import bootstrap_delivery  # noqa: E402
import oobe  # noqa: E402
import verify_chat_relay  # noqa: E402


class OutcomeCopyTest(unittest.TestCase):
    def test_the_copy_equals_the_classes_the_note_reads(self):
        note = {
            outcome: {
                bootstrap_delivery.OUTCOME_LOST: verify_chat_relay.LOST,
                bootstrap_delivery.OUTCOME_SILENT: verify_chat_relay.SILENT,
                bootstrap_delivery.OUTCOME_TRIED: verify_chat_relay.TRIED,
            }[cls]
            for outcome, cls in bootstrap_delivery.DELIVERY_OUTCOME_CLASS.items()
        }
        self.assertEqual(verify_chat_relay.DELIVERY_OUTCOME_CLASS, note)

    def test_the_checked_columns_and_status_are_the_ones_the_note_reads(self):
        self.assertIn(bootstrap_delivery.DELIVERY_OUTCOME_COLUMN, verify_chat_relay.NOTE_LEDGER_COLUMNS)
        self.assertIn(oobe.FINISHED_AT_COLUMN, verify_chat_relay.NOTE_LEDGER_COLUMNS)
        self.assertEqual(verify_chat_relay.COMPLETED_STATUS, oobe.COMPLETED_STATUS)

    def test_the_note_reads_the_ledger_the_verifier_checks(self):
        # The Platform Agent's HERMES_HOME is <data>/profiles/platform.
        self.assertEqual((oobe.PROFILES_DIR, oobe.PLATFORM_PROFILE), ("profiles", "platform"))
        self.assertEqual((oobe.CRON_DIR, oobe.EXECUTIONS_DB), verify_chat_relay.LEDGER_PARTS)
        self.assertEqual(verify_chat_relay.DELIVERY_FAILED_VALUE, bootstrap_delivery.DELIVERY_FAILED)


class WrittenOutcomesTest(unittest.TestCase):
    def _walk(self, source: str) -> set:
        with tempfile.TemporaryDirectory() as cron:
            with open(os.path.join(cron, "scheduler.py"), "w", encoding="utf-8") as fh:
                fh.write(textwrap.dedent(source))
            return verify_chat_relay.written_outcomes(cron)

    def test_literals_written_straight_to_the_outcome_are_found(self):
        written = self._walk(
            """
            def _classify(error):
                if error:
                    return "failed"
                return "delivered"

            def _crash():
                return None, "suppressed_acked"

            def run(d, error):
                delivery_outcome = _classify(error)
                err, delivery_outcome = _crash()
                d.delivery_outcome = "queued" if d.waiting else None
                delivery_outcome: str = "suppressed"
                finish_execution(1, delivery_outcome="not_configured")
            """
        )
        self.assertEqual(
            written, {"failed", "delivered", "suppressed_acked", "queued", None, "suppressed", "not_configured"}
        )

    def test_a_value_behind_a_named_constant_is_not_resolved(self):
        # The gap the canary covers: hoisting "failed" into a constant removes it from the walk,
        # and check 13 then fails on the walker rather than passing.
        written = self._walk(
            """
            FAILED = "failed"

            def run(error):
                delivery_outcome = FAILED if error else "delivered"
            """
        )
        self.assertEqual(written, {"delivered"})


if __name__ == "__main__":
    unittest.main()
