"""Reader for the shared kube-agents.chat/v1 cases in ``tests/conformance/chat/``.

Each case is one JSON file: an ``input`` and the ``expected`` chat messages the
next-mode emitter owes for it, one per output in order. ``source`` names the
today test the case comes from: the test its input was lifted from, or, where
the input was built to show a rule (a window past its cap, say), the test that
pins that rule; ``notes`` says where the two differ. ``a2a/lib/chatmsg`` checks every expected
message against the full validator; this reader checks the files' shape so the
Python presenters can be run against them (``spec-a2a-payloads.md``, "Chat
messages"). A case with ``input.events`` has one ``expected`` entry per event,
``null`` where the event emits no chat message.

The text fallback is not pinned: today's is Slack mrkdwn, escaped for Slack, and
under next the gateway escapes it, so the emitter decides it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: The repository root, from this file's directory.
REPO_ROOT = Path(__file__).resolve().parents[3]
CASES_DIR = REPO_ROOT / "tests" / "conformance" / "chat"
CASE_GLOB = "*.json"

SCHEMA = "kube-agents.chat/v1"
#: The carriers a chat message may ride (the spec's "Where one may appear").
CARRIERS = frozenset({"result", "turn", "progress", "notice", "input-required", "terminal", "notify"})
#: Only a conversation notify has an ``update`` key.
UPDATE_CARRIER = "notify"
#: ``source`` is ``<repo path>::<fixture>``.
SOURCE_SEPARATOR = "::"

CASE_REQUIRED = {"name": str, "source": str, "input": dict, "expected": list}
CASE_OPTIONAL = {"notes": str}
OUTPUT_REQUIRED = {"carrier": str, "chat": dict}
OUTPUT_OPTIONAL = {"update": str}

#: The JSON type of every chat message field and sub-field a case may use. A
#: renderer ignores unknown fields so v1 can grow; a case refuses them, so a typo
#: cannot pass as a field the emitter is not asked for.
CHAT_FIELDS = {
    "schema": str, "headline": str, "note": str, "detail": str, "tone": str, "rows": list,
    "after": list, "fold": dict, "choices": list, "links": list, "plan": dict, "mark": dict,
}
ROW_FIELDS = {"text": str, "severity": str, "detail": str}
FOLD_FIELDS = {"title": str, "text": str, "table": dict}
TABLE_FIELDS = {"columns": list, "rows": list}
CHOICE_FIELDS = {"text": str, "reply": str, "primary": bool}
LINK_FIELDS = {"text": str, "url": str, "primary": bool}
PLAN_FIELDS = {"title": str, "items": list}
ITEM_FIELDS = {
    "id": str, "title": str, "status": str, "steps": list, "stepCount": int, "note": str, "result": str,
}
MARK_FIELDS = {"ask": str, "settle": str}


class CaseError(ValueError):
    """A case file that does not have the shape this reader documents."""


def _fields(value: Any, fields: dict, where: str, required: tuple[str, ...] = ()) -> None:
    if not isinstance(value, dict):
        raise CaseError(f"{where}: not an object")
    unknown = sorted(set(value) - set(fields))
    if unknown:
        raise CaseError(f"{where}: unknown fields {unknown}")
    for key in required:
        if key not in value:
            raise CaseError(f"{where}: missing {key}")
    for key, kind in fields.items():
        # bool is an int to Python; a stepCount of true is not a count.
        if key in value and (not isinstance(value[key], kind) or (kind is int and isinstance(value[key], bool))):
            raise CaseError(f"{where}.{key}: not a {kind.__name__}")


def _each(values: list, fields: dict, where: str, required: tuple[str, ...] = ()) -> None:
    for i, value in enumerate(values):
        _fields(value, fields, f"{where}[{i}]", required)


def _strings(values: list, where: str) -> None:
    for i, value in enumerate(values):
        if not isinstance(value, str):
            raise CaseError(f"{where}[{i}]: not a string")


def check_chat(chat: Any, where: str) -> None:
    """Raise :class:`CaseError` unless ``chat`` is a v1 object of known, well-typed fields."""
    _fields(chat, CHAT_FIELDS, where, ("schema",))
    if chat["schema"] != SCHEMA:
        raise CaseError(f"{where}.schema: {chat['schema']!r} is not {SCHEMA!r}")
    _each(chat.get("rows", []), ROW_FIELDS, f"{where}.rows", ("text",))
    _strings(chat.get("after", []), f"{where}.after")
    if "fold" in chat:
        _fields(chat["fold"], FOLD_FIELDS, f"{where}.fold")
        if "table" in chat["fold"]:
            _fields(chat["fold"]["table"], TABLE_FIELDS, f"{where}.fold.table", ("columns",))
    _each(chat.get("choices", []), CHOICE_FIELDS, f"{where}.choices", ("text",))
    _each(chat.get("links", []), LINK_FIELDS, f"{where}.links", ("text", "url"))
    if "plan" in chat:
        _fields(chat["plan"], PLAN_FIELDS, f"{where}.plan")
        _each(chat["plan"].get("items", []), ITEM_FIELDS, f"{where}.plan.items", ("id", "status"))
        for i, item in enumerate(chat["plan"].get("items", [])):
            _strings(item.get("steps", []), f"{where}.plan.items[{i}].steps")
    if "mark" in chat:
        _fields(chat["mark"], MARK_FIELDS, f"{where}.mark")


def check_case(case: Any, stem: str, root: Path = REPO_ROOT) -> None:
    """Raise :class:`CaseError` unless ``case``, read from ``<stem>.json``, has the documented shape."""
    _fields(case, {**CASE_REQUIRED, **CASE_OPTIONAL}, stem, tuple(CASE_REQUIRED))
    if case["name"] != stem:
        raise CaseError(f"{stem}: name {case['name']!r} is not the file's name")
    path, separator, fixture = case["source"].partition(SOURCE_SEPARATOR)
    if not separator or not fixture:
        raise CaseError(f"{stem}.source: {case['source']!r} is not <path>{SOURCE_SEPARATOR}<fixture>")
    if not (root / path).is_file():
        raise CaseError(f"{stem}.source: {path} does not exist")
    expected = case["expected"]
    events = case["input"].get("events")
    if events is not None and len(events) != len(expected):
        raise CaseError(f"{stem}: {len(events)} events but {len(expected)} expected entries")
    if events is None and len(expected) != 1:
        raise CaseError(f"{stem}: a case without events expects exactly one output")
    if all(out is None for out in expected):
        raise CaseError(f"{stem}: expects no chat message at all")
    for i, out in enumerate(expected):
        if out is None:
            continue
        where = f"{stem}.expected[{i}]"
        _fields(out, {**OUTPUT_REQUIRED, **OUTPUT_OPTIONAL}, where, tuple(OUTPUT_REQUIRED))
        if out["carrier"] not in CARRIERS:
            raise CaseError(f"{where}.carrier: {out['carrier']!r} is not a carrier")
        if "update" in out and out["carrier"] != UPDATE_CARRIER:
            raise CaseError(f"{where}.update: only a {UPDATE_CARRIER} has an update key")
        check_chat(out["chat"], f"{where}.chat")


def load(cases_dir: Path = CASES_DIR) -> list[dict]:
    """Every case under ``cases_dir``, checked, sorted by name. Raises on an empty directory."""
    # By stem, not path: "pr-opened.json" sorts after "pr-opened-twice.json", "pr-opened" before it.
    paths = sorted(cases_dir.glob(CASE_GLOB), key=lambda path: path.stem)
    if not paths:
        raise CaseError(f"no cases under {cases_dir}")
    cases = []
    for path in paths:
        case = json.loads(path.read_text(encoding="utf-8"))
        check_case(case, path.stem)
        cases.append(case)
    return cases
