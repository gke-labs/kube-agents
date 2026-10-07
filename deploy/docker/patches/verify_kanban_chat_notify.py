#!/usr/bin/env python3
"""Verify the kanban_chat_notify patch against the patched Hermes tree.

Run from ``/opt/hermes`` by ``deploy/docker/Dockerfile`` after
``apply_kanban_chat_notify.py``. It imports the real patched notifier and
exercises the two seams the patch adds, so a base-image bump that changes what
``_Collector`` or ``_adapter_for_subscription`` look like fails the build here
rather than on a live install:

1. With ``A2A_NOTIFY_PLATFORM`` set and no Google Chat adapter connected, a
   collector counts ``google_chat`` as served, and ``_adapter_for_subscription``
   returns the stand-in; with it unset, neither happens.
2. The stand-in's ``send`` runs ``a2a notify`` with the thread and a ``--``
   before the text, and reports the answer as a successful ``SendResult``; a
   non-zero exit is a failed send, and exit 3 (outcome unknown) is not.
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

    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "a2a"
        fake.write_text(FAKE_A2A)
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        log = Path(tmp) / "argv"
        os.environ["PATH"] = f"{tmp}:{os.environ['PATH']}"
        os.environ["A2A_ARGV_LOG"] = str(log)

        os.environ["A2A_EXIT"] = "0"
        result = asyncio.run(adapter.send("spaces/H", "- done", metadata={"thread_id": "spaces/H/threads/T"}))
        argv = log.read_text().splitlines()
        check(result.success, "send: exit 0 is a successful send")
        check(argv == ["notify", "--platform", "google_chat", "--thread", "spaces/H/threads/T", "--", "- done"],
              f"send: argv carries the thread and ends the flags before the text ({argv})")

        os.environ["A2A_EXIT"] = "1"
        check(not asyncio.run(adapter.send("spaces/H", "x")).success, "send: exit 1 is a failed send")
        os.environ["A2A_EXIT"] = "3"
        check(asyncio.run(adapter.send("spaces/H", "x")).success,
              "send: exit 3 (outcome unknown) is not reported as a failure, so it is not re-sent")

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
