#!/usr/bin/env python3
"""The scope tag is the isolation boundary, so two people cannot share one.

``user:<id>`` on every fact is the whole of what keeps one person's memories out
of another's recall — there is one bank, and the tag filter is the only thing
narrowing it. The readable half of that tag is produced by a lossy sanitizer
(everything outside ``[A-Za-z0-9_-]`` collapses to a dash), and identities are
email-shaped, so punctuation — exactly what the sanitizer destroys — is what
distinguishes many of them. A collision would be a two-way leak: A recalls B's
private memories, and A's turns retain under B's name.

This locks down the digest that prevents it, the empty-identity case that must
*not* get a digest (it has to stay falsy so the provider fails closed on personal
memory), and the parity between the provider's sanitizer and the copy in
``memory_file_import.py`` — the migration files entries under the tag the
provider later reads them back with, so a drift between the two strands every
migrated memory silently.

Standalone unittest suite. See ``test_recall_reporting.py`` for how to
run it.

    HERMES_ROOT=~/git/hermes-agent python3 tests/memory/test_user_tag_isolation.py
"""

import hashlib
import os
import sys
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERMES = os.environ.get("HERMES_ROOT") or "/opt/hermes"
if os.path.isdir(_HERMES):
    sys.path.insert(0, _HERMES)
sys.path.insert(0, os.path.join(_REPO, "agents", "chat", "plugins", "memory"))
sys.path.insert(0, os.path.join(_REPO, "agents", "chat", "scripts"))

try:
    from . import _stubs  # noqa: F401
except (ImportError, ValueError):
    import _stubs  # type: ignore # noqa: F401

import memory_file_import as mfi  # noqa: E402
from kube_agents_memory import (  # noqa: E402
    NO_IDENTITY_NOTICE,
    USER_TAG_PREFIX,
    KubeAgentsMemoryProvider,
    sanitize_user_id,
)

# Pairs that the readable half alone maps onto one string. All are plausible
# addresses, not adversarial input: the first two differ by one separator, the
# third moves a dot across the '@'.
COLLIDING = [
    ("alice.smith@corp.example", "alice-smith@corp.example"),
    ("alice+dev@corp.example", "alice/dev@corp.example"),
    ("alice@eng.corp.example", "alice.eng@corp.example"),
]


class TestUserTagIsolation(unittest.TestCase):
    def test_the_readable_half_really_does_collide(self):
        """Without this the rest of the file would be testing nothing."""
        for left, right in COLLIDING:
            readable = lambda s: sanitize_user_id(s).rsplit("_", 1)[0]  # noqa: E731
            self.assertEqual(readable(left), readable(right), (left, right))

    def test_colliding_identities_get_different_tags(self):
        for left, right in COLLIDING:
            self.assertNotEqual(sanitize_user_id(left), sanitize_user_id(right), (left, right))

    def test_the_tag_stays_readable(self):
        """A digest-only tag would make the bank unauditable by a person."""
        tag = sanitize_user_id("alice.smith@corp.example")
        self.assertTrue(tag.startswith("alice-smith-corp-example_"), tag)
        self.assertTrue(tag.endswith(hashlib.sha256(b"alice.smith@corp.example").hexdigest()[:12]), tag)

    def test_the_same_identity_always_gets_the_same_tag(self):
        """Not a session nonce — yesterday's memories have to come back today."""
        self.assertEqual(sanitize_user_id("alice@corp.example"), sanitize_user_id("alice@corp.example"))
        # Padding is a transport artefact, not a different person.
        self.assertEqual(sanitize_user_id("  alice@corp.example  "), sanitize_user_id("alice@corp.example"))

    def test_an_empty_identity_produces_no_tag(self):
        """Must stay falsy: ``initialize`` reads it as "nobody" and refuses."""
        for empty in ("", "   ", None):
            self.assertEqual(sanitize_user_id(empty), "", repr(empty))

    def test_an_identity_of_pure_punctuation_still_gets_a_tag(self):
        """Nothing readable survives, but the person is real and must be separable."""
        tag = sanitize_user_id("@@@")
        self.assertEqual(tag, hashlib.sha256(b"@@@").hexdigest()[:12], tag)
        self.assertNotEqual(tag, sanitize_user_id("///"))

    def test_no_identity_still_fails_closed_on_personal_memory(self):
        """The digest must not have turned "nobody" into a valid-looking user."""
        p = KubeAgentsMemoryProvider()
        p.initialize("session-1", user_id="")
        self.assertEqual(p._user_tag, "", p._user_tag)
        self.assertEqual(p._personal_disabled_reason, NO_IDENTITY_NOTICE)

    def test_an_identity_becomes_the_tag_the_provider_scopes_on(self):
        p = KubeAgentsMemoryProvider()
        p.initialize("session-2", user_id="alice.smith@corp.example", chat_type="dm")
        self.assertEqual(p._user_tag, f"{USER_TAG_PREFIX}{sanitize_user_id('alice.smith@corp.example')}")
        self.assertEqual(p._personal_disabled_reason, "")

    def test_the_migration_script_agrees_with_the_provider(self):
        """Two copies of one algorithm; a drift strands every migrated memory."""
        for left, right in COLLIDING:
            for raw in (left, right):
                self.assertEqual(mfi.sanitize_user_id(raw), sanitize_user_id(raw), raw)
        for edge in ("", "   ", "@@@", "  alice@corp.example  ", "slackbot"):
            self.assertEqual(mfi.sanitize_user_id(edge), sanitize_user_id(edge), repr(edge))


if __name__ == "__main__":
    unittest.main()
