#!/usr/bin/env python3
"""Regression test for #784: what the wrapper forwards has to be bindable.

The defect it locks down: ``sync_turn`` declared ``**kwargs`` and forwarded them
verbatim. The harness reads any ``**kwargs`` as consent to send the completed
turn's ``messages`` list (``MemoryManager._provider_sync_accepts_messages``); the
stock provider's ``sync_turn`` takes ``session_id`` and nothing else. Every
attributed turn raised ``TypeError`` inside ``_call``, which logged at DEBUG and
returned None. Automatic capture wrote nothing, ever, and said nothing about it.

``tests/memory/test_read_only_profile.py::test_automatic_capture_is_off`` covered
this path and could not fail: it stubs ``_call`` — the method that forwards and
swallows — calls ``p.sync_turn("u", "a")`` with no keywords at all, and stands the
stock provider up as a ``SimpleNamespace`` with no ``sync_turn`` on it. It asserts
the wrapper *decides* to forward. This file asserts the forward *arrives*.

So the two rules here are:

  1. Drive the hooks through ``MemoryManager``, never by hand. The keyword that
     broke this is one the manager adds and no direct call would.
  2. Check every forward against the **real** stock signature, not a fake's. A
     stand-in stuck on last release's parameters agrees with a wrapper stuck on
     last release's parameters, which is the blind spot #780 shipped through.

Standalone: plain asserts, no pytest. Needs Hermes on the path for
``agent.memory_manager`` and ``plugins.memory.hindsight`` — the real signatures are
the point, so this one cannot stub its way out of that dependency. It never
reaches a real Hindsight; the calls land on recording stubs.

    HERMES_ROOT=~/git/hermes-agent python3 tests/memory/test_forwarding_matches_hindsight.py

Inside the agent image, Hermes is already importable:

    /opt/hermes/.venv/bin/python3 tests/memory/test_forwarding_matches_hindsight.py
"""

import inspect
import logging
import os
import sys
from types import SimpleNamespace

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERMES = os.environ.get("HERMES_ROOT") or "/opt/hermes"
if os.path.isdir(_HERMES):
    sys.path.insert(0, _HERMES)
sys.path.insert(0, os.path.join(_REPO, "agents", "chat", "plugins", "memory"))

from agent.memory_manager import MemoryManager  # noqa: E402
from kube_agents_memory import KubeAgentsMemoryProvider  # noqa: E402
from plugins.memory.hindsight import HindsightMemoryProvider as Stock  # noqa: E402

# Every method KubeAgentsMemoryProvider hands to _call. ``on_session_end`` is on
# the list because the wrapper forwards it even though the stock provider does
# not implement it — that resolves to the empty base method, which is why there
# was no fallback write path when sync_turn broke.
FORWARDED = (
    "shutdown",
    "queue_prefetch",
    "prefetch",
    "on_turn_start",
    "on_session_switch",
    "sync_turn",
    "on_session_end",
)


def _recording_stub(method, log):
    """A stand-in for ``Stock.method`` that reports the real signature.

    ``__signature__`` is what ``inspect.signature`` reads, so the wrapper's
    keyword filter sees exactly the parameters the deployed provider has, while
    the stub itself still accepts anything — a stub that rejected the bad call
    would only prove ``_call`` catches exceptions, which was never in doubt.
    """
    real = inspect.signature(getattr(Stock, method))
    # Drop ``self``: the wrapper reaches this through an instance attribute.
    without_self = real.replace(parameters=list(real.parameters.values())[1:])

    def stub(*a, **kw):
        log.append((method, a, kw))
        return None

    stub.__name__ = method
    stub.__signature__ = without_self
    return stub


def provider(*, read_only=False, user_tag="user:alice"):
    """An attributed, writable session whose forwards are recorded."""
    p = KubeAgentsMemoryProvider()
    p._read_only = read_only
    p._user_tag = user_tag
    p._session_id = "20260818_120000_abcd1234"
    log = []
    p._hindsight = SimpleNamespace(**{m: _recording_stub(m, log) for m in FORWARDED})
    return p, log


def manager_for(p):
    """A MemoryManager that syncs inline.

    ``sync_all`` dispatches to a background worker so a slow provider cannot
    stall the turn. That is right in production and useless in a test, and the
    thread is not what is under test here — the argument list is.
    """
    m = MemoryManager()
    m._submit_background = lambda fn, **kw: fn()
    m.add_provider(p)
    return m


def assert_binds(log):
    """Every recorded forward must bind against the real stock signature."""
    for method, a, kw in log:
        real = getattr(Stock, method)
        try:
            inspect.signature(real).bind(object(), *a, **kw)
        except TypeError as e:
            raise AssertionError(
                f"{method}{a!r} {kw!r} would not bind against "
                f"Stock.{method}{inspect.signature(real)}: {e}"
            )


def test_the_harness_always_sends_messages_to_this_provider():
    """The trap, stated as an assertion so it cannot quietly stop being true.

    ``**kwargs`` on sync_turn is read as "send everything". Narrowing the
    wrapper's own signature to make this False would silence the bug by giving
    up the message context for good, so the fix has to survive this staying True.
    """
    p, _ = provider()
    assert MemoryManager._provider_sync_accepts_messages(p) is True


def test_a_synced_turn_lands_on_the_stock_provider():
    p, log = provider()
    manager_for(p).sync_all(
        "what is RB-114?",
        "Drain the node before upgrading.",
        session_id="20260818_120000_abcd1234",
        messages=[{"role": "user", "content": "what is RB-114?"}],
    )
    assert [m for m, _, _ in log] == ["sync_turn"], log
    assert_binds(log)

    _, args, kwargs = log[0]
    assert args == ("what is RB-114?", "Drain the node before upgrading."), args
    # session_id survives — it is what ties a retained document back to a
    # conversation, and dropping it would turn one bug into a quieter one.
    assert kwargs == {"session_id": "20260818_120000_abcd1234"}, kwargs


def test_a_read_only_profile_still_syncs_nothing():
    """#112's guarantee, re-checked through the real caller this time."""
    p, log = provider(read_only=True)
    manager_for(p).sync_all("u", "a", session_id="s", messages=[{"role": "user"}])
    assert log == [], log


def test_every_forwarded_hook_binds():
    """The general drift guard: not just the one method that broke.

    A base image can narrow any of these. Exercising them together means the
    next mismatch fails here rather than in a bank nobody thinks to query.
    """
    p, log = provider()
    p.queue_prefetch("RB-114", session_id="s1")
    p.prefetch("RB-114", session_id="s1")
    p.on_turn_start(3, "what is RB-114?", session_id="s1", messages=[])
    p.on_session_switch("s2", user_id="alice@example.com")
    p.on_session_end([{"role": "user", "content": "u"}])
    p.shutdown()
    manager_for(p).sync_all("u", "a", session_id="s2", messages=[{"role": "user"}])

    assert sorted({m for m, _, _ in log}) == sorted(FORWARDED), log
    assert_binds(log)


def test_a_keyword_the_target_learns_about_is_forwarded():
    """Filtering has to be read off the target, not hardcoded.

    Spelling out today's parameters would drop ``messages`` forever, including
    the day the stock provider grows a use for it. That is the failure #780
    removed from the Slack relay's register() shim, in the opposite direction.
    """
    p, log = provider()

    def wide(user_content, assistant_content, *, session_id="", messages=None):
        log.append(("sync_turn", (user_content, assistant_content),
                    {"session_id": session_id, "messages": messages}))

    p._hindsight.sync_turn = wide
    manager_for(p).sync_all("u", "a", session_id="s1", messages=[{"role": "user"}])
    assert log[0][2]["messages"] == [{"role": "user"}], log


def test_a_failed_forward_is_not_silent():
    """DEBUG is what let this run for five days; the level is part of the fix."""
    p, log = provider()

    def explodes(*a, **kw):
        raise RuntimeError("hindsight daemon is unreachable")

    explodes.__signature__ = inspect.signature(_recording_stub("sync_turn", log))
    p._hindsight.sync_turn = explodes

    records = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("kube_agents_memory.session")
    handler = Capture()
    logger.addHandler(handler)
    try:
        manager_for(p).sync_all("u", "a", session_id="s1", messages=[{"role": "user"}])
    finally:
        logger.removeHandler(handler)

    warned = [r for r in records if r.levelno >= logging.WARNING]
    assert warned, [(r.levelname, r.getMessage()) for r in records]
    assert "sync_turn" in warned[0].getMessage(), warned[0].getMessage()
    # The traceback has to come with it, or the line names the method and not
    # the reason, and the next person is back to guessing.
    assert warned[0].exc_info is not None


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
