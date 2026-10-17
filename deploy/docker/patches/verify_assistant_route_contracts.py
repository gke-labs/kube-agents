#!/usr/bin/env python3
"""Build-time pin for the private upstream surface the assistant_route plugin reaches for.

agents/chat/defaults/plugins/assistant_route/plugin.py patches the Google Chat
thinking card through the adapter's private attributes, finds the gateway through
``gateway.run._gateway_runner_ref`` and its loop, widens its gated set from the
tool registry, and recognises the board's wake message by its opening text. None
of those is a published contract, and every failure mode is silent: the card
patch degrades to a debug log line, a reworded wake forces every blocked-card wake
into triage, a renamed registry call narrows the gate to its static pair. So the
image asserts each one here, next to the other message-surface verifiers, and a
Hermes bump that moves any of them fails the build instead.

Run from /opt/hermes after the patches are applied.
"""

from __future__ import annotations

import pathlib
import re
import sys

HERMES_ROOT = pathlib.Path("/opt/hermes")
ADAPTER_PATH = HERMES_ROOT / "plugins" / "platforms" / "google_chat" / "adapter.py"
GATEWAY_RUN_PATH = HERMES_ROOT / "gateway" / "run.py"
REGISTRY_PATH = HERMES_ROOT / "tools" / "registry.py"
LOCALE_PATH = HERMES_ROOT / "locales" / "en.yaml"
TYPING_CONSUMED_SENTINEL = '_TYPING_CONSUMED_SENTINEL = "<consumed>"'
ADAPTER_SYMBOLS = (
    "self._typing_messages",
    "async def _patch_message(self, message_name: str, body: Dict[str, Any])",
    TYPING_CONSUMED_SENTINEL,
)
GATEWAY_SYMBOLS = ("_gateway_runner_ref", "self._gateway_loop")
REGISTRY_SYMBOLS = ("def get_tool_names_for_toolset(",)
WAKE_PREFIX = "[kanban] Task "
# The wake text lives in the locale file (gateway.kanban.wake.message); the
# plugin keeps the same prefix in UNFORCED_MESSAGE_PREFIXES and tests the
# message's START, so the pin requires a `message:` value that opens with it,
# not the substring anywhere in the file.
WAKE_PATTERN = re.compile(r"^\s*message:\s*[\"']\[kanban\] Task \{task_id\}", re.MULTILINE)


def _require(path: pathlib.Path, needles: tuple[str, ...]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [needle for needle in needles if needle not in text]


def main() -> int:
    missing: list[str] = []
    for path, needles in (
        (ADAPTER_PATH, ADAPTER_SYMBOLS),
        (GATEWAY_RUN_PATH, GATEWAY_SYMBOLS),
        (REGISTRY_PATH, REGISTRY_SYMBOLS),
    ):
        if not path.is_file():
            missing.append(f"{path}: file missing")
            continue
        missing.extend(f"{path}: {needle!r}" for needle in _require(path, needles))
    if LOCALE_PATH.is_file():
        if not WAKE_PATTERN.search(LOCALE_PATH.read_text(encoding="utf-8")):
            missing.append(f"{LOCALE_PATH}: wake message no longer opens with {WAKE_PREFIX!r}")
    else:
        missing.append(f"{LOCALE_PATH}: file missing")
    if missing:
        print("assistant_route contracts moved upstream:", file=sys.stderr)
        for item in missing:
            print("  " + item, file=sys.stderr)
        return 1
    print("assistant_route contracts: adapter card patch, gateway runner ref, registry toolset lookup, wake prefix all present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
