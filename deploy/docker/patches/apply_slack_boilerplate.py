#!/usr/bin/env python3
"""Wire gateway/slack_boilerplate.py into the cron delivery, the heartbeat and the notices.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree, after
``slack_boilerplate.py`` has been copied to ``gateway/``. Five files:

``cron/scheduler_delivery.py``: ``_deliver_result`` imports the module beside
its own ``BasePlatformAdapter`` import, and each target's two send lanes take
``cron_delivery_text(...)`` in place of the shared ``cleaned_delivery_content``.
Imported in the function, as that function already imports ``gateway``: a
module-level import would run while ``cron.scheduler`` is still loading.

``gateway/run_turn.py``: the heartbeat's display mode passes through
``long_running_mode`` straight after it is read.

``gateway/run_notifications.py``: the post-restart notice returns before its
send when ``drop_notice`` says so (the ``finally`` still unlinks the marker),
and the home-channel startup loop skips the channel before calling
``_send_home_channel_message``. The helper itself is untouched; the
session-database warnings, which share it, skip a Slack home channel the same
way in their own loop.

``gateway/run_busy.py``: the one-time busy-input onboarding hint is neither
appended nor marked seen when ``drop_notice`` says the busy message came from
Slack.

``plugins/platforms/slack/adapter.py``: ``SlackAdapter.send`` passes its
content through ``system_text`` once the DM target is resolved, the one place
every busy ack, drain refusal, slash-command reply, background-task update,
provider error reply and interrupting notice (``run_shutdown``'s
``_send_notice_logged``) passes on the way to Slack, and
``SlackAdapter.edit_message`` does the
same once its outbound check passes, for a reply that arrives as a streamed or
edited message. Runs after
``apply_slack_ux_reactions.py``, which leaves ``send`` alone.

With the flag off every helper returns its input unchanged, so the calls are
upstream's. What the flag changes, and why, is in the module docstring of
``deploy/docker/patches/slack_boilerplate.py``. Anchors are derived against
v2026.9.14.

Usage::

    python3 apply_slack_boilerplate.py [HERMES_ROOT]  # /opt/hermes
"""

from __future__ import annotations

import sys
from pathlib import Path

import patchlib

PREFIX = "slack_boilerplate"

#: Asserted in the built bundle by the Dockerfile, and the second-run guard.
BUILD_MARKER = "_kage_slack_boilerplate."

#: The module-level import appended to the gateway files.
GATEWAY_IMPORT = (
    "\n\n# kube-agents patch: see gateway/slack_boilerplate.py\n"
    "from gateway import slack_boilerplate as _kage_slack_boilerplate  # noqa: E402\n"
)

DELIVERY = "cron/scheduler_delivery.py"

DELIVERY_IMPORT_ANCHOR = "    from gateway.platforms.base import BasePlatformAdapter\n"
DELIVERY_IMPORT_PATCHED = DELIVERY_IMPORT_ANCHOR + (
    "    # kube-agents patch: see gateway/slack_boilerplate.py\n"
    "    from gateway import slack_boilerplate as _kage_slack_boilerplate\n"
)

DELIVERY_SEND_ANCHOR = (
    "        target_errors: list = []\n"
    "        delivered = t.live_adapter_ready and _deliver_via_live_adapter(\n"
    "            t, cleaned_delivery_content, media_files,\n"
    "            target_errors=target_errors, delivery_errors=delivery_errors,\n"
    "            unverified_targets=unverified_targets,\n"
    "        )\n"
    "        if not delivered:\n"
    "            _deliver_standalone(\n"
    "                t, cleaned_delivery_content, media_files, target_errors, delivery_errors)\n"
)
DELIVERY_SEND_PATCHED = (
    "        # kube-agents patch: KAGE_SLACK_UX sends Slack the unwrapped report; see\n"
    "        # gateway/slack_boilerplate.py. Off, this is cleaned_delivery_content.\n"
    "        target_text = _kage_slack_boilerplate.cron_delivery_text(\n"
    "            t, content, cleaned_delivery_content, BasePlatformAdapter.extract_media)\n"
    "        target_errors: list = []\n"
    "        delivered = t.live_adapter_ready and _deliver_via_live_adapter(\n"
    "            t, target_text, media_files,\n"
    "            target_errors=target_errors, delivery_errors=delivery_errors,\n"
    "            unverified_targets=unverified_targets,\n"
    "        )\n"
    "        if not delivered:\n"
    "            _deliver_standalone(\n"
    "                t, target_text, media_files, target_errors, delivery_errors)\n"
)

RUN_TURN = "gateway/run_turn.py"

HEARTBEAT_ANCHOR = (
    '        _long_running_mode = disp._display_surface_mode("long_running_notifications",'
    " default=True, allow_generic=True)\n"
)
HEARTBEAT_PATCHED = HEARTBEAT_ANCHOR + (
    "        # kube-agents patch: KAGE_SLACK_UX makes the heartbeat generic on Slack;\n"
    "        # see gateway/slack_boilerplate.py. Off, the mode is unchanged.\n"
    "        _long_running_mode = _kage_slack_boilerplate.long_running_mode(turn_ctx.source, _long_running_mode)\n"
)

RUN_NOTIFICATIONS = "gateway/run_notifications.py"

RESTARTED_ANCHOR = (
    "            result = await transport.send(\n"
    '                platform, str(chat_id), "♻ Gateway restarted successfully. Your session continues.",\n'
)
RESTARTED_PATCHED = (
    "            # kube-agents patch: KAGE_SLACK_UX keeps this notice off Slack; see\n"
    "            # gateway/slack_boilerplate.py. The finally below still unlinks the marker.\n"
    "            if _kage_slack_boilerplate.drop_notice(platform):\n"
    '                logger.info("Restart notification to %s:%s not sent: KAGE_SLACK_UX", platform_str, chat_id)\n'
    "                return None\n"
) + RESTARTED_ANCHOR

STARTUP_ANCHOR = (
    "            target = _notice_target_key(platform.value, home.chat_id, home.thread_id)\n"
    "            if target in skipped or target in delivered:\n"
    "                continue\n"
)
STARTUP_PATCHED = (
    "            # kube-agents patch: KAGE_SLACK_UX keeps this notice off Slack; see\n"
    "            # gateway/slack_boilerplate.py.\n"
    "            if _kage_slack_boilerplate.drop_notice(platform):\n"
    '                logger.info("Home-channel startup notification to %s not sent: KAGE_SLACK_UX", platform.value)\n'
    "                continue\n"
) + STARTUP_ANCHOR

SESSION_DB_ANCHOR = (
    "        for platform, _platform_cfg, home, transport in self._home_channel_transports():\n"
    "            await self._send_home_channel_message(\n"
    '                platform, home, transport, message, "state.db warning notification failed for %s:%s: %s",\n'
)
SESSION_DB_PATCHED = (
    "        for platform, _platform_cfg, home, transport in self._home_channel_transports():\n"
    "            # kube-agents patch: KAGE_SLACK_UX keeps this warning off Slack (its\n"
    "            # error is logged above); see gateway/slack_boilerplate.py.\n"
    "            if _kage_slack_boilerplate.drop_notice(platform):\n"
    '                logger.info("state.db warning to %s not sent: KAGE_SLACK_UX", platform.value)\n'
    "                continue\n"
    "            await self._send_home_channel_message(\n"
    '                platform, home, transport, message, "state.db warning notification failed for %s:%s: %s",\n'
)

RUN_BUSY = "gateway/run_busy.py"

BUSY_HINT_ANCHOR = "            if not is_seen(_load_gateway_config(), BUSY_INPUT_FLAG):\n"
BUSY_HINT_PATCHED = (
    "            # kube-agents patch: KAGE_SLACK_UX leaves the hint off Slack; see\n"
    "            # gateway/slack_boilerplate.py.\n"
    "            if not is_seen(_load_gateway_config(), BUSY_INPUT_FLAG) and not (\n"
    "                    _kage_slack_boilerplate.drop_notice(event.source.platform)):\n"
)

SLACK_ADAPTER = "plugins/platforms/slack/adapter.py"

SLACK_SEND_ANCHOR = (
    '        """Send a message to a Slack channel or DM."""\n'
    '        blocked = self._outbound_blocked(chat_id, "outbound generic send to")\n'
    "        if blocked:\n"
    "            return blocked\n"
    "        chat_id = await self._dm_target(chat_id, metadata)\n"
)
SLACK_SEND_PATCHED = SLACK_SEND_ANCHOR + (
    "        # kube-agents patch: KAGE_SLACK_UX rewords the gateway's system replies;\n"
    "        # see gateway/slack_boilerplate.py. Off, this is content.\n"
    "        content = _kage_slack_boilerplate.system_text(content)\n"
)

SLACK_EDIT_ANCHOR = (
    '        """Edit a previously sent Slack message."""\n'
    '        blocked = self._outbound_blocked(chat_id, "message edit in")\n'
    "        if blocked:\n"
    "            return blocked\n"
)
SLACK_EDIT_PATCHED = SLACK_EDIT_ANCHOR + (
    "        # kube-agents patch: KAGE_SLACK_UX rewords the gateway's system replies\n"
    "        # on the streaming/edit path too; see gateway/slack_boilerplate.py.\n"
    "        content = _kage_slack_boilerplate.system_text(content)\n"
)


def apply(root: Path) -> None:
    """Apply the patch under ``root``, or raise SystemExit with the reason."""
    delivery = patchlib.Patch(root, DELIVERY, prefix=PREFIX)
    delivery.refuse_if_patched(BUILD_MARKER)
    delivery.substitute(DELIVERY_IMPORT_ANCHOR, DELIVERY_IMPORT_PATCHED, label="_deliver_result import")
    delivery.substitute(DELIVERY_SEND_ANCHOR, DELIVERY_SEND_PATCHED, label="per-target send lanes")

    run_turn = patchlib.Patch(root, RUN_TURN, prefix=PREFIX)
    run_turn.refuse_if_patched(BUILD_MARKER)
    run_turn.substitute(HEARTBEAT_ANCHOR, HEARTBEAT_PATCHED, label="heartbeat display mode")
    run_turn.append(GATEWAY_IMPORT)

    run_notifications = patchlib.Patch(root, RUN_NOTIFICATIONS, prefix=PREFIX)
    run_notifications.refuse_if_patched(BUILD_MARKER)
    run_notifications.substitute(RESTARTED_ANCHOR, RESTARTED_PATCHED, label="post-restart notice")
    run_notifications.substitute(STARTUP_ANCHOR, STARTUP_PATCHED, label="home-channel startup loop")
    run_notifications.substitute(SESSION_DB_ANCHOR, SESSION_DB_PATCHED, label="state.db warning loop")
    run_notifications.append(GATEWAY_IMPORT)

    run_busy = patchlib.Patch(root, RUN_BUSY, prefix=PREFIX)
    run_busy.refuse_if_patched(BUILD_MARKER)
    run_busy.substitute(BUSY_HINT_ANCHOR, BUSY_HINT_PATCHED, label="busy-input onboarding hint")
    run_busy.append(GATEWAY_IMPORT)

    slack_adapter = patchlib.Patch(root, SLACK_ADAPTER, prefix=PREFIX)
    slack_adapter.refuse_if_patched(BUILD_MARKER)
    slack_adapter.substitute(SLACK_SEND_ANCHOR, SLACK_SEND_PATCHED, label="SlackAdapter.send")
    slack_adapter.substitute(SLACK_EDIT_ANCHOR, SLACK_EDIT_PATCHED, label="SlackAdapter.edit_message")
    slack_adapter.append(GATEWAY_IMPORT)

    delivery.commit("2 anchors")
    run_turn.commit("1 anchor, 1 import")
    run_notifications.commit("3 anchors, 1 import")
    run_busy.commit("1 anchor, 1 import")
    slack_adapter.commit("2 anchors, 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
