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
assert on the manifest instead of on the text just inserted.
"""

import sys
import tempfile
import textwrap
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
        "app_mentions:read",
        "chat:write",
        "commands",
        "files:write",
        "reactions:read",
        "users:read",
    ]

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

    def test_insert_follows_reactions_read_at_its_own_indent(self):
        root = build()
        apply(root)
        source = (root / RELATIVE).read_text()
        self.assertIn(
            '        "reactions:read",\n        "reactions:write",\n', source
        )

    def test_indentation_is_taken_from_the_anchor_not_assumed(self):
        # Upstream could reindent the literal — a nested helper, a different
        # formatter. The insert has to follow it rather than hard-code eight
        # spaces, or the patched file stops parsing.
        root = build(
            UPSTREAM.replace(
                '        "reactions:read",', '            "reactions:read",'
            )
        )
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
            UPSTREAM.replace('        "reactions:read",\n', ""),
            "no longer holds 'reactions:read'",
        )

    def test_list_collapsed_onto_one_line(self):
        # expect_contains still passes here — the literal is a list and still
        # holds the scope — so the line-anchored insert is what has to refuse.
        self._refuses(
            UPSTREAM.replace(
                textwrap.dedent('''\
                    bot_scopes = [
                            "app_mentions:read",
                            "chat:write",
                            "commands",
                            "files:write",
                            "reactions:read",
                            "users:read",
                        ]'''),
                'bot_scopes = ["app_mentions:read", "chat:write", "commands", '
                '"files:write", "reactions:read", "users:read"]',
            ),
            'expected 1 "reactions:read" element on a line of its own',
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
