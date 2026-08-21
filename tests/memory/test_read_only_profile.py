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
from types import SimpleNamespace

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERMES = os.environ.get("HERMES_ROOT") or "/opt/hermes"
if os.path.isdir(_HERMES):
    sys.path.insert(0, _HERMES)
sys.path.insert(0, os.path.join(_REPO, "agents", "chat", "plugins", "memory"))

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


def test_the_write_tool_is_not_advertised():
    """Advertising it and refusing the call would read as a transient failure."""
    assert "memory_retain" not in _names(provider(read_only=True)[0])
    # ...and is still there when the setting is off, or every profile just lost
    # its write path.
    assert "memory_retain" in _names(provider(read_only=False)[0])


def test_reads_are_untouched():
    p, calls = provider(read_only=True)
    assert _names(p) == ["memory_recall", "memory_reflect"], _names(p)
    r = json.loads(p.handle_tool_call("memory_recall", {"query": "RB-114"}))
    assert r["status"] == "found", r
    assert "RB-114" in r["result"], r
    assert calls["recall"]["tags"] == ["user:alice", SHARED_TAG], calls


def test_the_write_call_is_refused_anyway():
    """Backstop: an invented call, or a schema cached across a config change."""
    p, calls = provider(read_only=True)
    r = json.loads(p.handle_tool_call("memory_retain", {"content": "x", "scope": "shared"}))
    assert r["status"] == "read_only", r
    assert "read-only" in r["error"], r
    # The refusal has to land before anything reaches Hindsight.
    assert "retain" not in calls, calls
    # And it must not read as retryable — a model that retries burns the task.
    assert "retrying will not change that" in r["error"], r


def test_automatic_capture_is_off():
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
    assert seen == [], seen

    # Same hooks, writable profile: both must fire, or this test would pass on a
    # provider that had simply stopped working.
    p, _ = provider(read_only=False)
    seen = []
    p._call = lambda name, *a, **kw: seen.append(name)
    p.sync_turn("u", "a")
    p.on_session_end([{"role": "user", "content": "u"}])
    assert seen == ["sync_turn", "on_session_end"], seen


def test_scoping_clears_the_stock_providers_own_write_state():
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
    assert stock._auto_retain is False
    assert stock._retain_tags == []
    assert stock._tags is None
    assert stock._observation_scopes is None
    # The read filter still has to be set, including the user's own tag: a
    # specialist that cannot write can still be handed a user's session.
    assert stock._recall_tags == ["user:alice", SHARED_TAG]


def test_the_prompt_says_read_only_and_says_not_to_cache():
    """#122: with no sanctioned route the specialist forked the corpus into its
    own skill file. Prose is the only mitigation the provider itself can carry."""
    p, _ = provider(read_only=True)
    block = p.system_prompt_block()
    assert "cannot write" in block, block
    assert "skill" in block, block
    # The #113 rule has to survive into this variant too.
    assert "Memory is a search, not an index" in block, block
    # No write guidance leaks in from the writable prompt.
    assert "memory_retain" not in block, block


def test_the_prompt_names_the_nomination_channel():
    """A read-only specialist is still the agent that discovers durable facts.
    The prompt has to say where one goes, or "you cannot write" reads as "throw
    it away" — and the corpus stays empty of anything a card ever learned."""
    p, _ = provider(read_only=True)
    block = p.system_prompt_block()
    assert "memory_candidates" in block, block
    # A nomination, not a write. The person on the other end of the card decides.
    assert "does not record" in block, block
    # The two exclusions travel with it, or the channel becomes a pipe for stale
    # cluster state — see SHARED_SCOPE_TEST for the same pair.
    assert "live state" in block, block


def test_read_only_defaults_off_and_is_read_from_the_profile_config():
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

    assert with_config({"memory": {"read_only": True}}) is True
    assert with_config({"memory": {"read_only": False}}) is False
    assert with_config({"memory": {}}) is False
    assert with_config({}) is False
    assert with_config(None) is False
    # A provider whose config read blows up must not silently go read-only and
    # drop the front door's writes on the floor.

    def exploding():
        raise RuntimeError("no profile")

    sys.modules["hermes_cli.config"] = SimpleNamespace(load_config=exploding)
    try:
        assert read() is False
    finally:
        if saved is None:
            sys.modules.pop("hermes_cli.config", None)
        else:
            sys.modules["hermes_cli.config"] = saved


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"ok    {name}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL  {name}: {e}")
    print("\nall pass" if not failed else f"\n{failed} failed")
    sys.exit(1 if failed else 0)
