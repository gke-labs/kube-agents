#!/usr/bin/env python3
"""Host tests for the Slack reactions-scope applier. No Hermes install required.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py'

The in-image gate (``verify_slack_reactions_scope.py``) drives the real
``_build_full_manifest`` and is the authority on what the shipped CLI prints.
These cover what is cheaper and sharper to test here: the drift cases, which a
healthy image never reaches and so never exercises — upstream renaming the list,
dropping ``reactions:read``, or reformatting it — plus the indentation the
insert has to reproduce.

The fixture mirrors the shape of upstream's function rather than its contents:
the two ``append`` branches and the ``sort`` are what make the emitted list
differ from the source literal, so the tests can exec the patched module and
assert on the manifest instead of on the text just inserted. The list is
written several scopes per line, as upstream has spelled it since v2026.9.14;
``ONE_PER_LINE`` is the v2026.8.19 spelling, kept so the insert is proven
against both.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from apply_slack_reactions_scope import (  # noqa: E402
    BUILD_MARKER,
    RELATIVE,
    WRITE_SCOPE,
    apply,
)

UPSTREAM = '''\
"""Fixture standing in for hermes_cli/slack_cli.py."""
from __future__ import annotations


def _build_full_manifest(
    bot_name: str,
    bot_description: str,
    messaging_experience: str | None = None,
) -> dict:
    bot_scopes = [
        "app_mentions:read", "chat:write", "commands",
        "files:write", "reactions:read", "users:read"]

    bot_events = [
        "app_mention",
        "reaction_added",
        "reaction_removed",
    ]

    if messaging_experience == "assistant":
        bot_scopes.append("assistant:write")
    elif messaging_experience == "agent":
        bot_scopes.append("assistant:write")

    bot_scopes.sort()
    bot_events.sort()

    return {
        "oauth_config": {"scopes": {"bot": bot_scopes}},
        "settings": {"event_subscriptions": {"bot_events": bot_events}},
    }
'''


# The same list one scope per line, which is how upstream wrote it through
# v2026.8.19 and how a formatter could write it again.
ONE_PER_LINE = UPSTREAM.replace(
    '''\
    bot_scopes = [
        "app_mentions:read", "chat:write", "commands",
        "files:write", "reactions:read", "users:read"]
''',
    '''\
    bot_scopes = [
        "app_mentions:read",
        "chat:write",
        "commands",
        "files:write",
        "reactions:read",
        "users:read",
    ]
''',
)
assert ONE_PER_LINE != UPSTREAM


def build(source=UPSTREAM):
    """Write ``source`` to a throwaway Hermes root and return the root."""
    root = Path(tempfile.mkdtemp())
    target = root / RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source)
    return root


def load(root):
    """Import the patched fixture and return its module object."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        f"slack_cli_fixture_{id(root)}", root / RELATIVE
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ApplyTest(unittest.TestCase):
    def test_scope_reaches_every_emitted_manifest(self):
        root = build()
        apply(root)
        module = load(root)
        for experience in ("assistant", "agent", "none", None):
            with self.subTest(messaging_experience=experience):
                manifest = module._build_full_manifest(
                    "Hermes", "test", messaging_experience=experience
                )
                scopes = manifest["oauth_config"]["scopes"]["bot"]
                self.assertEqual(scopes.count(WRITE_SCOPE), 1, scopes)
                # The sort is upstream's; this pins that we did not disturb it.
                self.assertEqual(scopes, sorted(scopes))

    def test_insert_follows_reactions_read_on_a_packed_line(self):
        root = build()
        apply(root)
        source = (root / RELATIVE).read_text()
        self.assertIn(
            '"files:write", "reactions:read", "reactions:write", "users:read"]',
            source,
        )

    def test_one_scope_per_line_gets_a_line_of_its_own(self):
        root = build(ONE_PER_LINE)
        apply(root)
        source = (root / RELATIVE).read_text()
        self.assertIn(
            '        "reactions:read",\n        "reactions:write",\n', source
        )
        manifest = load(root)._build_full_manifest("Hermes", "test")
        self.assertEqual(
            manifest["oauth_config"]["scopes"]["bot"].count(WRITE_SCOPE), 1
        )

    def test_indentation_is_taken_from_the_anchor_not_assumed(self):
        # Upstream could reindent the literal — a nested helper, a different
        # formatter. The insert has to follow it rather than hard-code eight
        # spaces, or the patched file stops parsing. Every element moves, as
        # a reindent would move them: the insert reuses the separator that
        # follows the anchor element, so reindenting that line alone would
        # only prove the next line's indent was copied.
        root = build(ONE_PER_LINE.replace('\n        "', '\n            "'))
        apply(root)
        self.assertIn(
            '            "reactions:read",\n            "reactions:write",\n',
            (root / RELATIVE).read_text(),
        )

    def test_second_run_is_refused(self):
        root = build()
        apply(root)
        with self.assertRaises(SystemExit) as caught:
            apply(root)
        self.assertIn(BUILD_MARKER, str(caught.exception))
        # And the first run's result is left exactly as it was.
        self.assertEqual(
            (root / RELATIVE).read_text().count(f'"{WRITE_SCOPE}"'), 1
        )


class DriftTest(unittest.TestCase):
    def _refuses(self, source, expected):
        root = build(source)
        before = (root / RELATIVE).read_text()
        with self.assertRaises(SystemExit) as caught:
            apply(root)
        self.assertIn(expected, str(caught.exception))
        # Nothing is written on a refusal.
        self.assertEqual((root / RELATIVE).read_text(), before)

    def test_list_renamed(self):
        self._refuses(
            UPSTREAM.replace("bot_scopes", "bot_oauth_scopes"),
            "expected 1 assignment to bot_scopes",
        )

    def test_reactions_read_dropped(self):
        # Upstream removing the read scope means it has stopped supporting
        # reactions; granting write into that is worse than failing the build.
        self._refuses(
            UPSTREAM.replace('"reactions:read", ', ""),
            "no longer holds 'reactions:read'",
        )

    def test_reactions_read_as_a_bare_last_element(self):
        # expect_contains still passes here — the literal is a list and still
        # holds the scope — but there is no comma-and-separator to insert after,
        # so the text-level check is what has to refuse rather than guess.
        self._refuses(
            UPSTREAM.replace(
                '"files:write", "reactions:read", "users:read"]',
                '"files:write", "users:read", "reactions:read"]',
            ),
            'expected 1 "reactions:read" element followed by a comma',
        )

    def test_scopes_not_a_list_literal(self):
        self._refuses(
            UPSTREAM.replace(
                '    bot_scopes = [\n        "app_mentions:read",',
                '    bot_scopes = list(_DEFAULT_SCOPES) + [\n        "app_mentions:read",',
            ),
            "not a tuple/list/set literal",
        )


if __name__ == "__main__":
    unittest.main()
