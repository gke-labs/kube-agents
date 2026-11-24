"""The gateway redaction hook against the shared credential-shapes fixture.

`tests/conformance/fixtures/credential-shapes.json` is the one list of
credential shapes and must-keep text that the credential broker's tool-output
redactor and the answer scrubber already read. This holds the LiteLLM
pre-call hook (`charts/kube-agents/files/litellm_redaction_callback.py`) to
the same list, at every position where a tool's output reaches the model:

- an Anthropic `tool_result` whose content is a string;
- an Anthropic `tool_result` whose content is a list of text blocks;
- an OpenAI-wire `role: tool` message;
- a Gemini `functionResponse`'s response;
- per driver, the output as each harness actually sends it: Claude Code's
  `tool_result` (the Anthropic string row above), and agy's `run_command`
  output layout (`testdata/agy/tool.jsonl`) inside a Gemini
  `functionResponse`, under both of agy's calls, `generateContent` and
  `streamGenerateContent`. agy speaks only those Gemini routes to the
  gateway, so that is the one position its tool output reaches.

A redact case's secret must not survive, nor any 8-character fragment of it,
as the fixture's own description states. A keep case's text must come out
byte-identical, except where the gateway is deliberately wider than the
broker (`DELIBERATELY_WIDER`). Over-masking there costs the model some
context; a miss would forward the credential.

The hook runs with IP pseudonymisation off and no operator rules, so only the
credential shapes act on the text.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import test_litellm_redaction as gateway  # noqa: E402  (its loader stubs LiteLLM)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = _REPO_ROOT / "tests" / "conformance" / "fixtures" / "credential-shapes.json"
TOOL_OUTPUT_SCOPE = "tool-output"
FRAGMENT_LENGTH = 8

# Keep cases the gateway masks on purpose, with the reason. The gateway is a
# backstop for output that never passed the broker, so it keeps the audit
# redactor's wider forms.
DELIBERATELY_WIDER = {
    "bearer-and-basic-in-prose": (
        "AuditRedactor masks any `bearer` or `basic` followed by twelve value "
        "characters; the broker's tool-output form needs a header's context or "
        "a value with a digit or symbol in it. At the gateway, a masked word "
        "costs context and a missed header value would forward a credential."
    ),
}


# agy's run_command output, as the recorded stream has it
# (a2a/worker-adapter/testdata/agy/tool.jsonl): the exit line, a blank line,
# then what the command printed.
AGY_RUN_COMMAND_OUTPUT = "Command exited with code 0.\n\nCommand output:\n{}"
# The LiteLLM call types agy's two Gemini routes arrive as.
AGY_CALL_TYPES = ("agenerate_content", "agenerate_content_stream")


def _agy_shapes(text: str):
    output = AGY_RUN_COMMAND_OUTPUT.format(text)
    for call_type in AGY_CALL_TYPES:
        yield (
            f"agy run_command output ({call_type})",
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"functionResponse": {"name": "run_command", "response": {"output": output}}}],
                    }
                ]
            },
            call_type,
            lambda r: r["contents"][0]["parts"][0]["functionResponse"]["response"]["output"],
            output,
        )


def _shapes(text: str):
    """(name, request, call type, how to read the text back) per walked position."""
    return (
        (
            "anthropic tool_result string",
            {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": text}]}]},
            "anthropic_messages",
            lambda r: r["messages"][0]["content"][0]["content"],
        ),
        (
            "anthropic tool_result text blocks",
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "t", "content": [{"type": "text", "text": text}]}
                        ],
                    }
                ]
            },
            "anthropic_messages",
            lambda r: r["messages"][0]["content"][0]["content"][0]["text"],
        ),
        (
            "openai role tool",
            {"messages": [{"role": "tool", "tool_call_id": "t", "content": text}]},
            "completion",
            lambda r: r["messages"][0]["content"],
        ),
        (
            "gemini functionResponse",
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"functionResponse": {"name": "run_command", "response": {"output": text}}}],
                    }
                ]
            },
            "agenerate_content",
            lambda r: r["contents"][0]["parts"][0]["functionResponse"]["response"]["output"],
        ),
    )


class TestGatewayMeetsTheCredentialShapesFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._previous_salt = os.environ.get(gateway._SALT_ENV_VAR)
        os.environ[gateway._SALT_ENV_VAR] = "test-salt"
        cls.hook = gateway._load_callback_module({"ip": {"action": "off"}, "rules": []}).proxy_handler_instance
        cases = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
        cls.cases = [c for c in cases if TOOL_OUTPUT_SCOPE in c["scope"]]

    @classmethod
    def tearDownClass(cls) -> None:
        if cls._previous_salt is None:
            os.environ.pop(gateway._SALT_ENV_VAR, None)
        else:
            os.environ[gateway._SALT_ENV_VAR] = cls._previous_salt

    def _through_the_hook(self, request: dict, call_type: str) -> dict:
        return asyncio.run(self.hook.async_pre_call_hook({}, None, request, call_type))

    def test_the_fixture_has_tool_output_cases_of_both_kinds(self) -> None:
        kinds = {c["expect"] for c in self.cases}
        self.assertEqual({"redact", "keep"}, kinds, "the fixture lost its redact or keep cases")

    def test_every_redact_case_is_redacted_at_every_walked_position(self) -> None:
        for case in (c for c in self.cases if c["expect"] == "redact"):
            secret = case["secret"]
            fragments = {secret[i : i + FRAGMENT_LENGTH] for i in range(len(secret) - FRAGMENT_LENGTH + 1)}
            for name, request, call_type, read_back in _shapes(case["text"]):
                with self.subTest(case=case["id"], position=name):
                    out = read_back(self._through_the_hook(request, call_type))
                    self.assertNotIn(secret, out)
                    self.assertEqual([], sorted(f for f in fragments if f in out), "a fragment of the secret survived")
            for name, request, call_type, read_back, _ in _agy_shapes(case["text"]):
                with self.subTest(case=case["id"], position=name):
                    out = read_back(self._through_the_hook(request, call_type))
                    self.assertNotIn(secret, out)
                    self.assertEqual([], sorted(f for f in fragments if f in out), "a fragment of the secret survived")

    def test_every_keep_case_is_kept_at_every_walked_position(self) -> None:
        for case in (c for c in self.cases if c["expect"] == "keep" and c["id"] not in DELIBERATELY_WIDER):
            for name, request, call_type, read_back in _shapes(case["text"]):
                with self.subTest(case=case["id"], position=name):
                    self.assertEqual(case["text"], read_back(self._through_the_hook(request, call_type)))
            for name, request, call_type, read_back, sent in _agy_shapes(case["text"]):
                with self.subTest(case=case["id"], position=name):
                    self.assertEqual(sent, read_back(self._through_the_hook(request, call_type)))

    def test_a_deliberately_wider_case_is_still_in_the_fixture_and_still_wider(self) -> None:
        # The list must not outlive its reason: a case the fixture dropped, or
        # one the gateway now keeps, comes off it.
        by_id = {c["id"]: c for c in self.cases}
        for case_id in DELIBERATELY_WIDER:
            with self.subTest(case=case_id):
                self.assertIn(case_id, by_id, "listed as deliberately wider but not a tool-output case")
                case = by_id[case_id]
                self.assertEqual("keep", case["expect"])
                _, request, call_type, read_back = _shapes(case["text"])[0]
                self.assertNotEqual(
                    case["text"],
                    read_back(self._through_the_hook(request, call_type)),
                    "the gateway now keeps this case; take it off DELIBERATELY_WIDER",
                )


if __name__ == "__main__":
    unittest.main()
