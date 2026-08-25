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


class TestUnattendedSharedWrites(unittest.TestCase):
    def test_an_unattended_write_with_no_scope_reaches_shared_memory(self):
        """The whole bug, in one call: the shape an agent writes without thinking.

        The old default was 'personal', so this exact call returned an error in
        every cron and event session there has ever been.
        """
        p, retained = provider()
        r = json.loads(p.handle_tool_call("memory_retain", {
            "content": "Dataplane V2 is off on kage-management, so NetworkPolicies are inert."
        }))
        self.assertEqual(r.get("result"), "Stored in shared memory.", r)
        self.assertEqual(retained["items"][0]["tags"], [SHARED_TAG], retained)

    def test_an_attributed_write_with_no_scope_is_still_personal(self):
        """The guardrail on the line above: DMs must not start writing to everyone."""
        p, retained = provider(user_tag="user:alice")
        r = json.loads(p.handle_tool_call("memory_retain", {"content": "Alice prefers dry runs."}))
        self.assertEqual(r.get("result"), "Stored in personal memory.", r)
        self.assertEqual(retained["items"][0]["tags"], ["user:alice"], retained)

    def test_an_unqualified_write_in_a_group_thread_is_refused_not_published(self):
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
        self.assertIn("error", r, r)
        self.assertEqual(r["error"], SHARED_SESSION_NOTICE, r)
        self.assertFalse(retained, retained)

    def test_a_group_thread_can_still_write_shared_when_it_says_so(self):
        """Refusing the default must not amount to refusing the scope. A team-wide
        fact stated in a space is exactly what a deliberate shared write is for."""
        p, retained = provider(unattended=False)
        r = json.loads(p.handle_tool_call("memory_retain", {
            "content": "Releases are cut on Tuesdays.", "scope": "shared",
        }))
        self.assertEqual(r.get("result"), "Stored in shared memory.", r)
        self.assertEqual(retained["items"][0]["tags"], [SHARED_TAG], retained)

    def test_a_group_thread_keeps_personal_in_its_schema(self):
        """Dropping 'personal' from the enum would remove the only way to say "this
        is one person's" — and with it the refusal that keeps the fact unpublished.
        The space is told, in the description, that personal is the default."""
        scope = _write_scope(provider(unattended=False)[0])
        self.assertEqual(scope["enum"], ["personal", "shared"], scope)
        self.assertNotIn("no user identity", scope["description"], scope)
        self.assertNotIn("nobody is present", scope["description"], scope)

    def test_asking_for_personal_without_an_identity_still_fails_loudly(self):
        """Defaulting to shared must not become 'silently reroute a personal write'.

        An explicit ``personal`` is a statement that this belongs to one person;
        quietly publishing it to every user instead would be a disclosure bug.
        """
        p, retained = provider()
        r = json.loads(p.handle_tool_call("memory_retain", {
            "content": "secret", "scope": "personal",
        }))
        self.assertIn("error", r, r)
        self.assertIn("no user identity", r["error"], r)
        self.assertFalse(retained, retained)

    def test_the_unattended_schema_offers_shared_and_only_shared(self):
        """A scope the session is refused has no business in the enum."""
        scope = _write_scope(provider()[0])
        self.assertEqual(scope["enum"], ["shared"], scope)
        self.assertIn("only option", scope["description"], scope)
        # And the attributed session keeps both, or personal memory just vanished.
        self.assertEqual(_write_scope(provider(user_tag="user:x")[0])["enum"], ["personal", "shared"])

    def test_every_variant_carries_the_test_for_what_belongs(self):
        """The old wording ('a fact the user states') excluded the unattended case
        by construction. Whatever replaces it has to reach all three."""
        for p in (provider()[0], provider(unattended=False)[0], provider(user_tag="user:alice")[0]):
            d = _write_scope(p)["description"]
            self.assertIn("could not find out for itself", d, d)
            # The two exclusions are the point; a description that keeps only the
            # invitation would fill the corpus with stale state and self-echo.
            self.assertIn("query that instead", d, d)
            self.assertIn("conclusion you reached this session", d, d)

    def test_a_session_with_no_identity_is_not_told_capture_is_automatic(self):
        """It is not, for either of them — `_auto_retain` is off without a user tag,
        so the DM wording ('captured automatically at the end of a session') is a
        false reassurance in both. What follows it has to differ, though: the tool
        is the only route in for cron, and the last thing a space needs is
        encouragement to record what a named person just said."""
        def _retain_description(p):
            return next(s for s in p.get_tool_schemas() if s["name"] == "memory_retain")["description"]

        unattended = _retain_description(provider()[0])
        self.assertIn("only way anything you learn here is kept", unattended, unattended)
        self.assertNotIn("captured automatically at the end of a session", unattended, unattended)

        space = _retain_description(provider(unattended=False)[0])
        self.assertNotIn("only way anything you learn here is kept", space, space)
        self.assertIn("belongs to the whole team", space, space)

        self.assertIn("captured automatically", _retain_description(provider(user_tag="user:alice")[0]))

    def test_a_read_only_profile_is_unaffected_in_every_state(self):
        """Specialists stay barred; none of this reaches them."""
        for has_identity, unattended in ((True, False), (False, True), (False, False)):
            names = [
                s["name"] for s in tool_schemas(
                    read_only=True, has_identity=has_identity, unattended=unattended,
                )
            ]
            self.assertNotIn("memory_retain", names, names)


if __name__ == "__main__":
    unittest.main()
