#!/usr/bin/env python3
"""Host tests for the KUBEAGENTS_DIAL Home tab patch. No Hermes install required.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py'

The fixture keeps the shape of upstream's adapter where the applier touches it:
``_handle_app_home_opened``'s Messages-only return and the plugin platform
handler wiring in ``_register_bolt_handlers``. The runtime is driven with the
stub adapter ``verify_slack_dial_home.py`` uses against the real tree.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import slack_dial_home as dial
import verify_slack_dial_home as verify
from apply_slack_dial_home import BUILD_MARKER, RELATIVE, apply

ADAPTER_SOURCE = '''\
"""Fixture standing in for plugins/platforms/slack/adapter.py."""
from typing import Optional


class SlackAdapter:
    def _register_bolt_handlers(self):
        self._register_plugin_action_handlers()
        self._wire_plugin_handlers(self._app)

    async def _authenticate_workspace(self, auth_response):
        team_id, bot_user_id = auth_response["team_id"], auth_response["user_id"]
        self._team_bot_user_ids[team_id] = bot_user_id
        if self._bot_user_id is None:
            self._bot_user_id = bot_user_id

    def _get_client(self, chat_id, team_id=None):
        return None

    async def _ensure_dm_conversation(self, chat_id, team_id=None):
        return chat_id

    def _is_interactive_user_authorized(self, user_id, *, channel_id="", user_name=None, team_id=""):
        return False

    @staticmethod
    def _event_team_id(event, body=None):
        return ""

    async def _handle_app_home_opened(self, event: dict, body: Optional[dict] = None) -> None:
        """Handle Slack Agent DM-open lifecycle events without producing replies."""
        if event.get("tab") != "messages":
            return
        self.seeded = True
'''


def run(coro):
    return asyncio.run(coro)


async def ack():
    return None


def shown(blocks):
    """The blocks as one string, with the marks and dots unescaped."""
    return json.dumps(blocks, ensure_ascii=False)


class ApplierTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        path = self.root / RELATIVE
        path.parent.mkdir(parents=True)
        path.write_text(ADAPTER_SOURCE)
        self.path = path

    def test_patched_adapter_passes_the_build_gate(self):
        apply(self.root)
        verify.check_adapter(self.path.read_text())

    def test_unpatched_adapter_fails_the_build_gate(self):
        with self.assertRaises(SystemExit):
            verify.check_adapter(ADAPTER_SOURCE)

    def test_second_run_is_refused(self):
        apply(self.root)
        with self.assertRaises(SystemExit) as caught:
            apply(self.root)
        self.assertIn(BUILD_MARKER, str(caught.exception))

    def test_the_dockerfiles_build_check_matches_what_the_applier_writes(self):
        # The Dockerfile greps the patched adapter right after running the
        # applier; a string the applier no longer writes fails every image build.
        dockerfile = (HERE.parent / "Dockerfile").read_text().splitlines()
        at = next(i for i, line in enumerate(dockerfile) if "apply_slack_dial_home.py" in line and "python3" in line)
        check = dockerfile[at + 1]
        self.assertIn("grep -qF", check)
        needle = check.split('"')[1]
        apply(self.root)
        self.assertIn(needle, self.path.read_text())
        self.assertNotIn(needle, ADAPTER_SOURCE)

    def test_moved_anchor_fails_without_writing(self):
        self.path.write_text(ADAPTER_SOURCE.replace('!= "messages"', 'not in ("messages",)'))
        before = self.path.read_text()
        with self.assertRaises(SystemExit):
            apply(self.root)
        self.assertEqual(self.path.read_text(), before)


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        patcher = mock.patch.dict(os.environ, {"HERMES_HOME": self.home, dial.FLAG_ENV: "on"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(dial.ADMINS_ENV, None)
        dial._dm_channels.clear()
        self.adapter = verify._Adapter(
            {"UADMIN", "UMEMBER", "UOWNER", "UGUEST"},
            {
                "UADMIN": {"is_admin": True, "team_id": "T1"},
                "UOWNER": {"is_owner": True, "team_id": "T1"},
                "UMEMBER": {"team_id": "T1"},
                "UGUEST": {"is_admin": True, "team_id": "T2"},
            },
        )

    def published(self):
        return self.adapter.client.published[-1][1]["blocks"]

    def pick(self, user, level):
        run(dial.on_level(self.adapter, ack, {"user": {"id": user}}, {"selected_option": {"value": level}}))

    def test_build_gate_behaviour(self):
        verify.check_runtime(dial)

    def test_switch_values(self):
        for value, on in (("on", True), (" ON ", True), ("off", False), ("", False), ("true", False)):
            with mock.patch.dict(os.environ, {dial.FLAG_ENV: value}):
                self.assertIs(dial.enabled(), on, value)

    def test_admin_sees_the_radio_on_the_recorded_level(self):
        run(dial.opened(self.adapter, {"user": "UADMIN", "channel": "D1"}))
        radio = next(b for b in self.published() if b["type"] == "actions")["elements"][0]
        self.assertEqual(radio["action_id"], dial.LEVEL_ACTION_ID)
        self.assertEqual([o["value"] for o in radio["options"]], ["explain", "propose", "automerge"])
        self.assertEqual(radio["initial_option"]["value"], dial.DEFAULT_LEVEL)

    def test_owner_counts_as_admin(self):
        self.pick("UOWNER", "automerge")
        self.assertEqual(dial.read_state()["level"], "automerge")

    def test_member_sees_marks_and_the_read_only_line(self):
        run(dial.opened(self.adapter, {"user": "UMEMBER"}))
        blocks = self.published()
        self.assertNotIn("actions", [b["type"] for b in blocks])
        text = shown(blocks)
        self.assertIn(f"{dial.MARK_ON}  *Propose fixes*", text)
        self.assertIn(dial.READ_ONLY, text)

    def test_member_pick_changes_nothing_and_says_why(self):
        self.pick("UADMIN", "propose")
        self.pick("UMEMBER", "explain")
        self.assertEqual(dial.read_state()["level"], "propose")
        self.assertIn(dial.READ_ONLY, shown(self.published()))

    def test_configured_admins_replace_workspace_admins(self):
        with mock.patch.dict(os.environ, {dial.ADMINS_ENV: " UMEMBER , "}):
            self.pick("UADMIN", "explain")
            self.assertEqual(dial.read_state()["set_by"], "")
            self.pick("UMEMBER", "explain")
            self.assertEqual(dial.read_state()["set_by"], "UMEMBER")

    def test_failed_lookup_is_read_only_and_says_so(self):
        async def broken(user):
            raise RuntimeError("missing_scope")

        self.adapter.client.users_info = broken
        run(dial.opened(self.adapter, {"user": "UADMIN"}))
        self.assertNotIn("actions", [b["type"] for b in self.published()])
        self.assertIn(dial.NOT_CHECKED, shown(self.published()))
        self.pick("UADMIN", "explain")
        self.assertEqual(dial.read_state()["set_by"], "")

    def test_stranger_pick_and_unknown_level_record_nothing(self):
        self.pick("USTRANGER", "explain")
        self.pick("UADMIN", "everything")
        self.assertFalse((Path(self.home) / dial.STATE_NAME).exists())

    def test_setter_shown_after_a_pick(self):
        self.pick("UADMIN", "explain")
        self.assertIn("Set to *Explain* by <@UADMIN>", shown(self.published()))

    def test_kind_auto_merges_only_at_the_auto_merge_level(self):
        path = Path(self.home) / dial.STATE_NAME
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"level": "propose", "kinds": {"missing-pdb": True}}))
        self.assertIn("Disruption budgets (dev) · off", shown(dial.blocks(dial.read_state(), False)))
        self.pick("UADMIN", "automerge")
        self.assertEqual(dial.read_state()["kinds"], {"missing-pdb": True})
        self.assertIn("Disruption budgets (dev) · auto-merges", shown(self.published()))

    def test_unreadable_record_is_the_default(self):
        path = Path(self.home) / dial.STATE_NAME
        path.parent.mkdir(parents=True)
        for raw in ("{not json", json.dumps(["explain"]), json.dumps({"level": "yolo"})):
            path.write_text(raw)
            self.assertEqual(dial.read_state()["level"], dial.DEFAULT_LEVEL, raw)

    def test_publish_failure_is_swallowed(self):
        async def refuse(user_id, view):
            raise RuntimeError("not_enabled")

        self.adapter.client.views_publish = refuse
        run(dial.opened(self.adapter, {"user": "UADMIN"}))

    def test_admin_of_another_workspace_is_not_an_install_admin(self):
        run(dial.opened(self.adapter, {"user": "UGUEST"}))
        self.assertNotIn("actions", [b["type"] for b in self.published()])
        self.pick("UGUEST", "automerge")
        self.assertEqual(dial.read_state()["set_by"], "")

    def test_unknown_home_workspace_is_read_only(self):
        self.adapter._team_bot_user_ids = {}
        run(dial.opened(self.adapter, {"user": "UADMIN"}))
        self.assertIn(dial.NOT_CHECKED, shown(self.published()))

    def test_tab_says_the_level_is_recorded_only(self):
        run(dial.opened(self.adapter, {"user": "UADMIN"}))
        self.assertIn(dial.NOT_ENFORCED, shown(self.published()))

    def test_pick_keeps_keys_it_does_not_own(self):
        path = Path(self.home) / dial.STATE_NAME
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"level": "propose", "kinds": {"missing-pdb": {"env": "dev"}}, "extra": 1}))
        self.pick("UADMIN", "explain")
        stored = json.loads(path.read_text())
        self.assertEqual(stored["kinds"], {"missing-pdb": {"env": "dev"}})
        self.assertEqual((stored["extra"], stored["level"]), (1, "explain"))

    def test_unreadable_record_is_never_overwritten(self):
        path = Path(self.home) / dial.STATE_NAME
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        self.pick("UADMIN", "explain")
        self.assertEqual(path.read_text(), "{not json")
        self.assertIn(dial.NOT_SAVED, shown(self.published()))

    def test_pick_after_a_restart_opens_the_dm_to_authorize(self):
        seen = []
        self.adapter._is_interactive_user_authorized = lambda user_id, **kw: seen.append(kw["channel_id"]) or True
        self.pick("UADMIN", "explain")
        self.assertEqual(seen[0], "DUADMIN")
        self.assertEqual(dial.read_state()["level"], "explain")

    def test_click_with_the_switch_off_answers_and_records_nothing(self):
        with mock.patch.dict(os.environ, {dial.FLAG_ENV: "off"}):
            self.pick("UADMIN", "explain")
        self.assertEqual(self.published(), [dial._section(dial.OFF)])
        self.assertFalse((Path(self.home) / dial.STATE_NAME).exists())

    def test_picking_the_default_level_is_recorded_or_says_not_saved(self):
        self.pick("UADMIN", "propose")
        self.assertEqual(json.loads((Path(self.home) / dial.STATE_NAME).read_text())["set_by"], "UADMIN")
        (Path(self.home) / dial.STATE_NAME).write_text("{broken")
        self.pick("UADMIN", "propose")
        self.assertIn(dial.NOT_SAVED, shown(self.published()))
        self.assertEqual((Path(self.home) / dial.STATE_NAME).read_text(), "{broken")

    def test_open_with_the_switch_off_clears_a_stale_dial(self):
        run(dial.opened(self.adapter, {"user": "UADMIN", "channel": "D1", "tab": "home"}))
        self.assertIn("actions", [b["type"] for b in self.published()])
        for value in ("off", "", "yes"):
            with mock.patch.dict(os.environ, {dial.FLAG_ENV: value}):
                run(dial.opened(self.adapter, {"user": "UMEMBER", "tab": "home"}))
            self.assertEqual(self.published(), [dial._section(dial.OFF)], value)
        self.assertFalse((Path(self.home) / dial.STATE_NAME).exists())

    def test_open_with_the_switch_off_checks_no_one(self):
        self.adapter._is_interactive_user_authorized = mock.Mock(side_effect=AssertionError("checked"))
        with mock.patch.dict(os.environ, {dial.FLAG_ENV: "off"}):
            run(dial.opened(self.adapter, {"user": "USTRANGER", "tab": "home"}))
        self.assertEqual(self.published(), [dial._section(dial.OFF)])

    def test_open_with_the_switch_off_survives_a_publish_failure(self):
        async def boom(user_id, view):
            raise RuntimeError("not_enabled")

        self.adapter.client.views_publish = boom
        with mock.patch.dict(os.environ, {dial.FLAG_ENV: "off"}):
            run(dial.opened(self.adapter, {"user": "UADMIN", "tab": "home"}))

    def test_pick_is_authorized_as_the_dm_it_was_opened_in(self):
        seen = []
        self.adapter._is_interactive_user_authorized = lambda user_id, **kw: seen.append(kw["channel_id"]) or True
        run(dial.opened(self.adapter, {"user": "UADMIN", "channel": "D42"}))
        self.pick("UADMIN", "explain")
        self.assertEqual(seen, ["D42", "D42", "D42"])


if __name__ == "__main__":
    unittest.main()
