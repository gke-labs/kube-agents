"""Record each pull request a kanban worker opens as a ``pr_opened`` event on its card.

A worker opens a pull request or merge request only through the credential
broker's ``proposal-create`` verb: the sandbox's ``/v1/exec`` runs neither
``git`` nor a forge CLI, and every caller (``vcs.py``, submit-suggestion, the
fleet audit) reaches the broker through ``vcs_client.call``. When the broker
answers with a new proposal, ``call`` writes one JSON line to stderr naming it
(``vcs_client.PROPOSAL_OPENED_KEY``). That line comes back to the worker as part
of its terminal output, in the process whose ``HERMES_KANBAN_TASK`` names the
card it is working.

This hook sees that output before the model does. It takes each such line out,
so the model's view of the command is what it was before the record existed,
and appends a ``pr_opened`` event (the forge's ``url``, ``number`` and
``title``) to the card. The gateway's notifier claims the kind and, with
``KAGE_SLACK_UX`` on, posts the PR as its own message in the thread the card
reports to (``gateway/kanban_progress_lines.py``, ``gateway/slack_ux_moments.py``),
whatever the worker goes on to say about it.

Outside a kanban worker there is no card, so the line is taken out and nothing
is recorded. A record that cannot be written is logged and dropped: the PR is
open either way, and the thread falls back to reading the worker's own note.
Background processes are not covered, since this hook runs on foreground
output only.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

#: ``vcs_client.PROPOSAL_OPENED_KEY`` / ``_EVENT`` / ``_FIELDS``, held here because a
#: plugin is loaded by file path and the sandbox's client is not on its import
#: path. ``test_pr_opened.py`` fails when the two drift.
PROPOSAL_OPENED_KEY = "kube-agents.event"
PROPOSAL_OPENED_EVENT = "proposal-opened"
PROPOSAL_OPENED_FIELDS = ("url", "number", "title")

#: ``kanban_progress_lines.PR_OPENED_KIND``: the event kind the notifier claims for it.
PR_OPENED_KIND = "pr_opened"

#: The card a dispatcher-spawned worker is running, as the dispatcher sets it.
TASK_ENV = "HERMES_KANBAN_TASK"

#: The longest title kept on the event; the thread shows it clipped shorter.
TITLE_MAX = 300

#: What a record line starts with once stripped.
JSON_OBJECT_START = "{"


def register(ctx):
    ctx.register_hook("transform_terminal_output", on_terminal_output)


def opened(line):
    """The ``{url, number, title}`` a proposal-opened record on ``line`` names, else None."""
    text = line.strip()
    if not text.startswith(JSON_OBJECT_START) or PROPOSAL_OPENED_KEY not in text:
        return None
    try:
        record = json.loads(text)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get(PROPOSAL_OPENED_KEY) != PROPOSAL_OPENED_EVENT:
        return None
    proposal = record.get("proposal")
    if not isinstance(proposal, dict) or not isinstance(proposal.get("url"), str) or not proposal["url"]:
        return None
    found = {field: proposal.get(field) for field in PROPOSAL_OPENED_FIELDS}
    found["title"] = str(found.get("title") or "")[:TITLE_MAX]
    return found


def record(task_id, proposal):
    """Append a ``pr_opened`` event for ``proposal`` to card ``task_id``, in its current run."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn, kb.write_txn(conn):
        kb._append_event(conn, task_id, PR_OPENED_KIND, proposal, run_id=kb._current_run_id(conn, task_id))


def on_terminal_output(command="", output="", **_):
    """Take each proposal-opened record out of ``output`` and record it on the worker's card.

    Returns the output without those lines, or None (unchanged) when it holds none.
    """
    if not isinstance(output, str) or PROPOSAL_OPENED_KEY not in output:
        return None
    kept, found = [], []
    for line in output.splitlines(keepends=True):
        proposal = opened(line)
        if proposal is None:
            kept.append(line)
        else:
            found.append(proposal)
    if not found:
        return None
    task_id = os.environ.get(TASK_ENV, "").strip()
    for proposal in found if task_id else ():
        try:
            record(task_id, proposal)
        except Exception as exc:  # noqa: BLE001 — the PR is open; the thread falls back to the note
            logger.warning("pr_opened: recording %s on %s failed: %s", proposal.get("url"), task_id, exc)
    return "".join(kept)
