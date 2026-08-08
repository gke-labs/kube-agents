"""Unit tests for the require-result gate installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import copy
import time
import unittest

import kanban_result_required as krr
from kanban_result_required import (
    MISSING_RESULT_ERROR,
    NEW_GATE,
    NEW_RESULT_DESCRIPTION,
    NEW_SUMMARY_DESCRIPTION,
    NEW_TOOL_DESCRIPTION,
    NUDGE_TTL_SECONDS,
    OLD_RESULT_DESCRIPTION,
    OLD_SUMMARY_DESCRIPTION,
    OLD_TOOL_DESCRIPTION,
    apply_schema,
    blank_to_none,
    require_result,
)

# The card that started this: the worker held a 5.5 KB catalogue, called
# kanban_complete with a 169-character status line and no result, and the
# catalogue was never seen again.
INCIDENT_SUMMARY = (
    "Successfully inspected and cataloged all 9 active platform-agent-level and "
    "system-wide cron jobs. Compiled their detailed purposes, schedules, and "
    "active configurations."
)
INCIDENT_RESULT = "\n".join(f"{i}. cron-job-{i} — schedule 0 {i} * * *" for i in range(1, 10))


def _ev_summary(summary, result):
    """``complete_task``'s event-summary expression, copied from upstream.

    ``hermes_cli/kanban_db.py`` runs this inside ``write_txn`` after the UPDATE
    to ``done``, so anything it raises rolls the completion back and reaches the
    worker as ``kanban_complete: list index out of range``.
    """
    ev_summary = (summary if summary is not None else result) or ""
    return ev_summary.strip().splitlines()[0][:400] if ev_summary else ""


def _schema():
    """A minimal stand-in shaped like upstream's KANBAN_COMPLETE_SCHEMA."""
    return copy.deepcopy({
        "name": "kanban_complete",
        "description": OLD_TOOL_DESCRIPTION + "If you created new tasks, list them.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": OLD_SUMMARY_DESCRIPTION},
                "result": {"type": "string", "description": OLD_RESULT_DESCRIPTION},
                "metadata": {"type": "object", "description": "Free-form dict."},
            },
            "required": [],
        },
    })


class RequireResultTest(unittest.TestCase):
    def setUp(self):
        krr._refused_at.clear()
        self.addCleanup(krr._refused_at.clear)

    def _expire(self, task_id):
        """Age the outstanding refusal for ``task_id`` past NUDGE_TTL_SECONDS."""
        krr._refused_at[str(task_id)] = time.monotonic() - NUDGE_TTL_SECONDS - 1

    def test_a_result_passes_through_untouched(self):
        err, out = require_result("t_1", INCIDENT_SUMMARY, INCIDENT_RESULT)
        self.assertIsNone(err)
        self.assertEqual(out, INCIDENT_RESULT)

    def test_a_one_line_result_is_enough(self):
        # No length floor: a card whose honest answer is one line must close.
        err, out = require_result("t_1", "Restarted it.", "3/3 pods ready")
        self.assertIsNone(err)
        self.assertEqual(out, "3/3 pods ready")

    def test_the_incident_completion_is_refused(self):
        err, out = require_result("t_8d1cf5cf", INCIDENT_SUMMARY, None)
        self.assertEqual(err, MISSING_RESULT_ERROR)
        self.assertIsNone(out)

    def test_the_refusal_says_what_to_do(self):
        err, _ = require_result("t_1", INCIDENT_SUMMARY, None)
        self.assertIn("result", err)
        self.assertIn("kanban_complete again", err)
        # The three dead ends the incident workers actually chose.
        for dead_end in ("transcript", "file", "comment"):
            self.assertIn(dead_end, err)

    def test_whitespace_only_result_counts_as_empty(self):
        err, _ = require_result("t_1", INCIDENT_SUMMARY, "   \n\t  ")
        self.assertEqual(err, MISSING_RESULT_ERROR)

    def test_the_retry_carrying_a_result_is_accepted(self):
        require_result("t_1", INCIDENT_SUMMARY, None)
        err, out = require_result("t_1", INCIDENT_SUMMARY, INCIDENT_RESULT)
        self.assertIsNone(err)
        self.assertEqual(out, INCIDENT_RESULT)

    def test_a_card_is_never_wedged_shut(self):
        # Second empty completion for the same task closes the card rather than
        # refusing forever; summary is promoted so something survives.
        self.assertEqual(require_result("t_1", INCIDENT_SUMMARY, None)[0], MISSING_RESULT_ERROR)
        err, out = require_result("t_1", INCIDENT_SUMMARY, None)
        self.assertIsNone(err)
        self.assertEqual(out, INCIDENT_SUMMARY)

    def test_a_totally_empty_second_completion_still_closes(self):
        require_result("t_1", None, None)
        err, out = require_result("t_1", None, None)
        self.assertIsNone(err)
        self.assertIsNone(out)

    def test_each_task_gets_its_own_nudge(self):
        # A shared worker process must not spend task B's nudge on task A.
        self.assertEqual(require_result("t_a", "s", None)[0], MISSING_RESULT_ERROR)
        self.assertEqual(require_result("t_b", "s", None)[0], MISSING_RESULT_ERROR)

    def test_non_string_task_ids_do_not_collide_by_accident(self):
        require_result(1, "s", None)
        # str(1) == "1" — the same key, correctly.
        err, _ = require_result("1", "s", None)
        self.assertIsNone(err)

    def test_a_completed_card_leaves_no_refusal_behind(self):
        # The mapping is the cards still awaiting a retry, not a ledger of every
        # card ever refused. A gateway session completes cards for the life of
        # the pod, so a ledger there is both a leak and a disabled gate.
        require_result("t_1", INCIDENT_SUMMARY, None)
        require_result("t_1", INCIDENT_SUMMARY, INCIDENT_RESULT)
        self.assertEqual(krr._refused_at, {})

    def test_a_card_reopened_after_a_refusal_is_nudged_again(self):
        # The 2026-08-07 regression: an orchestrator refused t_1, the card was
        # reopened and retried, and the second content-free completion was
        # accepted in silence because the process still remembered the refusal.
        require_result("t_1", INCIDENT_SUMMARY, None)
        require_result("t_1", INCIDENT_SUMMARY, INCIDENT_RESULT)
        err, _ = require_result("t_1", INCIDENT_SUMMARY, None)
        self.assertEqual(err, MISSING_RESULT_ERROR)

    def test_a_stale_refusal_belongs_to_a_different_attempt(self):
        # A refusal nobody came back for within NUDGE_TTL_SECONDS is not the
        # first half of this completion, so this completion gets its own nudge.
        require_result("t_1", INCIDENT_SUMMARY, None)
        self._expire("t_1")
        err, _ = require_result("t_1", INCIDENT_SUMMARY, None)
        self.assertEqual(err, MISSING_RESULT_ERROR)

    def test_expiry_delays_the_close_by_one_call_and_no_more(self):
        # Expiry must not reintroduce the wedge. A caller that retries promptly
        # is inside the window on its next call, so the card still closes.
        require_result("t_1", INCIDENT_SUMMARY, None)
        self._expire("t_1")
        require_result("t_1", INCIDENT_SUMMARY, None)
        err, out = require_result("t_1", INCIDENT_SUMMARY, None)
        self.assertIsNone(err)
        self.assertEqual(out, INCIDENT_SUMMARY)

    def test_a_whitespace_only_result_is_never_stored(self):
        # It survived the gate as the promoted value and raised IndexError in
        # complete_task's write_txn, rolling the completion back — with the
        # nudge already spent, so the identical retry failed identically.
        require_result("t_1", None, "   \n\t ")
        err, out = require_result("t_1", None, "   \n\t ")
        self.assertIsNone(err)
        self.assertIsNone(out)
        self.assertEqual(_ev_summary(None, out), "")

    def test_a_whitespace_only_summary_is_never_promoted(self):
        require_result("t_1", " \n ", None)
        err, out = require_result("t_1", " \n ", None)
        self.assertIsNone(err)
        self.assertIsNone(out)

    def test_upstream_would_have_raised_on_the_value_the_gate_now_folds(self):
        # Guards the premise: if complete_task ever stops indexing line zero of
        # a stripped string, this fails and blank_to_none can be reconsidered.
        with self.assertRaises(IndexError):
            _ev_summary(None, "   \n\t ")
        with self.assertRaises(IndexError):
            _ev_summary(" ", INCIDENT_RESULT)


class BlankToNoneTest(unittest.TestCase):
    def test_content_is_returned_byte_for_byte(self):
        # kanban_complete has already redacted these; trimming them here would
        # silently edit what redact_sensitive_text produced.
        for value in (INCIDENT_RESULT, " leading and trailing ", "x"):
            self.assertEqual(blank_to_none(value), value)

    def test_every_shape_of_empty_becomes_none(self):
        for value in (None, "", "   ", "\n", "\t \r\n"):
            self.assertIsNone(blank_to_none(value))

    def test_the_gate_folds_the_summary_the_handler_passes_on(self):
        # require_result only owns `result`; the handler's own `summary` local
        # goes straight to kb.complete_task, so the gate has to fold it too.
        self.assertIn("summary = _blank_to_none(summary)", NEW_GATE)
        self.assertIn("_require_result(tid, summary, result)", NEW_GATE)


class ApplySchemaTest(unittest.TestCase):
    def test_all_three_descriptions_are_rewritten(self):
        schema = apply_schema(_schema())
        props = schema["parameters"]["properties"]
        self.assertIn(NEW_TOOL_DESCRIPTION, schema["description"])
        self.assertEqual(props["summary"]["description"], NEW_SUMMARY_DESCRIPTION)
        self.assertEqual(props["result"]["description"], NEW_RESULT_DESCRIPTION)

    def test_the_legacy_framing_is_gone(self):
        schema = apply_schema(_schema())
        blob = repr(schema)
        # The exact wording that told workers to throw the answer away.
        self.assertNotIn("legacy field", blob)
        self.assertNotIn("Use ``summary`` instead", blob)
        self.assertNotIn("1-3 sentence", blob)

    def test_the_tool_description_tail_is_preserved(self):
        schema = apply_schema(_schema())
        self.assertIn("If you created new tasks, list them.", schema["description"])

    def test_result_becomes_a_required_parameter(self):
        schema = apply_schema(_schema())
        self.assertIn("result", schema["parameters"]["required"])

    def test_required_is_not_double_appended(self):
        schema = _schema()
        apply_schema(schema)
        schema["description"] = OLD_TOOL_DESCRIPTION
        schema["parameters"]["properties"]["summary"]["description"] = OLD_SUMMARY_DESCRIPTION
        schema["parameters"]["properties"]["result"]["description"] = OLD_RESULT_DESCRIPTION
        apply_schema(schema)
        self.assertEqual(schema["parameters"]["required"].count("result"), 1)

    def test_the_new_wording_names_the_real_limits(self):
        # The persona previously advertised 1200 characters; the kernel cuts at
        # 400 on the first line. A worker must be told the number that is true.
        self.assertIn("400", NEW_SUMMARY_DESCRIPTION)
        self.assertIn("FIRST LINE", NEW_SUMMARY_DESCRIPTION)
        self.assertIn("400", NEW_TOOL_DESCRIPTION)
        self.assertIn("REQUIRED", NEW_RESULT_DESCRIPTION)

    def test_a_changed_upstream_description_fails_loudly(self):
        schema = _schema()
        schema["parameters"]["properties"]["result"]["description"] = "something else"
        with self.assertRaises(ValueError) as ctx:
            apply_schema(schema)
        self.assertIn("re-derive", str(ctx.exception))

    def test_a_missing_field_fails_loudly(self):
        schema = _schema()
        del schema["parameters"]["properties"]["summary"]
        with self.assertRaises(KeyError) as ctx:
            apply_schema(schema)
        self.assertIn("re-derive", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
