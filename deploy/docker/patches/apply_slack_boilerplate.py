#!/usr/bin/env python3
"""Wire gateway/slack_boilerplate.py into the cron delivery, the heartbeat and the notices.

Run by ``deploy/docker/Dockerfile`` against the Hermes tree, after
``slack_boilerplate.py`` has been copied to ``gateway/``. Four files:

``cron/scheduler_delivery.py``: ``_deliver_result`` imports the module beside
its own ``BasePlatformAdapter`` import, and each target's two send lanes take
``cron_delivery_text(...)`` in place of the shared ``cleaned_delivery_content``.
Imported in the function, as that function already imports ``gateway``: a
module-level import would run while ``cron.scheduler`` is still loading.

``gateway/run_turn.py``: the heartbeat's display mode passes through
``long_running_mode`` straight after it is read, so the ``off`` check below it
sees the result.

``gateway/run_shutdown.py``: ``_send_notice_logged``, which sends both the
shutdown notice and the interrupted-cron-job notice, sends ``notice_text(...)``.

``gateway/run_notifications.py``: the post-restart notice and
``_send_home_channel_message`` (the home-channel startup notice) do the same.

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
    "        # kube-agents patch: KAGE_SLACK_UX gives Slack the generic heartbeat; see\n"
    "        # gateway/slack_boilerplate.py. Off, the mode is unchanged.\n"
    "        _long_running_mode = _kage_slack_boilerplate.long_running_mode(\n"
    "            turn_ctx.source, _long_running_mode)\n"
)

RUN_SHUTDOWN = "gateway/run_shutdown.py"

SHUTDOWN_SEND_ANCHOR = "            result = await adapter.send(chat_id, msg, **kw)\n"
SHUTDOWN_SEND_PATCHED = (
    "            # kube-agents patch: KAGE_SLACK_UX rewords the notice on Slack; see\n"
    "            # gateway/slack_boilerplate.py. Off, this is msg.\n"
    "            result = await adapter.send(\n"
    "                chat_id, _kage_slack_boilerplate.notice_text(platform_str, msg), **kw)\n"
)

RUN_NOTIFICATIONS = "gateway/run_notifications.py"

RESTARTED_ANCHOR = (
    '                platform, str(chat_id), "♻ Gateway restarted successfully. Your session continues.",\n'
)
RESTARTED_PATCHED = (
    "                # kube-agents patch: reworded on Slack under KAGE_SLACK_UX; see\n"
    "                # gateway/slack_boilerplate.py.\n"
    "                platform, str(chat_id), _kage_slack_boilerplate.notice_text(\n"
    '                    platform, "♻ Gateway restarted successfully. Your session continues."),\n'
)

HOME_CHANNEL_ANCHOR = (
    '        """Best-effort send to one home channel; True on success, failures logged with ``failure_fmt``."""\n'
    "        from gateway.run import _non_conversational_metadata\n"
)
HOME_CHANNEL_PATCHED = HOME_CHANNEL_ANCHOR + (
    "        # kube-agents patch: KAGE_SLACK_UX rewords the notice on Slack; see\n"
    "        # gateway/slack_boilerplate.py. Off, the message is unchanged.\n"
    "        message = _kage_slack_boilerplate.notice_text(platform, message)\n"
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

    run_shutdown = patchlib.Patch(root, RUN_SHUTDOWN, prefix=PREFIX)
    run_shutdown.refuse_if_patched(BUILD_MARKER)
    run_shutdown.substitute(SHUTDOWN_SEND_ANCHOR, SHUTDOWN_SEND_PATCHED, label="_send_notice_logged send")
    run_shutdown.append(GATEWAY_IMPORT)

    run_notifications = patchlib.Patch(root, RUN_NOTIFICATIONS, prefix=PREFIX)
    run_notifications.refuse_if_patched(BUILD_MARKER)
    run_notifications.substitute(RESTARTED_ANCHOR, RESTARTED_PATCHED, label="post-restart notice")
    run_notifications.substitute(HOME_CHANNEL_ANCHOR, HOME_CHANNEL_PATCHED, label="_send_home_channel_message")
    run_notifications.append(GATEWAY_IMPORT)

    delivery.commit("2 anchors")
    run_turn.commit("1 anchor, 1 import")
    run_shutdown.commit("1 anchor, 1 import")
    run_notifications.commit("2 anchors, 1 import")


if __name__ == "__main__":
    apply(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
