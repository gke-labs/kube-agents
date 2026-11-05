"""Unit tests for unstructured_reasoning.py, apply_unstructured_reasoning_replay.py and its verifier.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches

The rule on its own; the applier's contract against miniatures of the two
message builds; a seeded history run through each patched miniature; and the
verifier against the miniatures patched, unpatched and patched wrong.
"""

import copy
import importlib.util
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import unstructured_reasoning
import verify_unstructured_reasoning_replay as verify
from apply_unstructured_reasoning_replay import (
    MARKER,
    REASONING_COPY_ANCHOR,
    SUMMARY_RELATIVE,
    TURN_CONTEXT_RELATIVE,
    apply,
)
from unstructured_reasoning import PLACEHOLDER, opens_with_unstructured_reasoning

RULE_SOURCE = Path(unstructured_reasoning.__file__)
RULE_RELATIVE = "hermes_cli/unstructured_reasoning.py"

# The shape of build_api_messages's per-message loop at v2026.9.14, cut to what
# the patch and the verifier read.
TURN_CONTEXT_STUB = '''import copy


def _clone_message_for_send(msg):
    return copy.deepcopy(msg)


def build_api_messages(agent, messages, *, current_turn_user_idx):
    api_messages = []
    for idx, msg in enumerate(messages):
        api_msg = _clone_message_for_send(msg)
        _api_content = api_msg.pop("api_content", None)
        if (
            isinstance(_api_content, str) and _api_content
            and msg.get("role") in ("user", "assistant")
        ):
            api_msg["content"] = _api_content

        # Pass reasoning back to the API for ALL assistant messages so multi-turn
        # reasoning context is preserved.
        agent._copy_reasoning_content_for_api(msg, api_msg)
        api_msg.pop("reasoning", None)
        api_msg.pop("finish_reason", None)
        api_messages.append(api_msg)
    return api_messages, ""
'''

# The shape of _iteration_summary_api_messages's loop at v2026.9.14.
SUMMARY_STUB = '''def substitute_api_content(api_msg):
    value = api_msg.pop("api_content", None)
    if isinstance(value, str) and value:
        api_msg["content"] = value


def _iteration_summary_api_messages(agent, messages: list) -> list:
    needs_sanitize = agent._should_sanitize_tool_calls()
    sanitize_model = agent.model
    api_messages = []
    for msg in messages:
        api_msg = msg.copy()
        agent._copy_reasoning_content_for_api(msg, api_msg)
        api_msg.pop("finish_reason", None)
        substitute_api_content(api_msg)
        if needs_sanitize:
            agent._sanitize_tool_calls_for_strict_api(api_msg, model=sanitize_model)
        api_messages.append(api_msg)
    return api_messages
'''

TOOL_CALLS = [{"id": "call_1__thought__c2ln", "type": "function",
               "function": {"name": "terminal", "arguments": '{"command": "kubectl get ns"}'}}]

# A history like the #3206 and #3773 sessions': reasoning replies in each shape,
# one that called a tool, its result, and the replies that must pass.
SEEDED_HISTORY = [
    {"role": "user", "content": "what version is a2a-next-dev"},
    {"role": "assistant", "content": "thought\nThe user wants the version. I will run kubectl.",
     "tool_calls": TOOL_CALLS, "finish_reason": "tool_calls"},
    {"role": "tool", "tool_call_id": "call_1__thought__c2ln", "content": "v1.33.2"},
    {"role": "assistant", "content": "Thought\nNow that I have it, I need to tell the user.checking.checking.",
     "finish_reason": "stop"},
    {"role": "user", "content": "delete seeded-stall"},
    {"role": "assistant", "content": "thought:\nThe user wants to delete the namespace."},
    {"role": "assistant", "content": "thought:The user asked for the delete."},
    {"role": "assistant", "content": "The cluster runs 1.33.2."},
    {"role": "assistant", "content": "I thought it had three nodes; it has four.\nthought\nend"},
    {"role": "assistant", "content": "Thought about it: it has four nodes."},
    {"role": "assistant", "content": "", "api_content": "thought\nreplayed from the sidecar"},
    {"role": "user", "content": "thought\nthanks"},
]
REPLACED = (1, 3, 5, 6, 10)


class _Agent:
    model = "model-default"

    def _copy_reasoning_content_for_api(self, source_msg, api_msg):
        pass

    def _should_sanitize_tool_calls(self):
        return False


def stage(turn_source=TURN_CONTEXT_STUB, summary_source=SUMMARY_STUB, rule=True):
    root = Path(tempfile.mkdtemp())
    for relative, source in ((TURN_CONTEXT_RELATIVE, turn_source), (SUMMARY_RELATIVE, summary_source)):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    if rule:
        (root / RULE_RELATIVE).parent.mkdir(parents=True)
        shutil.copy(RULE_SOURCE, root / RULE_RELATIVE)
    return root


def load(root, relative):
    spec = importlib.util.spec_from_file_location("patched_" + Path(relative).stem, root / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rule_modules():
    """sys.modules entries the inserted import resolves through, as the image's would."""
    return {"hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.unstructured_reasoning": unstructured_reasoning}


def run_verifier(root):
    with mock.patch.object(verify, "HERMES", root), mock.patch.object(verify, "FAILURES", []), \
            mock.patch.dict(sys.modules):
        rc = verify.main()
        return rc, list(verify.FAILURES)


class RuleTest(unittest.TestCase):
    def test_the_three_shapes_match(self):
        for content in [
            "thought\nThe user wants the version.",
            "thought:\nThe user wants to delete the namespace.",
            "thought:The user asked for the delete.",
            "Thought\nplan",
            "THOUGHT: plan",
            "\n \n  thought  \r\nplan",
            "thought",
            "thought:",
        ]:
            self.assertTrue(opens_with_unstructured_reasoning(content), repr(content))

    def test_prose_and_later_mentions_do_not_match(self):
        for content in [
            "The cluster runs 1.33.",
            "Thought about it, the cluster runs 1.33.",
            "thoughts\nplan",
            "thoughtful answer",
            "thought :plan",
            "I thought it was 1.32.\nthought\nIt is 1.33.",
            "My thought: it is 1.33.",
            "The answer.\nthought: more",
            "thought\rplan",
            # Go's TrimSpace keeps the information separators; so does the rule here.
            "\x1cthought\nplan",
            "",
            "\n  \n",
            None,
            [{"type": "text", "text": "thought\nplan"}],
        ]:
            self.assertFalse(opens_with_unstructured_reasoning(content), repr(content))


class ApplierTest(unittest.TestCase):
    def test_each_site_gets_the_call(self):
        root = stage()
        apply(root)
        turn = (root / TURN_CONTEXT_RELATIVE).read_text()
        self.assertIn(REASONING_COPY_ANCHOR, turn, "the upstream reasoning copy stays")
        self.assertLess(turn.index(MARKER), turn.index(REASONING_COPY_ANCHOR))
        summary = (root / SUMMARY_RELATIVE).read_text()
        self.assertLess(summary.index("substitute_api_content(api_msg)\n"), summary.index(MARKER))
        self.assertLess(summary.index(MARKER), summary.index("        if needs_sanitize:"))

    def test_a_second_apply_is_refused_and_changes_nothing(self):
        root = stage()
        apply(root)
        once = [(root / r).read_text() for r in (TURN_CONTEXT_RELATIVE, SUMMARY_RELATIVE)]
        with self.assertRaises(SystemExit):
            apply(root)
        self.assertEqual([(root / r).read_text() for r in (TURN_CONTEXT_RELATIVE, SUMMARY_RELATIVE)], once)
        self.assertTrue(all(text.count(MARKER) == 1 for text in once))

    def test_a_moved_anchor_writes_neither_file(self):
        for turn_source, summary_source in [
            (TURN_CONTEXT_STUB.replace("so multi-turn\n", "so multi-turn  \n"), SUMMARY_STUB),
            (TURN_CONTEXT_STUB, SUMMARY_STUB.replace("        substitute_api_content(api_msg)\n",
                                                     "        substitute_api_content(api_msg)  # moved\n")),
        ]:
            root = stage(turn_source, summary_source)
            with self.assertRaises(SystemExit):
                apply(root)
            self.assertNotIn(MARKER, (root / TURN_CONTEXT_RELATIVE).read_text())
            self.assertNotIn(MARKER, (root / SUMMARY_RELATIVE).read_text())


class SeededHistoryTest(unittest.TestCase):
    def setUp(self):
        root = stage()
        apply(root)
        modules = mock.patch.dict(sys.modules, rule_modules())
        modules.start()
        self.addCleanup(modules.stop)
        self.history = copy.deepcopy(SEEDED_HISTORY)
        turn, _ = load(root, TURN_CONTEXT_RELATIVE).build_api_messages(
            _Agent(), self.history, current_turn_user_idx=len(SEEDED_HISTORY) - 1)
        summary = load(root, SUMMARY_RELATIVE)._iteration_summary_api_messages(_Agent(), self.history)
        self.sent = {"build_api_messages": turn, "_iteration_summary_api_messages": summary}
        unpatched = stage()
        self.unpatched = {
            "build_api_messages": load(unpatched, TURN_CONTEXT_RELATIVE).build_api_messages(
                _Agent(), copy.deepcopy(SEEDED_HISTORY), current_turn_user_idx=len(SEEDED_HISTORY) - 1)[0],
            "_iteration_summary_api_messages": load(unpatched, SUMMARY_RELATIVE)._iteration_summary_api_messages(
                _Agent(), copy.deepcopy(SEEDED_HISTORY)),
        }

    def test_reasoning_replies_go_out_as_the_placeholder(self):
        for site, sent in self.sent.items():
            for i in REPLACED:
                self.assertEqual(sent[i]["content"], PLACEHOLDER, f"{site} message {i}")

    def test_tool_calls_stay_paired(self):
        for site, sent in self.sent.items():
            self.assertEqual(sent[1]["tool_calls"], TOOL_CALLS, site)
            self.assertEqual(sent[2]["tool_call_id"], TOOL_CALLS[0]["id"], site)

    def test_only_the_content_of_reasoning_replies_changes(self):
        for site, sent in self.sent.items():
            for i, (got, base) in enumerate(zip(sent, self.unpatched[site], strict=True)):
                if i in REPLACED:
                    self.assertEqual(dict(got, content=None), dict(base, content=None), f"{site} message {i}")
                else:
                    self.assertEqual(got, base, f"{site} message {i}")

    def test_the_stored_history_is_untouched(self):
        self.assertEqual(self.history, SEEDED_HISTORY)


class VerifierTest(unittest.TestCase):
    def patched(self):
        root = stage()
        apply(root)
        return root

    def assert_refused(self, root, needle):
        rc, failures = run_verifier(root)
        self.assertEqual(rc, 1)
        self.assertTrue(any(needle in f for f in failures), failures)

    def test_a_patched_tree_passes(self):
        self.assertEqual(run_verifier(self.patched()), (0, []))

    def test_an_unpatched_tree_is_refused_with_a_reason(self):
        self.assert_refused(stage(), "missing or moved")

    def test_a_tree_without_the_rule_module_is_refused(self):
        root = self.patched()
        (root / RULE_RELATIVE).unlink()
        self.assert_refused(root, "is not installed")

    def test_a_moved_message_build_is_refused(self):
        root = self.patched()
        path = root / TURN_CONTEXT_RELATIVE
        path.write_text(path.read_text().replace("def build_api_messages(", "def build_wire_messages("))
        self.assert_refused(root, "message build moved")

    def test_a_call_after_the_reasoning_copy_is_refused(self):
        root = self.patched()
        path = root / TURN_CONTEXT_RELATIVE
        source = path.read_text()
        start = source.index("        # A reply that is the model's reasoning")
        end = source.index(REASONING_COPY_ANCHOR)
        block = source[start:end]
        source = source[:start] + source[end:]
        path.write_text(source.replace(REASONING_COPY_ANCHOR, REASONING_COPY_ANCHOR + block))
        self.assert_refused(root, "not between")

    def test_a_summary_site_without_the_call_is_refused(self):
        root = self.patched()
        path = root / SUMMARY_RELATIVE
        path.write_text(path.read_text().replace("        _kube_replace_reasoning(api_msg)\n", ""))
        self.assert_refused(root, "not followed by its call")

    def test_a_rule_that_drops_tool_calls_is_caught(self):
        root = self.patched()
        path = root / RULE_RELATIVE
        path.write_text(path.read_text().replace(
            '        api_msg["content"] = PLACEHOLDER\n',
            '        api_msg["content"] = PLACEHOLDER\n        api_msg.pop("tool_calls", None)\n'))
        self.assert_refused(root, "with tool_calls")

    def test_a_rule_that_matches_any_mention_is_caught(self):
        root = self.patched()
        path = root / RULE_RELATIVE
        path.write_text(path.read_text().replace(
            "    if not isinstance(content, str):\n        return False\n",
            "    if not isinstance(content, str):\n        return False\n"
            "    return REASONING_WORD in content.lower()\n"))
        self.assert_refused(root, "says thought later")


if __name__ == "__main__":
    unittest.main()
