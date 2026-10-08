#!/usr/bin/env python3
"""Verify the kanban_chat_notify patch against the patched Hermes tree.

Run from ``/opt/hermes`` by ``deploy/docker/Dockerfile`` after
``apply_kanban_chat_notify.py``. It imports the real patched notifier and
exercises what the patch adds, so a base-image bump that changes what
``_Collector``, ``_adapter_for_subscription`` or the wake path look like fails
the build here rather than on a live install. A fake ``a2a`` on PATH stands in
for the CLI from the first routed resolve on.

1. Resolution: with ``A2A_NOTIFY_PLATFORM`` set and no Google Chat adapter
   connected, a collector counts ``google_chat`` as served and
   ``_adapter_for_subscription`` returns the stand-in, reused across
   deliveries and never registered in ``runner.adapters``; with it unset,
   neither happens. A threadless subscription and ``multiplex_profiles`` get
   none, and the collector's claim is wrapped by the stale filter.
2. Send: ``a2a notify`` with the thread and a ``--`` before the text; exit 0
   is sent, exit 1 failed, exit 3 (outcome unknown) not a failure, and exit 4
   a failure that holds the route down.
3. The route probe: an empty notify; exit 4 reads as down before any send,
   an armed gateway's refusal as up.
4. Attachments are not posted.
5. A failure wake is admitted, reaches the runner's handler, and the turn's
   reply goes out through ``a2a notify``.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.getcwd())

from gateway import kanban_watchers_notifier as notifier  # noqa: E402
from gateway.config import Platform  # noqa: E402
from gateway.kanban_chat_notify import NOTIFY_PLATFORM_ENV, ChatNotifyAdapter  # noqa: E402

GCHAT = None

FAKE_A2A = """#!/bin/sh
printf '%s\\n' "$@" > "$A2A_ARGV_LOG"
exit "${A2A_EXIT:-0}"
"""


class _Runner:
    """Just enough of GatewayRunner for the collector and the resolver."""

    def __init__(self) -> None:
        self.adapters = {Platform.API_SERVER: object()}
        self._profile_adapters = {}
        self.config = SimpleNamespace(multiplex_profiles=False, profile_routes=[])
        self.handled = []

    def _authorization_adapter(self, platform, owner_profile):
        return self.adapters.get(platform)

    def _owns_kanban_dispatcher_lock(self):
        return True

    def _active_profile_name(self):
        return "default"

    def _primary_message_handler(self):
        runner = self

        async def handler(event):
            runner.handled.append(event)
            return "the wake turn's reply"
        return handler


def check(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}")
        sys.exit(1)
    print(f"  ok   {message}")


def main() -> None:
    # Google Chat is a plugin platform: the enum has no member for it, and the
    # notifier builds it from the subscription's string the same way.
    global GCHAT
    GCHAT = Platform("google_chat")
    sub = {"task_id": "t_1", "platform": "google_chat", "chat_id": "spaces/H", "thread_id": "spaces/H/threads/T"}

    os.environ.pop(NOTIFY_PLATFORM_ENV, None)
    runner = _Runner()
    collector = notifier._Collector(runner, kb=None, notifier_profile=None, gc_due=False, gc_retention_days=30)
    check("google_chat" not in collector.active_platforms, "unrouted: google_chat is not served")
    check(notifier._adapter_for_subscription(runner, GCHAT, sub, None) is None,
          "unrouted: no adapter for google_chat")

    # From here on the CLI is a fake on PATH: the first routed resolve probes
    # the route, and an armed gateway's refusal of the empty probe (exit 1)
    # reads as up. HERMES_HOME holds the stale filter's rollout record.
    tmp_dir = tempfile.TemporaryDirectory()
    tmp = tmp_dir.name
    fake = Path(tmp) / "a2a"
    fake.write_text(FAKE_A2A)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    log = Path(tmp) / "argv"
    os.environ["PATH"] = f"{tmp}:{os.environ['PATH']}"
    os.environ["A2A_ARGV_LOG"] = str(log)
    os.environ["A2A_EXIT"] = "1"
    os.environ["HERMES_HOME"] = tmp

    os.environ[NOTIFY_PLATFORM_ENV] = "google_chat"
    collector = notifier._Collector(runner, kb=None, notifier_profile=None, gc_due=False, gc_retention_days=30)
    check("google_chat" in collector.active_platforms, "routed: google_chat is served")
    adapter = notifier._adapter_for_subscription(runner, GCHAT, sub, None)
    check(isinstance(adapter, ChatNotifyAdapter), "routed: the stand-in answers for google_chat")
    check(notifier._adapter_for_subscription(runner, GCHAT, sub, None) is adapter,
          "routed: the stand-in is reused across deliveries")
    check(notifier._adapter_for_subscription(runner, Platform.API_SERVER, sub, None) is runner.adapters[Platform.API_SERVER],
          "routed: a connected platform still resolves to its own adapter")
    check(adapter._message_handler is not None, "the stand-in carries the runner's message handler for wakes")
    check(GCHAT not in runner.adapters, "the stand-in is not registered in runner.adapters")
    threadless = dict(sub, thread_id="")
    check(notifier._adapter_for_subscription(runner, GCHAT, threadless, None) is None,
          "routed: a subscription with no thread gets no stand-in (it would land in the home channel)")
    runner.config.multiplex_profiles = True
    check(notifier._adapter_for_subscription(runner, GCHAT, sub, None) is None,
          "routed: no stand-in under multiplex_profiles")
    runner.config.multiplex_profiles = False
    check(notifier._Collector._claim_for_sub.__name__ == "_kage_claim_for_sub",
          "the collector's claim is wrapped to drop stale events")

    with tmp_dir:
        os.environ["A2A_EXIT"] = "0"
        result = asyncio.run(adapter.send("spaces/H", "- done", metadata={"thread_id": "spaces/H/threads/T"}))
        argv = log.read_text().splitlines()
        check(result.success, "send: exit 0 is a successful send")
        check(argv == ["notify", "--platform", "google_chat", "--timeout", "60s", "--thread", "spaces/H/threads/T", "--", "- done"],
              f"send: argv carries the thread and ends the flags before the text ({argv})")

        os.environ["A2A_EXIT"] = "1"
        check(not asyncio.run(adapter.send("spaces/H", "x")).success, "send: exit 1 is a failed send")
        os.environ["A2A_EXIT"] = "3"
        check(asyncio.run(adapter.send("spaces/H", "x")).success,
              "send: exit 3 (outcome unknown) is not reported as a failure, so it is not re-sent")
        os.environ["A2A_EXIT"] = "4"
        check(not asyncio.run(adapter.send("spaces/H", "x")).success, "send: exit 4 (route unavailable) is a failed send")
        check(notifier._adapter_for_subscription(runner, GCHAT, sub, None) is None,
              "route down: no adapter, so the notifier skips without spending the failure budget")
        # The probe: an empty notify, run from the collector's thread (not the
        # event loop). An armed gateway refuses it ("text is empty", exit 1),
        # which reads as up; no responders (exit 4) reads as down.
        adapter._route_down_until = 0.0
        adapter._probed_at = float("-inf")
        log.unlink(missing_ok=True)
        os.environ["A2A_EXIT"] = "4"
        check(notifier._adapter_for_subscription(runner, GCHAT, sub, None) is None,
              "probe: a route answering no responders is down before any send, so nothing is claimed or counted")
        probe = log.read_text().splitlines() if log.exists() else []
        check(probe == ["notify", "--platform", "google_chat", "--timeout", "5s", "--", ""],
              f"probe: it is an empty notify ({probe})")
        adapter._route_down_until = 0.0
        adapter._probed_at = float("-inf")
        os.environ["A2A_EXIT"] = "1"
        check(notifier._adapter_for_subscription(runner, GCHAT, sub, None) is adapter,
              "route back: a probe the gateway answers (a refusal) reads as up, and the stand-in answers again")
        log.unlink(missing_ok=True)
        os.environ["A2A_EXIT"] = "0"
        media = asyncio.run(adapter.send_document("spaces/H", "/opt/data/report.pdf", metadata={"thread_id": "spaces/H/threads/T"}))
        check(not media.success and not log.exists(), "attachments are not posted (no notice per file)")

        # The failure wake: the notifier admits a synthetic internal event on
        # the adapter. It must be accepted (not WakeNotAccepted), reach the
        # runner's handler, and the turn's reply must go out through send().
        from gateway.platforms.base import MessageEvent, MessageType
        from gateway.session import SessionSource
        from gateway.wake import admit_internal_event

        os.environ["A2A_EXIT"] = "0"
        log.unlink(missing_ok=True)
        source = SessionSource(platform=GCHAT, chat_id="spaces/H", chat_type="group", thread_id="spaces/H/threads/T")
        event = MessageEvent(text="[wake] card t_1 gave up", message_type=MessageType.TEXT, source=source, internal=True)

        async def wake_and_settle():
            await admit_internal_event(adapter, event)
            for _ in range(50):
                if log.exists():
                    return
                await asyncio.sleep(0.1)

        asyncio.run(wake_and_settle())
        check(len(runner.handled) == 1, "wake: the internal event reached the runner's handler")
        reply = log.read_text().splitlines() if log.exists() else []
        check(reply[:3] == ["notify", "--platform", "google_chat"] and reply[-1] == "the wake turn's reply",
              f"wake: the turn's reply went out through a2a notify ({reply})")
    print("kanban_chat_notify: verified")


if __name__ == "__main__":
    main()
