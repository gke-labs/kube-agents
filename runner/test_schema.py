"""The schemas and the validator that reads them."""

from __future__ import annotations

import json
import unittest

import schema
from jsonschema_mini import UnsupportedSchemaError, validate


class MiniValidator(unittest.TestCase):
    def test_an_unimplemented_keyword_raises_rather_than_passing(self):
        # The property the whole file rests on: a schema this validator cannot
        # enforce must never be reported as satisfied.
        with self.assertRaises(UnsupportedSchemaError) as caught:
            validate({}, {"type": "object", "patternProperties": {"^x": {}}})
        self.assertIn("patternProperties", str(caught.exception))

    def test_booleans_are_not_integers(self):
        # bool subclasses int in Python, so the naive check accepts `true` for
        # an integer field.
        self.assertTrue(validate(True, {"type": "integer"}))
        self.assertEqual([], validate(1, {"type": "integer"}))

    def test_booleans_are_not_numbers(self):
        self.assertTrue(validate(False, {"type": "number"}))

    def test_additional_properties_false_is_enforced(self):
        errors = validate({"a": 1, "b": 2}, {"type": "object", "properties": {"a": {}}, "additionalProperties": False})
        self.assertEqual(1, len(errors))
        self.assertIn("'b'", errors[0])

    def test_required_properties_are_reported_by_name(self):
        errors = validate({}, {"type": "object", "required": ["x", "y"]})
        self.assertEqual(2, len(errors))

    def test_a_wrong_type_does_not_cascade(self):
        # One error, not one per unchecked constraint underneath it.
        errors = validate("nope", {"type": "object", "required": ["a", "b", "c"]})
        self.assertEqual(1, len(errors))

    def test_one_of_requires_exactly_one_match(self):
        one_of = {"oneOf": [{"const": "a"}, {"const": "b"}]}
        self.assertEqual([], validate("a", one_of))
        self.assertTrue(validate("c", one_of))

    def test_nested_errors_name_their_path(self):
        errors = validate(
            {"outer": {"inner": 1}},
            {"type": "object", "properties": {"outer": {"type": "object", "properties": {"inner": {"type": "string"}}}}},
        )
        self.assertEqual(1, len(errors))
        self.assertIn("$.outer.inner", errors[0])

    def test_min_length_and_minimum_and_min_items(self):
        self.assertTrue(validate("", {"type": "string", "minLength": 1}))
        self.assertTrue(validate(0, {"type": "integer", "minimum": 1}))
        self.assertTrue(validate([], {"type": "array", "minItems": 1}))


class ContractSchemas(unittest.TestCase):
    def test_both_schemas_parse_and_use_only_supported_keywords(self):
        # validate() raises on an unimplemented keyword, so running it over a
        # trivial instance is a structural check of the schema itself.
        for loaded in (schema.request_schema(), schema.event_schema()):
            try:
                validate({"unlikely": True}, loaded)
            except UnsupportedSchemaError as exc:
                self.fail(f"contract schema uses an unsupported keyword: {exc}")

    def test_new_request_builds_a_valid_request(self):
        request = schema.new_request(
            run_id="r", subject="user:a", issuer="system", profile="p", input_text="hi"
        )
        self.assertEqual([], schema.request_errors(request))

    def test_new_request_with_a_workspace_and_budget_is_valid(self):
        request = schema.new_request(
            run_id="r",
            subject="user:a",
            issuer="google-chat",
            profile="platform",
            input_text="hi",
            workspace_mode="read-write",
            workspace_path="/workspace/lease-1",
            conversation="conv-9",
            budget={"max_tokens": 64000, "max_tool_calls": 40, "deadline_seconds": 900},
        )
        self.assertEqual([], schema.request_errors(request))

    def test_an_unknown_top_level_field_is_rejected(self):
        request = schema.new_request(
            run_id="r", subject="user:a", issuer="system", profile="p", input_text="hi"
        )
        request["tenant"] = "acme"
        self.assertTrue(schema.request_errors(request))

    def test_a_bad_workspace_mode_is_rejected(self):
        request = schema.new_request(
            run_id="r", subject="user:a", issuer="system", profile="p", input_text="hi"
        )
        request["workspace"]["mode"] = "read-write-and-then-some"
        self.assertTrue(schema.request_errors(request))

    def test_check_request_raises_with_the_reasons_in_the_message(self):
        with self.assertRaises(schema.ContractViolation) as caught:
            schema.check_request({"contract_version": schema.CONTRACT_VERSION})
        self.assertIn("run_id", str(caught.exception))

    def test_every_declared_event_type_has_a_schema_branch(self):
        titles = {branch["title"] for branch in schema.event_schema()["oneOf"]}
        self.assertEqual(schema.EVENT_TYPES, titles)

    def test_the_terminal_status_constant_matches_the_schema(self):
        branch = next(
            b for b in schema.event_schema()["oneOf"] if b["title"] == schema.RUN_FINISHED
        )
        self.assertEqual(schema.TERMINAL_STATUSES, set(branch["properties"]["status"]["enum"]))

    def test_an_event_of_two_minds_matches_no_branch(self):
        # oneOf, not anyOf: mixing two variants' fields must not validate.
        event = {
            "contract_version": schema.CONTRACT_VERSION,
            "run_id": "r",
            "seq": 0,
            "type": "message",
            "role": "assistant",
            "text": "hi",
            "call_id": "c",
        }
        self.assertTrue(schema.event_errors(event))

    def test_the_schemas_are_valid_json_on_disk(self):
        for name in ("run_request", "run_event"):
            path = schema.CONTRACT_DIR / f"{name}.schema.json"
            json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
