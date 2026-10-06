"""Build-time behaviour gate for the KAGE_SLACK_UX moments module.

Run by ``deploy/docker/Dockerfile`` against ``/opt/hermes`` once
``gateway/slack_ux_moments.py`` is installed, with ``slack_presenter.py`` and
``slack_moments.py`` staged beside this script (``/opt/defaults/scripts`` is
not populated yet at that point in the build).

Three things are checked:

1. ``gateway/kanban_progress_lines.py`` imports the module, since it is the
   only caller that posts a moment and an import that drifted would read as
   flag off.
2. ``gateway/kanban_watchers_notifier.py`` passes its wake text through
   ``wake_text`` (``apply_slack_ux_moments.py``).
3. The module, loaded by path and driven with a stub adapter: flag off it is
   inert; flag on, an opened PR posts once with its two url buttons, a PR
   that is only cited posts nothing, and a ``needs_input`` block posts its
   question with a choice per option it ends with while any other block posts
   nothing. The wake for that question's ``blocked`` event carries the note
   and a wake for another event does not; the question loses its buttons when
   settled; and a card with no thread gets the question without buttons.

A refused post raises nothing and logs a warning, so the build is where a
broken layout is caught.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

RUNTIME = "gateway/slack_ux_moments.py"
CALLER = "gateway/kanban_progress_lines.py"
CALLER_IMPORT = "from gateway import slack_ux_moments"
NOTIFIER = "gateway/kanban_watchers_notifier.py"
NOTIFIER_IMPORT = "from gateway.slack_ux_moments import wake_text as _kage_moments_wake_text"
FLAG_ENV = "KAGE_SLACK_UX"

CHANNEL = "C0KAGE"
THREAD = "1700000000.000100"
PR = "https://github.com/acme/fleet-config/pull/412"
OPENED = f"Opened PR {PR} raising the limit to 512Mi"
CITED = f"{PR} already covers this"
QUESTION = "Which checkout-gateway did you mean?\nTwo clusters run one. Which should I look at?\n- seeded-reliability\n- seeded-debug"
CHOICES = ["seeded-reliability", "seeded-debug"]
BLOCKED = "blocked"
BLOCKED_ID = 7
WAKE = "Task t_verify is blocked."


def _fail(detail: str) -> SystemExit:
    return SystemExit(f"slack_ux_moments verify: {detail}")


class _StubClient:
    def __init__(self, adapter: _StubAdapter) -> None:
        self.adapter = adapter

    async def chat_postMessage(self, **kwargs):
        self.adapter.posts.append(kwargs)
        return {"ts": "1700000000.000300"}

    async def chat_update(self, **kwargs):
        self.adapter.updates.append(kwargs)


class _StubAdapter:
    def __init__(self) -> None:
        self.posts: list = []
        self.updates: list = []

    def _get_client(self, chat_id, team_id=None):
        return _StubClient(self)


class _Event:
    def __init__(self, event_id: int, kind: str) -> None:
        self.id = event_id
        self.kind = kind


def _buttons(blocks: list) -> list:
    return [e for b in blocks if b.get("type") == "actions" for e in b["elements"]]


def check_caller(root: Path) -> None:
    path = root / CALLER
    if not path.is_file() or CALLER_IMPORT not in path.read_text():
        raise _fail(f"{CALLER} does not import slack_ux_moments")
    path = root / NOTIFIER
    if not path.is_file() or NOTIFIER_IMPORT not in path.read_text():
        raise _fail(f"{NOTIFIER} does not pass its wake text through slack_ux_moments")


def _load_runtime(root: Path):
    path = root / RUNTIME
    if not path.is_file():
        raise _fail(f"{path} does not exist")
    spec = importlib.util.spec_from_file_location("slack_ux_moments_verify", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module._presenter is None or module._moments is None:
        raise _fail("slack_presenter or slack_moments did not import beside the runtime module")
    return module


async def _drive(module) -> None:
    os.environ.pop(FLAG_ENV, None)
    if module.enabled():
        raise _fail(f"enabled() is true with {FLAG_ENV} unset")
    os.environ[FLAG_ENV] = "1"
    sub = {"platform": "slack", "chat_id": CHANNEL, "thread_id": THREAD, "task_id": "t_verify"}

    adapter = _StubAdapter()
    await module.pr_opened(adapter, sub, CITED)
    await module.pr_opened(adapter, sub, OPENED)
    await module.pr_opened(adapter, sub, OPENED)
    if len(adapter.posts) != 1:
        raise _fail(f"the PR moment posted {len(adapter.posts)} times, expected once")
    urls = [b.get("url") for b in _buttons(adapter.posts[0]["blocks"])]
    if urls != [PR, PR + "/files"] or adapter.posts[0]["thread_ts"] != THREAD:
        raise _fail(f"the PR moment was {adapter.posts[0]!r}")

    adapter = _StubAdapter()
    await module.needs_you(adapter, sub, {"kind": "capability", "reason": QUESTION})
    if module.wake_text(sub, [_Event(BLOCKED_ID, BLOCKED)], {BLOCKED}, WAKE) != WAKE:
        raise _fail("the wake changed before any question was posted")
    await module.needs_you(adapter, sub, {"kind": "needs_input", "reason": QUESTION}, BLOCKED_ID)
    if len(adapter.posts) != 1:
        raise _fail(f"the question posted {len(adapter.posts)} times, expected once")
    labels = [b["text"]["text"] for b in _buttons(adapter.posts[0]["blocks"])]
    if labels != CHOICES:
        raise _fail(f"the question's buttons were {labels!r}")
    if f"\n{module.QUESTION_CARD_NOTE.format(card=sub['task_id'])}\n" not in adapter.posts[0]["text"]:
        raise _fail("the question's text does not name its card")
    noted = module.wake_text(sub, [_Event(BLOCKED_ID, BLOCKED)], {BLOCKED}, WAKE)
    if noted != f"{WAKE}\n\n{module.WAKE_NOTE} {module.WAKE_NOTE_ANSWERED}":
        raise _fail("the wake for the posted question does not carry the note")
    if module.wake_text(sub, [_Event(BLOCKED_ID + 1, BLOCKED)], {BLOCKED}, WAKE) != WAKE:
        raise _fail("the wake for another blocked event carries the note")
    await module.settle_question(adapter, sub)
    if len(adapter.updates) != 1 or _buttons(adapter.updates[0]["blocks"]):
        raise _fail(f"settling the question sent {adapter.updates!r}")

    adapter = _StubAdapter()
    await module.needs_you(adapter, {**sub, "thread_id": ""}, {"kind": "needs_input", "reason": QUESTION})
    if len(adapter.posts) != 1 or _buttons(adapter.posts[0]["blocks"]):
        raise _fail("a question with no thread to answer in got buttons")
    os.environ.pop(FLAG_ENV, None)


def main(root: Path = Path("/opt/hermes")) -> None:
    check_caller(root)
    asyncio.run(_drive(_load_runtime(root)))
    print(
        "slack_ux_moments verify: reached from kanban_progress_lines and the wake; "
        "posts an opened PR once and a needs_input question with its choices and card, "
        "notes the question in its wake and settles it"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/hermes"))
