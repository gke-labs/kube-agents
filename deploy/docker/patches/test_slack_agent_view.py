#!/usr/bin/env python3
"""Host tests for the Slack agent-view applier. No Hermes install required.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py'

The fixtures keep the shape of upstream's two files where the patch touches
them: the builder's agent-view description, and the adapter's
``suggested_prompts`` read. The patched modules are
imported and driven with the flag off and on, so the tests assert on the
manifest and prompts they return rather than on inserted text.
``verify_slack_agent_view.py`` does the same against the real tree in the image.
"""

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parents[2] / "agents" / "platform" / "scripts"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS))

from apply_slack_agent_view import (
    ADAPTER,
    BUILD_MARKER,
    FLAG_ENV,
    FLAG_ON_VALUES,
    MANIFEST,
    SUGGESTED_PROMPTS,
    apply,
)

try:
    import slack_presenter
except ImportError:  # the presenter ships with the Slack reactions change
    slack_presenter = None

STOP_EVENT = "agent_session_stopped"

MANIFEST_SOURCE = '''\
"""Fixture standing in for hermes_cli/slack_cli.py."""
from __future__ import annotations

import json
import sys


def _build_full_manifest(bot_name, bot_description, messaging_experience=None):
    features = {}
    bot_scopes = ["chat:write", "files:write", "reactions:read", "users:read"]
    bot_events = ["app_mention", "message.im"]
    if messaging_experience == "assistant":
        features["assistant_view"] = {"assistant_description": "d"}
        bot_scopes.append("assistant:write")
        bot_events.extend(["assistant_thread_context_changed", "assistant_thread_started"])
    elif messaging_experience == "agent":
        features["agent_view"] = {"agent_description": "Chat with Hermes in Slack Messages."}
        bot_scopes.append("assistant:write")
        bot_events.extend(["app_context_changed", "app_home_opened"])

    bot_scopes.sort()
    bot_events.sort()
    return {
        "features": features,
        "oauth_config": {"scopes": {"bot": bot_scopes}},
        "settings": {"event_subscriptions": {"bot_events": bot_events}},
    }


def slack_manifest_command(args) -> int:
    if getattr(args, "agent_view", False):
        messaging_experience = "agent"
    elif getattr(args, "no_assistant", False):
        messaging_experience = "none"
    else:
        messaging_experience = "assistant"
    name = getattr(args, "name", None) or "Hermes"
    manifest = _build_full_manifest(name, "d", messaging_experience=messaging_experience)
    sys.stdout.write(json.dumps(manifest))
    return 0


# ---- BEGIN PLUGIN-COMPAT ----
import os  # noqa: F401,E402
# ---- END PLUGIN-COMPAT ----
'''

ADAPTER_SOURCE = '''\
"""Fixture standing in for plugins/platforms/slack/adapter.py."""


class SlackAdapter:
    def _assistant_suggested_prompts(self):
        raw = self.config.extra.get("suggested_prompts")
        title = str(raw.get("title") or "").strip() if isinstance(raw, dict) else ""
        prompt_rows = raw.get("prompts") if isinstance(raw, dict) else raw
        if not isinstance(prompt_rows, list):
            return title, []
        prompts = []
        for item in prompt_rows:
            prompt_title = str(item.get("title") or "").strip()
            prompt_message = str(item.get("message") or "").strip()
            if prompt_title and prompt_message:
                prompts.append({"title": prompt_title[:75], "message": prompt_message})
            if len(prompts) >= 4:
                break
        return title, prompts


def register(ctx):
    ctx.register_platform(name="slack")
'''


def build(manifest=MANIFEST_SOURCE, adapter=ADAPTER_SOURCE):
    root = Path(tempfile.mkdtemp())
    for relative, source in ((MANIFEST, manifest), (ADAPTER, adapter)):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    return root


def load(root, relative):
    spec = importlib.util.spec_from_file_location(
        f"fixture_{id(root)}_{Path(relative).stem}", root / relative
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def manifest(module, **flags):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        module.slack_manifest_command(argparse.Namespace(**flags))
    return json.loads(out.getvalue())


def prompts(module, extra):
    adapter = SimpleNamespace(config=SimpleNamespace(extra=extra))
    return module.SlackAdapter._assistant_suggested_prompts(adapter)[1]


def flag(value):
    env = {k: v for k, v in os.environ.items() if k != FLAG_ENV}
    if value is not None:
        env[FLAG_ENV] = value
    return mock.patch.dict(os.environ, env, clear=True)


class FlagOffTest(unittest.TestCase):
    """Flag off, the patched tree behaves exactly as the unpatched one."""

    def test_manifest_matches_upstream_for_every_experience(self):
        original = build()
        patched = build()
        apply(patched)
        before = load(original, MANIFEST)
        after = load(patched, MANIFEST)
        for value in (None, "", "0", "false", "off"):
            for flags in ({}, {"agent_view": True}, {"no_assistant": True}, {"agent_view": True, "name": "kube-agents"}):
                with self.subTest(flag=value, args=flags), flag(value):
                    self.assertEqual(manifest(after, **flags), manifest(before, **flags))

    def test_prompts_match_upstream(self):
        original = build()
        patched = build()
        apply(patched)
        before = load(original, ADAPTER)
        after = load(patched, ADAPTER)
        configured = [{"title": "t", "message": "m"}]
        with flag(None):
            for extra in ({}, {"suggested_prompts": configured}):
                with self.subTest(extra=extra):
                    self.assertEqual(prompts(after, extra), prompts(before, extra))
            self.assertEqual(prompts(after, {}), [])


class FlagOnTest(unittest.TestCase):
    def setUp(self):
        self.root = build()
        apply(self.root)

    def test_only_agent_view_differs_from_upstream(self):
        # Agent view is one-way in Slack, so the flag never picks it, and a
        # manifest without --agent-view is upstream's.
        upstream = load(build(), MANIFEST)
        for value in ("1", "true", "TRUE", " yes ", "on"):
            for flags in ({}, {"no_assistant": True}, {"name": "kube-agents"}):
                with self.subTest(flag=value, args=flags), flag(value):
                    got = manifest(load(self.root, MANIFEST), **flags)
                    self.assertEqual(got, manifest(upstream, **flags))
                    self.assertNotIn("agent_view", got["features"])

    def test_agent_view_is_named_and_offers_no_stop(self):
        with flag("true"):
            got = manifest(load(self.root, MANIFEST), agent_view=True, name="kube-agents")
        self.assertEqual(got["features"]["agent_view"]["agent_description"], "Chat with kube-agents in Slack Messages.")
        events = got["settings"]["event_subscriptions"]["bot_events"]
        # Nothing handles a Stop press yet, so Slack must not offer one.
        self.assertNotIn(STOP_EVENT, events)
        self.assertIn("app_home_opened", events)

    def test_unset_prompts_fall_back_to_the_three_asks(self):
        with flag("true"):
            got = prompts(load(self.root, ADAPTER), {})
        self.assertEqual([row["message"] for row in got], list(SUGGESTED_PROMPTS))
        self.assertEqual([row["title"] for row in got], list(SUGGESTED_PROMPTS))
        for row in got:
            self.assertLessEqual(len(row["title"]), 75)

    def test_configured_prompts_win(self):
        configured = [{"title": "t", "message": "m"}]
        with flag("true"):
            module = load(self.root, ADAPTER)
            self.assertEqual(prompts(module, {"suggested_prompts": configured}), configured)
            # An explicit empty list is a statement too: no prompts.
            self.assertEqual(prompts(module, {"suggested_prompts": []}), [])


@unittest.skipIf(slack_presenter is None, "slack_presenter.py is not in the tree")
class FlagValuesTest(unittest.TestCase):
    def test_manifest_check_accepts_what_the_presenter_accepts(self):
        self.assertEqual(set(FLAG_ON_VALUES), set(slack_presenter.FLAG_ON_VALUES))


class RefusalTest(unittest.TestCase):
    def _refuses(self, expected, **sources):
        root = build(**sources)
        before = {rel: (root / rel).read_text() for rel in (MANIFEST, ADAPTER)}
        with self.assertRaises(SystemExit) as caught:
            apply(root)
        self.assertIn(expected, str(caught.exception))
        for rel, text in before.items():
            self.assertEqual((root / rel).read_text(), text, rel)

    def test_second_run_is_refused(self):
        root = build()
        apply(root)
        with self.assertRaises(SystemExit) as caught:
            apply(root)
        self.assertIn(BUILD_MARKER, str(caught.exception))

    def test_second_run_on_the_adapter_alone_is_refused(self):
        root = build()
        apply(root)
        (root / MANIFEST).write_text(MANIFEST_SOURCE)
        with self.assertRaises(SystemExit) as caught:
            apply(root)
        self.assertIn(BUILD_MARKER, str(caught.exception))

    def test_agent_description_moved(self):
        self._refuses(
            "agent view description",
            manifest=MANIFEST_SOURCE.replace("Chat with Hermes in Slack Messages.", "Chat in Slack."),
        )

    def test_prompt_read_moved(self):
        # The manifest would be patched and the adapter not; neither is written.
        self._refuses(
            "suggested prompts config read",
            adapter=ADAPTER_SOURCE.replace('extra.get("suggested_prompts")', 'extra["p"]'),
        )


if __name__ == "__main__":
    unittest.main()
