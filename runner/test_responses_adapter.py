"""The Responses -> contract translation, over payloads shaped like real ones."""

from __future__ import annotations

import unittest

import schema
from responses_adapter import events_from_responses, sniff_failure


def translate(payload, run_id="r1", profile=None):
    return list(events_from_responses(payload, run_id=run_id, profile=profile))


def _message(text):
    return {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}


def _call(call_id, name="kubectl", arguments='{"argv": ["get", "nodes"]}'):
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}


def _output(call_id, output):
    return {"type": "function_call_output", "call_id": call_id, "output": output}


class Translation(unittest.TestCase):
    def test_an_empty_payload_still_frames_a_run(self):
        events = translate({})
        self.assertEqual([schema.RUN_STARTED, schema.RUN_FINISHED], [e["type"] for e in events])

    def test_every_translated_event_satisfies_the_contract(self):
        payload = {
            "status": "completed",
            "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
            "output": [
                _call("c1"),
                _output("c1", "node-1 Ready"),
                _message("One node."),
            ],
        }
        for event in translate(payload, profile="platform"):
            self.assertEqual([], schema.event_errors(event), f"invalid: {event!r}")

    def test_the_stream_passes_the_contracts_ordering_rules(self):
        payload = {"output": [_call("c1"), _output("c1", "ok"), _message("done")]}
        events = translate(payload)
        self.assertEqual(schema.RUN_STARTED, events[0]["type"])
        self.assertEqual(schema.RUN_FINISHED, events[-1]["type"])
        self.assertEqual(list(range(len(events))), [e["seq"] for e in events])

    def test_arguments_are_decoded_from_their_json_string(self):
        call = next(e for e in translate({"output": [_call("c1")]}) if e["type"] == schema.TOOL_CALL)
        self.assertEqual({"argv": ["get", "nodes"]}, call["arguments"])

    def test_unparseable_arguments_are_preserved_rather_than_dropped(self):
        payload = {"output": [_call("c1", arguments="{not json")]}
        call = next(e for e in translate(payload) if e["type"] == schema.TOOL_CALL)
        self.assertEqual({"raw": "{not json"}, call["arguments"])

    def test_a_replayed_call_id_is_not_emitted_twice(self):
        # The endpoint is stateful and re-sends earlier turns' items; a second
        # emission would read downstream as a tool loop the agent never ran.
        payload = {"output": [_call("c1"), _output("c1", "ok"), _call("c1")]}
        calls = [e for e in translate(payload) if e["type"] == schema.TOOL_CALL]
        self.assertEqual(1, len(calls))

    def test_an_output_with_no_matching_call_is_dropped(self):
        payload = {"output": [_output("ghost", "ok")]}
        self.assertEqual([], [e for e in translate(payload) if e["type"] == schema.TOOL_RESULT])

    def test_an_elided_duplicate_output_leaves_the_call_unanswered(self):
        payload = {"output": [_call("c1"), _output("c1", "[Duplicate tool output for c1]")]}
        types = [e["type"] for e in translate(payload)]
        self.assertIn(schema.TOOL_CALL, types)
        self.assertNotIn(schema.TOOL_RESULT, types)

    def test_block_wrapped_output_is_flattened(self):
        payload = {"output": [_call("c1"), _output("c1", [{"text": "a"}, {"text": "b"}])]}
        result = next(e for e in translate(payload) if e["type"] == schema.TOOL_RESULT)
        self.assertEqual("ab", result["output"])

    def test_a_failed_tool_output_becomes_an_error_status(self):
        payload = {"output": [_call("c1"), _output("c1", "Error executing tool kubectl: denied")]}
        result = next(e for e in translate(payload) if e["type"] == schema.TOOL_RESULT)
        self.assertEqual("error", result["status"])

    def test_an_upstream_failure_status_is_carried_with_a_reason(self):
        final = translate({"status": "incomplete"})[-1]
        self.assertEqual("failed", final["status"])
        self.assertIn("incomplete", final["error"])

    def test_usage_is_carried_into_the_terminal_event(self):
        payload = {"usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8}}
        self.assertEqual(
            {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
            translate(payload)[-1]["usage"],
        )

    def test_a_message_with_no_text_is_not_emitted(self):
        payload = {"output": [{"type": "message", "role": "assistant", "content": []}]}
        self.assertEqual([], [e for e in translate(payload) if e["type"] == schema.MESSAGE])

    def test_a_user_message_is_not_translated(self):
        payload = {"output": [dict(_message("hi"), role="user")]}
        self.assertEqual([], [e for e in translate(payload) if e["type"] == schema.MESSAGE])

    def test_a_non_list_output_field_degrades_rather_than_raises(self):
        self.assertEqual(2, len(translate({"output": "unexpected"})))


class FailureSniffing(unittest.TestCase):
    """The guess the contract exists to make unnecessary."""

    def test_prose_containing_the_word_error_is_not_a_failure(self):
        self.assertFalse(sniff_failure("2 errors found in the log, both benign"))

    def test_a_structured_failure_flag_is(self):
        self.assertTrue(sniff_failure('{"success": false}'))
        self.assertTrue(sniff_failure('{"ok": false}'))

    def test_a_nonzero_exit_code_is(self):
        self.assertTrue(sniff_failure('{"exit_code": 1}'))
        self.assertFalse(sniff_failure('{"exit_code": 0}'))

    def test_an_error_beside_a_real_payload_is_a_diagnostic_not_a_failure(self):
        self.assertFalse(sniff_failure('{"error": "warn", "result": "the answer"}'))
        self.assertTrue(sniff_failure('{"error": "it broke"}'))


if __name__ == "__main__":
    unittest.main()
