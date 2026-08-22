#!/usr/bin/env python3
"""A session with no user identity can write shared memory, and is told how.

Cron runs and the k8s event watcher have always been allowed to write — they
are not read-only, and shared is a scope that needs no identity. In three
months not one of them called the tool, because everything they were shown
pointed the other way: ``scope`` defaulted to ``personal``, which is the single
scope such a session is refused, and the schema gated ``shared`` on a fact "the
user states" when there is no user in the room.

Nothing here grants a new capability. It stops the schema and the default from
contradicting the permission the session already has, so the tests are about
what an unattended agent is *shown* and what its first, unqualified call does.

Two other populations are asserted alongside every unattended one, because a
change that widened to either would leak one person's facts to everyone — a
worse bug than the one being fixed. The attributed DM is the obvious one. The
group thread is the trap: it has no ``_user_tag`` either, so anything keyed on
that alone treats a room full of named people as an empty room.

Standalone: plain asserts, no pytest. See ``test_recall_reporting.py`` for how
to run it.

    HERMES_ROOT=~/git/hermes-agent python3 tests/memory/test_unattended_shared_writes.py
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

from kube_agents_memory import (  # noqa: E402
    NO_IDENTITY_NOTICE,
    SHARED_SESSION_NOTICE,
    SHARED_TAG,
    KubeAgentsMemoryProvider,
)
from kube_agents_memory.prompts import tool_schemas  # noqa: E402


def provider(*, user_tag="", unattended=None):
    """A writable provider in one of the three live states, over a stub client.

    ``unattended`` defaults to "whatever matches ``user_tag``" so the common two
    cases stay one argument; pass ``unattended=False`` with no tag to build the
    group thread, which is the state that has no identity and is still full of
    people.
    """
    if unattended is None:
        unattended = not user_tag
    assert not (user_tag and unattended), "an attributed session is not unattended"
    p = KubeAgentsMemoryProvider()
    p._read_only = False
    p._user_tag = user_tag
    p._unattended = unattended
    if user_tag:
        p._personal_disabled_reason = ""
    else:
        p._personal_disabled_reason = (
            NO_IDENTITY_NOTICE if unattended else SHARED_SESSION_NOTICE
        )
    retained = {}

    class StubClient:
        def aretain_batch(self, **kw):
            retained.update(kw)
            return SimpleNamespace(id="doc-1")

        def arecall(self, **kw):
            retained["recall"] = kw
            return SimpleNamespace(results=[])

    p._hindsight = SimpleNamespace(
        _bank_id="kube-agents-memory",
        _budget="low",
        _recall_max_tokens=4096,
        _recall_types=["observation"],
        _run_hindsight_operation=lambda op: op(StubClient()),
    )
    return p, retained


def _write_scope(p):
    for s in p.get_tool_schemas():
        if s["name"] == "memory_retain":
            return s["parameters"]["properties"]["scope"]
    raise AssertionError("memory_retain is not advertised")


def test_an_unattended_write_with_no_scope_reaches_shared_memory():
    """The whole bug, in one call: the shape an agent writes without thinking.

    The old default was 'personal', so this exact call returned an error in
    every cron and event session there has ever been.
    """
    p, retained = provider()
    r = json.loads(p.handle_tool_call("memory_retain", {
        "content": "Dataplane V2 is off on kage-management, so NetworkPolicies are inert."
    }))
    assert r.get("result") == "Stored in shared memory.", r
    assert retained["items"][0]["tags"] == [SHARED_TAG], retained


def test_an_attributed_write_with_no_scope_is_still_personal():
    """The guardrail on the line above: DMs must not start writing to everyone."""
    p, retained = provider(user_tag="user:alice")
    r = json.loads(p.handle_tool_call("memory_retain", {"content": "Alice prefers dry runs."}))
    assert r.get("result") == "Stored in personal memory.", r
    assert retained["items"][0]["tags"] == ["user:alice"], retained


def test_an_unqualified_write_in_a_group_thread_is_refused_not_published():
    """The mirror image of the headline test, and the reason it is keyed on
    `_unattended` rather than on `_user_tag` being empty.

    A space has no user tag and is not empty: every fact in it belongs to one
    of several named people. Defaulting the write to shared here would take
    "remember I'm on call next week" from one participant and publish it to
    every user of the install, with nothing said out loud about it.
    """
    p, retained = provider(unattended=False)
    r = json.loads(p.handle_tool_call("memory_retain", {
        "content": "Dmitry is on call for networking next week.",
    }))
    assert "error" in r, r
    assert r["error"] == SHARED_SESSION_NOTICE, r
    assert not retained, retained


def test_a_group_thread_can_still_write_shared_when_it_says_so():
    """Refusing the default must not amount to refusing the scope. A team-wide
    fact stated in a space is exactly what a deliberate shared write is for."""
    p, retained = provider(unattended=False)
    r = json.loads(p.handle_tool_call("memory_retain", {
        "content": "Releases are cut on Tuesdays.", "scope": "shared",
    }))
    assert r.get("result") == "Stored in shared memory.", r
    assert retained["items"][0]["tags"] == [SHARED_TAG], retained


def test_a_group_thread_keeps_personal_in_its_schema():
    """Dropping 'personal' from the enum would remove the only way to say "this
    is one person's" — and with it the refusal that keeps the fact unpublished.
    The space is told, in the description, that personal is the default."""
    scope = _write_scope(provider(unattended=False)[0])
    assert scope["enum"] == ["personal", "shared"], scope
    assert "no user identity" not in scope["description"], scope
    assert "nobody is present" not in scope["description"], scope


def test_asking_for_personal_without_an_identity_still_fails_loudly():
    """Defaulting to shared must not become 'silently reroute a personal write'.

    An explicit ``personal`` is a statement that this belongs to one person;
    quietly publishing it to every user instead would be a disclosure bug.
    """
    p, retained = provider()
    r = json.loads(p.handle_tool_call("memory_retain", {
        "content": "secret", "scope": "personal",
    }))
    assert "error" in r, r
    assert "no user identity" in r["error"], r
    assert not retained, retained


def test_the_unattended_schema_offers_shared_and_only_shared():
    """A scope the session is refused has no business in the enum."""
    scope = _write_scope(provider()[0])
    assert scope["enum"] == ["shared"], scope
    assert "only option" in scope["description"], scope
    # And the attributed session keeps both, or personal memory just vanished.
    assert _write_scope(provider(user_tag="user:alice")[0])["enum"] == ["personal", "shared"]


def test_every_variant_carries_the_test_for_what_belongs():
    """The old wording ('a fact the user states') excluded the unattended case
    by construction. Whatever replaces it has to reach all three."""
    for p in (provider()[0], provider(unattended=False)[0], provider(user_tag="user:alice")[0]):
        d = _write_scope(p)["description"]
        assert "could not find out for itself" in d, d
        # The two exclusions are the point; a description that keeps only the
        # invitation would fill the corpus with stale state and self-echo.
        assert "query that instead" in d, d
        assert "conclusion you reached this session" in d, d


def _retain_description(p):
    return next(s for s in p.get_tool_schemas() if s["name"] == "memory_retain")["description"]


def test_a_session_with_no_identity_is_not_told_capture_is_automatic():
    """It is not, for either of them — `_auto_retain` is off without a user tag,
    so the DM wording ('captured automatically at the end of a session') is a
    false reassurance in both. What follows it has to differ, though: the tool
    is the only route in for cron, and the last thing a space needs is
    encouragement to record what a named person just said."""
    unattended = _retain_description(provider()[0])
    assert "only way anything you learn here is kept" in unattended, unattended
    assert "captured automatically at the end of a session" not in unattended, unattended

    space = _retain_description(provider(unattended=False)[0])
    assert "only way anything you learn here is kept" not in space, space
    assert "belongs to the whole team" in space, space

    assert "captured automatically" in _retain_description(provider(user_tag="user:alice")[0])


def test_a_read_only_profile_is_unaffected_in_every_state():
    """Specialists stay barred; none of this reaches them."""
    for has_identity, unattended in ((True, False), (False, True), (False, False)):
        names = [
            s["name"] for s in tool_schemas(
                read_only=True, has_identity=has_identity, unattended=unattended,
            )
        ]
        assert "memory_retain" not in names, names


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
