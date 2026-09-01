#!/usr/bin/env python3
"""Regression test for #112: a read-only profile cannot write, by any route.

The platform specialist reads shared memory and writes nothing. That is one
setting (``memory.read_only`` in the profile's config.yaml) enforced in four
places, and this locks all four down, because three of them failing open is
silent — the model would simply start writing.

  1. ``memory_retain`` is absent from the advertised schemas.
  2. A ``memory_retain`` call is refused anyway, as a backstop.
  3. Automatic capture is off: no per-turn sync, no end-of-session absorb, and
     the stock provider's own ``_auto_retain`` is cleared.
  4. The system prompt says so, so the model does not spend a turn discovering it.

The read side must be untouched — a read-only profile that also cannot read is
the failure this whole change exists to undo.

Standalone: plain asserts, no pytest. See ``test_recall_reporting.py`` for how to
run it.

    HERMES_ROOT=~/git/hermes-agent python3 tests/memory/test_read_only_profile.py
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERMES = os.environ.get("HERMES_ROOT") or "/opt/hermes"
if os.path.isdir(_HERMES):
    sys.path.insert(0, _HERMES)
sys.path.insert(0, os.path.join(_REPO, "agents", "chat", "plugins", "memory"))

try:
    from . import _stubs  # noqa: F401
except (ImportError, ValueError):
    import _stubs  # type: ignore # noqa: F401

import kube_agents_memory  # noqa: E402
from kube_agents_memory import SHARED_TAG, KubeAgentsMemoryProvider  # noqa: E402


def provider(*, read_only, user_tag="user:alice"):
    """A provider in one of the two modes, wired to a stub client."""
    p = KubeAgentsMemoryProvider()
    p._read_only = read_only
    p._user_tag = user_tag
    calls = {}

    class StubClient:
        def arecall(self, **kw):
            calls["recall"] = kw
            return SimpleNamespace(results=[SimpleNamespace(text="RB-114: drain before upgrade.")])

        def aretain(self, **kw):
            calls["retain"] = kw
            return SimpleNamespace(id="doc-1")

    p._hindsight = SimpleNamespace(
        _bank_id="kube-agents-memory",
        _budget="low",
        _recall_max_tokens=4096,
        _recall_types=["observation"],
        _run_hindsight_operation=lambda op: op(StubClient()),
    )
    return p, calls


def _names(p):
    return [s["name"] for s in p.get_tool_schemas()]


class TestReadOnlyProfile(unittest.TestCase):
    def test_the_write_tool_is_not_advertised(self):
        """Advertising it and refusing the call would read as a transient failure."""
        self.assertNotIn("memory_retain", _names(provider(read_only=True)[0]))
        # ...and is still there when the setting is off, or every profile just lost
        # its write path.
        self.assertIn("memory_retain", _names(provider(read_only=False)[0]))

    def test_reads_are_untouched(self):
        p, calls = provider(read_only=True)
        self.assertEqual(_names(p), ["memory_recall", "memory_reflect"])
        r = json.loads(p.handle_tool_call("memory_recall", {"query": "RB-114"}))
        self.assertEqual(r["status"], "found", r)
        self.assertIn("RB-114", r["result"], r)
        self.assertEqual(calls["recall"]["tags"], ["user:alice", SHARED_TAG], calls)

    def test_the_write_call_is_refused_anyway(self):
        """Backstop: an invented call, or a schema cached across a config change."""
        p, calls = provider(read_only=True)
        r = json.loads(p.handle_tool_call("memory_retain", {"content": "x", "scope": "shared"}))
        self.assertEqual(r["status"], "read_only", r)
        self.assertIn("read-only", r["error"], r)
        # The refusal has to land before anything reaches Hindsight.
        self.assertNotIn("retain", calls, calls)
        # And it must not read as retryable — a model that retries burns the task.
        self.assertIn("retrying will not change that", r["error"], r)

    def test_automatic_capture_is_off(self):
        """The tool surface is not the only write path; the turn hooks are one too.

        Scoped to the read-only decision, and only that. Stubbing ``_call`` is fine
        for "does the hook return before forwarding"; it is worthless for "does the
        forward arrive", because ``_call`` is the method that forwards and, until
        #784, swallowed the result. That half lives in
        ``test_forwarding_matches_hindsight.py``, which drives the same hooks through
        ``MemoryManager`` and binds each forward against the real stock signature.
        """
        p, _ = provider(read_only=True)
        seen = []
        p._call = lambda name, *a, **kw: seen.append(name)
        p.sync_turn("u", "a")
        p.on_session_end([{"role": "user", "content": "u"}])
        self.assertEqual(seen, [], seen)

        # Same hooks, writable profile: both must fire, or this test would pass on a
        # provider that had simply stopped working.
        p, _ = provider(read_only=False)
        seen = []
        p._call = lambda name, *a, **kw: seen.append(name)
        p.sync_turn("u", "a")
        p.on_session_end([{"role": "user", "content": "u"}])
        self.assertEqual(seen, ["sync_turn", "on_session_end"], seen)

    def test_scoping_clears_the_stock_providers_own_write_state(self):
        """`_auto_retain` is read by Hindsight itself, below our hooks."""
        stock = SimpleNamespace(
            _config={"recall_budget": "low"},
            _prefetch_method="recall",
            _auto_retain=True,
            _retain_tags=["stale"],
            _tags=["stale"],
            _observation_scopes=[["stale"]],
        )
        kube_agents_memory.apply_scoping(stock, user_tag="user:alice", read_only=True)
        self.assertFalse(stock._auto_retain)
        self.assertEqual(stock._retain_tags, [])
        self.assertIsNone(stock._tags)
        self.assertIsNone(stock._observation_scopes)
        # The read filter still has to be set, including the user's own tag: a
        # specialist that cannot write can still be handed a user's session.
        self.assertEqual(stock._recall_tags, ["user:alice", SHARED_TAG])

    def test_the_prompt_says_read_only_and_says_not_to_cache(self):
        """#122: with no sanctioned route the specialist forked the corpus into its
        own skill file. Prose is the only mitigation the provider itself can carry."""
        p, _ = provider(read_only=True)
        block = p.system_prompt_block()
        self.assertIn("cannot write", block, block)
        self.assertIn("skill", block, block)
        # The #113 rule has to survive into this variant too.
        self.assertIn("Memory is a search, not an index", block, block)
        # No write guidance leaks in from the writable prompt.
        self.assertNotIn("memory_retain", block, block)

    def test_the_prompt_names_the_nomination_channel(self):
        """A read-only specialist is still the agent that discovers durable facts.
        The prompt has to say where one goes, or "you cannot write" reads as "throw
        it away" — and the corpus stays empty of anything a card ever learned."""
        p, _ = provider(read_only=True)
        block = p.system_prompt_block()
        self.assertIn("memory_candidates", block, block)
        # A nomination, not a write. The person on the other end of the card decides.
        self.assertIn("does not record", block, block)
        # The two exclusions travel with it, or the channel becomes a pipe for stale
        # cluster state — see SHARED_SCOPE_TEST for the same pair.
        self.assertIn("live state", block, block)
        self.assertIn("conclusion about the task in hand", block, block)
        # A run with no card still has the result block; the prompt must not make
        # the card the only route, or a cron finding has nowhere to go.
        self.assertIn("Worth remembering", block, block)
        self.assertIn("no card", block, block)

    def test_read_only_defaults_off_and_is_read_from_the_profile_config(self):
        """A profile that says nothing keeps its write tools; a broken config too."""
        read = kube_agents_memory.memory_is_read_only

        saved = sys.modules.get("hermes_cli.config")

        def with_config(value):
            sys.modules["hermes_cli.config"] = SimpleNamespace(load_config=lambda: value)
            try:
                return read()
            finally:
                if saved is None:
                    sys.modules.pop("hermes_cli.config", None)
                else:
                    sys.modules["hermes_cli.config"] = saved

        self.assertTrue(with_config({"memory": {"read_only": True}}))
        self.assertFalse(with_config({"memory": {"read_only": False}}))
        self.assertFalse(with_config({"memory": {}}))
        self.assertFalse(with_config({}))
        self.assertFalse(with_config(None))
        # A provider whose config read blows up must not silently go read-only and
        # drop the front door's writes on the floor.

        def exploding():
            raise RuntimeError("no profile")

        sys.modules["hermes_cli.config"] = SimpleNamespace(load_config=exploding)
        try:
            self.assertFalse(read())
        finally:
            if saved is None:
                sys.modules.pop("hermes_cli.config", None)
            else:
                sys.modules["hermes_cli.config"] = saved


if __name__ == "__main__":
    unittest.main()
