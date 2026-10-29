"""The writes a kanban card made, kept where the card stop can read them.

A stop in a gateway conversation archives the cards its turns filed
(session-kv's card-stop route). What it can say back is what it stopped; a
pull request a card opened before the stop has landed already, and the
person who pressed Stop is owed that. So the ``tool_call_audit`` plugin,
which every profile runs, hands each finished tool call of a kanban worker
here, and a call that shows a write appends one JSON line to the card's own
ledger file: ``<board dir>/kanban-writes/<card id>.jsonl``, the board dir
being the directory of ``HERMES_KANBAN_DB``, which the dispatcher pins on
every worker it spawns. The card-stop route reads the file of every card in
the conversation (``CARD_WRITES_DIR_NAME`` in ``session_kv_server.py`` is the
same name, and a test there holds the two together).

What counts as a write, today: a pull request the ``submit-suggestion``
skill opened or updated, read off its own success line in a terminal call's
output. That is every pull request the Planning Agent's change path opens.
Nothing else is recorded yet (a fleet-audit remediation, a ledger issue, a
cluster write with read-only enforcement off, a ``gke`` MCP call), so the
ledger names writes it saw and never vouches that there were none.

Only a worker's own process writes: ``HERMES_KANBAN_TASK`` names the card, and
a call outside a worker (the front door, a cron run) records nothing. Every
failure is swallowed after one log line: the ledger must never be what fails
a tool call.
"""

import json
import logging
import os
import re
import time
from typing import Any

KANBAN_TASK_ENV = "HERMES_KANBAN_TASK"
KANBAN_DB_ENV = "HERMES_KANBAN_DB"
CARD_WRITES_DIR_NAME = "kanban-writes"
CARD_WRITES_SUFFIX = ".jsonl"
# The kanban card id grammar the file name may carry; anything else is not
# written, so a card id can never name a path outside the directory.
CARD_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")
# The terminal tools whose output a write is read from.
TERMINAL_TOOLS = frozenset({"terminal"})
# submit_suggestion.py's success line (scripts/submit_suggestion.py, after
# `_land_proposal`), and the progress lines it logs before it: one when it
# creates the proposal, the others when it updates an open one (or finds one
# a sibling run opened a moment earlier, which it then updates).
SUBMITTED_RE = re.compile(r"PR SUBMITTED SUCCESSFULLY!.*?URL: (\S+)")
OPENING_MARKER = "Opening a proposal for"
UPDATING_MARKERS = ("updating it in place", "updating it.", "Leaving the description of")
# A pull request URL on github.com, owner and repository in GitHub's own
# grammar. A URL outside it is not recorded: the reply links what it names.
PR_URL_RE = re.compile(r"\Ahttps://github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})/pull/([0-9]{1,9})\Z")
KIND_PULL_REQUEST = "pull_request"
VERB_OPENED = "opened"
VERB_UPDATED = "updated"
# The most entries one card's file holds; later writes are not recorded (a
# reader names the first ones only anyway).
MAX_ENTRIES_PER_CARD = 50
LEDGER_FILE_MODE = 0o640
LEDGER_DIR_MODE = 0o750

logger = logging.getLogger("hermes.plugin.write_ledger")


def _output_of(result: Any) -> str | None:
    """The terminal result's output and stderr merged, as Hermes returns it, or None."""
    parsed = result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except ValueError:
            return None
    if not isinstance(parsed, dict):
        return None
    output = parsed.get("output")
    return output if isinstance(output, str) else None


def writes_in(tool_name: str, result: Any) -> list[dict[str, Any]]:
    """The writes one finished tool call shows, oldest first; [] for none."""
    if tool_name not in TERMINAL_TOOLS:
        return []
    output = _output_of(result)
    if not output:
        return []
    found: list[dict[str, Any]] = []
    for match in SUBMITTED_RE.finditer(output):
        url = match.group(1)
        parsed = PR_URL_RE.match(url)
        if parsed is None:
            continue
        # The progress line nearest before the success line says which it was.
        before = output[: match.start()]
        updated = max(before.rfind(m) for m in UPDATING_MARKERS) > before.rfind(OPENING_MARKER)
        found.append({
            "kind": KIND_PULL_REQUEST,
            "verb": VERB_UPDATED if updated else VERB_OPENED,
            "url": url,
            "repo": f"{parsed.group(1)}/{parsed.group(2)}",
            "number": int(parsed.group(3)),
        })
    return found


def ledger_path(board_db: str, card_id: str) -> str | None:
    """The card's ledger file beside the board, or None for an id outside the grammar."""
    if not board_db or not CARD_ID_RE.match(card_id):
        return None
    return os.path.join(os.path.dirname(board_db), CARD_WRITES_DIR_NAME, card_id + CARD_WRITES_SUFFIX)


def record(tool_name: str, result: Any) -> None:
    """Append the writes this call shows to the worker's card ledger, if any."""
    try:
        card_id = os.environ.get(KANBAN_TASK_ENV, "").strip()
        if not card_id:
            return
        writes = writes_in(tool_name, result)
        if not writes:
            return
        path = ledger_path(os.environ.get(KANBAN_DB_ENV, "").strip(), card_id)
        if path is None:
            return
        os.makedirs(os.path.dirname(path), mode=LEDGER_DIR_MODE, exist_ok=True)
        try:
            with open(path, encoding="utf-8") as existing:
                held = sum(1 for _ in existing)
        except FileNotFoundError:
            held = 0
        lines = []
        now = time.time()
        for write in writes:
            if held >= MAX_ENTRIES_PER_CARD:
                break
            lines.append(json.dumps({**write, "card": card_id, "at": now}, sort_keys=True) + "\n")
            held += 1
        if not lines:
            return
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, LEDGER_FILE_MODE)
        with os.fdopen(fd, "a", encoding="utf-8") as out:
            out.write("".join(lines))
    except Exception as exc:  # noqa: BLE001 - the ledger never fails a tool call
        logger.error("write ledger: could not record a write: %s", exc)
