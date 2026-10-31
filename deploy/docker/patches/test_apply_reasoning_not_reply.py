"""Unit tests for apply_reasoning_not_reply.py and its verifier.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The applier against miniatures of the three Hermes v2026.9.14 sites (the
anchor lines copied verbatim, the rest cut down), and the verifier against the
same tree patched and unpatched, plus the drift it exists to catch: a new
``_extract_reasoning`` caller, and an allowed one that moved.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import verify_reasoning_not_reply as verify
from apply_reasoning_not_reply import (
    CLEAN_STOP_ANCHOR,
    DISPLAY_REFUSED,
    DISPLAY_TOGGLE_ANCHOR,
    EMPTY_RESPONSE_RELATIVE,
    FINAL_RESPONSE_RELATIVE,
    MARKER,
    PREPEND_ANCHOR,
    REFUSAL_ANCHOR,
    RUN_TURN_RELATIVE,
    SLASH_RELATIVE,
    TERMINAL_ANCHOR,
    TRUNCATION_RELATIVE,
    apply,
)

FINAL_RESPONSE_STUB = '''def finish_text_response(agent, assistant_message, finish_reason):
    _content = assistant_message.content
    if (
        finish_reason == "stop"
        and not assistant_message.tool_calls
        and (_content is None or (isinstance(_content, str) and not _content.strip()))
    ):
        _promoted = agent._extract_reasoning(assistant_message)
        if _promoted:
            assistant_message.content = _promoted
    return assistant_message.content or ""
'''

EMPTY_RESPONSE_STUB = '''def _terminal_empty(agent, assistant_message, finish_reason, messages):
    reasoning_text = agent._extract_reasoning(assistant_message)
    if not reasoning_text:
        return "(empty)"

    reasoning_preview = reasoning_text[:500] + "..." if len(reasoning_text) > 500 else reasoning_text
    return (
        "⚠️ The model produced only internal reasoning and no final answer, despite retries"
        + (" and fallback" if agent._fallback_chain else "")
        + ". Its last reasoning, which may contain the answer:\\n\\n" + reasoning_preview
    )
'''

TRUNCATION_STUB = '''def handle_content_policy_refusal(agent, response, normalize_response_for_agent):
    _refusal_result = normalize_response_for_agent(agent, response)
    _refusal_text = (getattr(_refusal_result, "content", None) or "").strip()
    if not _refusal_text:
        _refusal_text = (agent._extract_reasoning(_refusal_result) or "").strip()
    return _refusal_text
'''

SLASH_STUB = '''_REASONING_DISPLAY_TOGGLES = {"show": True, "on": True, "hide": False, "off": False}


class Runner:
    _show_reasoning = False

    def _apply_reasoning_selection(self, session_key, platform_key, value, persist_global=False):
        value = (value or "").strip().lower()
        show = _REASONING_DISPLAY_TOGGLES.get(value)
        if show is not None:  # per-platform display toggle
            self._show_reasoning = show
            return "display on" if show else "display off"
        return "effort " + value
'''

RUN_TURN_STUB = '''class Runner:
    _show_reasoning = True

    def _hmwa_prepend_reasoning(self, agent_result, response, source, _intentional_silence):
        _show_reasoning_effective = self._show_reasoning
        last_reasoning = agent_result.get("last_reasoning")
        if not (_show_reasoning_effective and response and not _intentional_silence and last_reasoning):
            return response
        return last_reasoning + "\\n\\n" + response
'''

HELPERS_STUB = '''def _assistant_reasoning_text(agent, assistant_message):
    return agent._extract_reasoning(assistant_message)
'''

RUN_AGENT_STUB = '''class AIAgent:
    _extract_reasoning = staticmethod(lambda message: None)
'''


class _Agent:
    _fallback_chain = ()

    def __init__(self, reasoning):
        self.reasoning = reasoning

    def _extract_reasoning(self, _message):
        return self.reasoning


class _Message:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


def stage(final=FINAL_RESPONSE_STUB, empty=EMPTY_RESPONSE_STUB, truncation=TRUNCATION_STUB, helpers=HELPERS_STUB):
    root = Path(tempfile.mkdtemp())
    (root / "agent").mkdir()
    (root / FINAL_RESPONSE_RELATIVE).write_text(final)
    (root / EMPTY_RESPONSE_RELATIVE).write_text(empty)
    (root / TRUNCATION_RELATIVE).write_text(truncation)
    (root / "gateway").mkdir()
    (root / SLASH_RELATIVE).write_text(SLASH_STUB)
    (root / RUN_TURN_RELATIVE).write_text(RUN_TURN_STUB)
    (root / "agent" / "chat_completion_helpers.py").write_text(helpers)
    (root / "run_agent.py").write_text(RUN_AGENT_STUB)
    return root


def load(root, relative):
    namespace = {}
    exec(compile((root / relative).read_text(), relative, "exec"), namespace)  # noqa: S102 -- the stub under test
    return namespace


def run_verifier(root):
    with mock.patch.object(verify, "HERMES", root), mock.patch.object(verify, "FAILURES", []):
        rc = verify.main()
        return rc, list(verify.FAILURES)


class ApplierTest(unittest.TestCase):
    def test_the_stub_anchors_are_the_appliers(self):
        # Guards the stubs: an anchor edit that the stubs did not follow would
        # make every test below vacuous.
        self.assertIn(CLEAN_STOP_ANCHOR, FINAL_RESPONSE_STUB)
        self.assertIn(TERMINAL_ANCHOR, EMPTY_RESPONSE_STUB)
        self.assertIn(REFUSAL_ANCHOR, TRUNCATION_STUB)
        self.assertIn(DISPLAY_TOGGLE_ANCHOR, SLASH_STUB)
        self.assertIn(PREPEND_ANCHOR, RUN_TURN_STUB)

    def test_unpatched_each_site_makes_a_reply_of_the_reasoning(self):
        root = stage()
        agent = _Agent("thinking about the cluster")
        self.assertEqual(
            load(root, FINAL_RESPONSE_RELATIVE)["finish_text_response"](agent, _Message(), "stop"),
            "thinking about the cluster",
        )
        self.assertIn("thinking about the cluster", load(root, EMPTY_RESPONSE_RELATIVE)["_terminal_empty"](agent, None, "stop", []))
        refusal = load(root, TRUNCATION_RELATIVE)["handle_content_policy_refusal"]
        self.assertEqual(refusal(agent, None, lambda _a, _r: _Message()), "thinking about the cluster")

    def test_patched_no_site_makes_a_reply_of_the_reasoning(self):
        root = stage()
        apply(root)
        agent = _Agent("thinking about the cluster")
        self.assertEqual(load(root, FINAL_RESPONSE_RELATIVE)["finish_text_response"](agent, _Message(), "stop"), "")
        terminal = load(root, EMPTY_RESPONSE_RELATIVE)["_terminal_empty"](agent, None, "stop", [])
        self.assertNotIn("thinking about the cluster", terminal)
        self.assertTrue(terminal.startswith("⚠️ The model produced only internal reasoning and no final answer"))
        refusal = load(root, TRUNCATION_RELATIVE)["handle_content_policy_refusal"]
        self.assertEqual(refusal(agent, None, lambda _a, _r: _Message()), "")

    def test_reasoning_display_cannot_be_turned_on(self):
        unpatched = stage()
        runner = load(unpatched, SLASH_RELATIVE)["Runner"]()
        self.assertEqual(runner._apply_reasoning_selection("s", "slack", "show"), "display on")
        root = stage()
        apply(root)
        runner = load(root, SLASH_RELATIVE)["Runner"]()
        for value in ("show", "on", " SHOW "):
            self.assertEqual(runner._apply_reasoning_selection("s", "slack", value), DISPLAY_REFUSED)
        self.assertFalse(runner._show_reasoning)
        self.assertEqual(runner._apply_reasoning_selection("s", "slack", "off"), "display off")
        self.assertEqual(runner._apply_reasoning_selection("s", "slack", "high"), "effort high")

    def test_the_reasoning_is_never_prepended(self):
        result = {"last_reasoning": "thinking about the cluster"}
        unpatched = load(stage(), RUN_TURN_RELATIVE)["Runner"]()
        self.assertIn("thinking about the cluster", unpatched._hmwa_prepend_reasoning(result, "the answer", None, False))
        root = stage()
        apply(root)
        patched = load(root, RUN_TURN_RELATIVE)["Runner"]()
        self.assertEqual(patched._hmwa_prepend_reasoning(result, "the answer", None, False), "the answer")

    def test_text_the_model_did_write_is_kept(self):
        root = stage()
        apply(root)
        agent = _Agent("thinking")
        self.assertEqual(
            load(root, FINAL_RESPONSE_RELATIVE)["finish_text_response"](agent, _Message("the answer"), "stop"),
            "the answer",
        )
        refusal = load(root, TRUNCATION_RELATIVE)["handle_content_policy_refusal"]
        self.assertEqual(refusal(agent, None, lambda _a, _r: _Message("I cannot help with that")), "I cannot help with that")
        self.assertEqual(load(root, EMPTY_RESPONSE_RELATIVE)["_terminal_empty"](_Agent(None), None, "stop", []), "(empty)")

    def test_a_second_apply_is_refused(self):
        root = stage()
        apply(root)
        with self.assertRaises(SystemExit):
            apply(root)

    def test_a_moved_anchor_leaves_every_file_untouched(self):
        root = stage(truncation=TRUNCATION_STUB.replace("if not _refusal_text:", "if _refusal_text == '':"))
        with self.assertRaises(SystemExit):
            apply(root)
        for relative in (FINAL_RESPONSE_RELATIVE, EMPTY_RESPONSE_RELATIVE, TRUNCATION_RELATIVE, SLASH_RELATIVE, RUN_TURN_RELATIVE):
            self.assertNotIn(MARKER, (root / relative).read_text())


class VerifierTest(unittest.TestCase):
    def test_a_patched_tree_passes(self):
        root = stage()
        apply(root)
        self.assertEqual(run_verifier(root), (0, []))

    def test_an_unpatched_tree_fails(self):
        rc, failures = run_verifier(stage())
        self.assertEqual(rc, 1)
        self.assertTrue(any("returns text built from" in f for f in failures), failures)
        self.assertTrue(any("finish_text_response" in f for f in failures), failures)
        self.assertTrue(any("handle_content_policy_refusal" in f for f in failures), failures)

    def test_a_new_reasoning_reader_fails(self):
        root = stage()
        apply(root)
        with (root / "agent" / "new_fallback.py").open("w") as handle:
            handle.write("def surface(agent, message):\n    return agent._extract_reasoning(message)\n")
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertTrue(any("agent/new_fallback.py:2 (surface)" in f for f in failures), failures)

    def test_a_direct_helper_call_fails(self):
        root = stage()
        apply(root)
        with (root / "agent" / "new_fallback.py").open("w") as handle:
            handle.write(
                "from agent.agent_runtime_helpers import extract_reasoning\n"
                "def surface(agent, message):\n    return extract_reasoning(agent, message)\n"
            )
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertTrue(any("agent/new_fallback.py:3 (surface)" in f for f in failures), failures)

    def test_a_second_call_inside_an_allowed_function_fails(self):
        root = stage(empty=EMPTY_RESPONSE_STUB.replace(
            '        return "(empty)"\n',
            '        return "(empty)"\n    extra = agent._extract_reasoning(assistant_message)\n'))
        apply(root)
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertTrue(any("_terminal_empty) calls _extract_reasoning 2 times" in f for f in failures), failures)

    def test_an_allowed_reader_that_moved_fails(self):
        root = stage(helpers="def _assistant_reasoning_text(agent, assistant_message):\n    return None\n")
        apply(root)
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertTrue(any("re-derive ALLOWED_READERS" in f for f in failures), failures)


if __name__ == "__main__":
    unittest.main()
