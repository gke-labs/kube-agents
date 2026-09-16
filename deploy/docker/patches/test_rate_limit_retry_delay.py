"""Unit tests for the retryDelay parser installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The text under test is what the worker saw during the 2026-09-03 quota storm:
the OpenAI SDK's ``str()`` of a 429 whose message is LiteLLM's pass-through of
Google's ``RetryInfo`` body, with no ``Retry-After`` header anywhere.
"""

import json
import unittest

from rate_limit_retry_delay import (
    RETRY_DELAY_CAP_SECONDS,
    retry_delay_from_error,
    retry_delay_from_text,
)

GOOGLE_BODY = {
    "error": {
        "code": 429,
        "message": "Resource exhausted. Please try again later.",
        "status": "RESOURCE_EXHAUSTED",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.RetryInfo",
                "retryDelay": "54s",
            }
        ],
    }
}

LITELLM_MESSAGE = "litellm.RateLimitError: VertexAIException - " + json.dumps(
    GOOGLE_BODY
)

SDK_BODY = {
    "error": {"message": LITELLM_MESSAGE, "type": None, "param": None, "code": "429"}
}

# What ``str(openai.RateLimitError)`` prints: a Python repr of the JSON body,
# so the inner JSON keeps double quotes while the outer dict uses single ones.
SDK_TEXT = f"Error code: 429 - {SDK_BODY}"


class FakeResponse:
    def __init__(self, payload=None, headers=None):
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSDKError(Exception):
    """The attributes ``openai.APIStatusError`` exposes."""

    def __init__(self, text, *, message=None, body=None, response=None):
        super().__init__(text)
        self.message = message if message is not None else text
        self.body = body
        self.response = response


class TextParserTest(unittest.TestCase):
    def test_the_storm_text_yields_the_quoted_delay(self):
        self.assertEqual(retry_delay_from_text(SDK_TEXT), 54.0)

    def test_json_quoting(self):
        self.assertEqual(retry_delay_from_text('{"retryDelay": "54s"}'), 54.0)

    def test_python_repr_quoting(self):
        self.assertEqual(retry_delay_from_text("{'retryDelay': '54s'}"), 54.0)

    def test_escaped_quoting_inside_a_serialised_string(self):
        self.assertEqual(
            retry_delay_from_text('"{\\"retryDelay\\": \\"54s\\"}"'), 54.0
        )

    def test_fractional_seconds(self):
        self.assertEqual(retry_delay_from_text('"retryDelay": "0.5s"'), 0.5)
        self.assertEqual(
            retry_delay_from_text('"retryDelay": "54.123456789s"'), 54.123456789
        )

    def test_whitespace_around_the_colon_is_tolerated(self):
        self.assertEqual(retry_delay_from_text('"retryDelay" : "7s"'), 7.0)

    def test_the_cap_is_the_header_paths_cap(self):
        self.assertEqual(RETRY_DELAY_CAP_SECONDS, 600.0)
        self.assertEqual(retry_delay_from_text('"retryDelay": "99999s"'), 600.0)

    def test_a_caller_may_lower_the_cap(self):
        self.assertEqual(retry_delay_from_text('"retryDelay": "54s"', cap=10), 10.0)

    def test_a_zero_delay_is_not_a_wait(self):
        """Returning 0 would skip the stock backoff and retry immediately."""
        self.assertIsNone(retry_delay_from_text('"retryDelay": "0s"'))
        self.assertIsNone(retry_delay_from_text('"retryDelay": "0.0s"'))

    def test_absent_delay(self):
        self.assertIsNone(retry_delay_from_text("Rate limit reached for model"))
        self.assertIsNone(retry_delay_from_text(""))
        self.assertIsNone(retry_delay_from_text(None))

    def test_garbage_values_are_ignored(self):
        self.assertIsNone(retry_delay_from_text('"retryDelay": "soon"'))
        self.assertIsNone(retry_delay_from_text('"retryDelay": "54m"'))
        self.assertIsNone(retry_delay_from_text('"retryDelay": "54"'))
        self.assertIsNone(retry_delay_from_text('"retryDelay": "54sec"'))

    def test_a_delay_named_something_else_is_not_matched(self):
        self.assertIsNone(retry_delay_from_text('"retry_after": "54s"'))
        self.assertIsNone(retry_delay_from_text('"delay": "54s"'))


class ErrorParserTest(unittest.TestCase):
    def test_the_sdk_error_text_is_enough(self):
        self.assertEqual(retry_delay_from_error(FakeSDKError(SDK_TEXT)), 54.0)

    def test_a_delay_only_in_message_is_found(self):
        err = FakeSDKError("Error code: 429", message=LITELLM_MESSAGE)
        self.assertEqual(retry_delay_from_error(err), 54.0)

    def test_a_delay_only_in_the_body_dict_is_found(self):
        err = FakeSDKError("Error code: 429", body=SDK_BODY["error"])
        self.assertEqual(retry_delay_from_error(err), 54.0)

    def test_a_delay_only_in_the_response_json_is_found(self):
        err = FakeSDKError("Error code: 429", response=FakeResponse(SDK_BODY))
        self.assertEqual(retry_delay_from_error(err), 54.0)

    def test_a_response_whose_json_raises_is_skipped(self):
        err = FakeSDKError("Error code: 429", response=FakeResponse(None))
        self.assertIsNone(retry_delay_from_error(err))

    def test_the_cause_chain_is_followed(self):
        inner = FakeSDKError(SDK_TEXT)
        try:
            try:
                raise inner
            except FakeSDKError as e:
                raise RuntimeError("wrapped") from e
        except RuntimeError as outer:
            self.assertEqual(retry_delay_from_error(outer), 54.0)

    def test_an_implicit_context_is_followed_too(self):
        try:
            try:
                raise FakeSDKError(SDK_TEXT)
            except FakeSDKError:
                raise RuntimeError("during handling")
        except RuntimeError as outer:
            self.assertEqual(retry_delay_from_error(outer), 54.0)

    def test_the_cause_chain_is_bounded(self):
        err = FakeSDKError(SDK_TEXT)
        for _ in range(10):
            wrapper = RuntimeError("layer")
            wrapper.__cause__ = err
            err = wrapper
        self.assertIsNone(retry_delay_from_error(err))

    def test_a_cycle_in_the_chain_terminates(self):
        a = RuntimeError("a")
        b = RuntimeError("b")
        a.__cause__ = b
        b.__cause__ = a
        self.assertIsNone(retry_delay_from_error(a))

    def test_the_cap_applies_to_errors(self):
        err = FakeSDKError('{"retryDelay": "3600s"}')
        self.assertEqual(retry_delay_from_error(err), 600.0)

    def test_a_plain_429_leaves_the_stock_backoff_in_charge(self):
        self.assertIsNone(retry_delay_from_error(FakeSDKError("Error code: 429")))

    def test_it_never_raises(self):
        class Hostile(Exception):
            @property
            def body(self):
                raise RuntimeError("no body for you")

            @property
            def response(self):
                raise RuntimeError("no response either")

        self.assertIsNone(retry_delay_from_error(Hostile("x")))
        self.assertIsNone(retry_delay_from_error(None))
        self.assertIsNone(retry_delay_from_error(object()))


if __name__ == "__main__":
    unittest.main()
