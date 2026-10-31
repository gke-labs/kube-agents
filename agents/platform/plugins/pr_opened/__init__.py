"""Record each pull request a kanban worker opens as a ``pr_opened`` event on its card.

A worker opens a pull request or merge request only through the credential
broker's ``proposal-create`` verb: the sandbox's ``/v1/exec`` runs neither
``git`` nor a forge CLI, and every caller (``vcs.py``, submit-suggestion, the
fleet audit) reaches the broker through ``vcs_client.call``. When the broker
answers with a new proposal, ``call`` writes one JSON line to stderr naming it
(``vcs_client.PROPOSAL_OPENED_KEY``). That line comes back to the worker as part
of its terminal output, in the process whose ``HERMES_KANBAN_TASK`` names the
card it is working.

This hook sees that output before the model does. A line that has the record's
shape is not proof by itself: terminal output also carries text nobody here
wrote (a commit message, a file, a pod's log), and such text could hold a line
naming someone else's pull request on the install's own repository, or one the
agent opened weeks ago. So each record is checked with the forge first
(:meth:`Checker.ours`): the broker must serve the pull request at that url on a
repository this install manages, open, created within
:data:`CREATED_WITHIN_SECONDS`, and authored by the account the broker's
credential authenticates as, the identity every pull request the agent opens
carries. Only then is a ``pr_opened`` event, carrying the forge's own ``url``,
``number`` and ``title`` and never the line's, appended to the card and the
line taken out of the output. At most :data:`RECORDS_CHECKED_MAX` distinct urls
in one output are checked, since each check costs broker reads and the output
can be text nobody here wrote. So the model's view of the
command is what it was before the record existed. A line that fails the check
is left where it was, for the model to read as the text it is. The gateway's
notifier claims the kind and, with ``KAGE_SLACK_UX`` on, posts the PR as its
own message in the thread the card reports to
(``gateway/kanban_progress_lines.py``, ``gateway/slack_ux_moments.py``),
whatever the worker goes on to say about it.

The check is two broker reads through the sandbox (``forge.call``), made in the
terminal call that opened the pull request, so that call returns a few seconds
later; it happens once per pull request. It is made in line rather than on a
thread because a worker leaves through ``os._exit``, which would drop a thread
still waiting on the broker.

Outside a kanban worker there is no card, so nothing is checked, recorded or
taken out. A check or a record that fails is logged and the line left in: the
PR is open either way, and the thread falls back to reading the worker's own
note. Background processes are not covered, since this hook runs on
foreground output only.

The plugin also refuses ``submit_suggestion.py prepare`` and ``submit`` from a
kanban worker whose own run on its card has ended (:func:`on_pre_tool_call`).
On a presubmit eval in October 2026 (issue #2610) a platform worker asked to
propose a PodDisruptionBudget called ``kanban_complete`` with the manifest in
its result and a closing ``[next: Submit a pull request with this manifest]``
line, then, still in the same session, acted on that line and opened the pull
request three minutes after its card was done. Nobody had asked for one.

The check has to sit here, in the worker process. The script runs in the shell
sandbox, which learns the card id from the working directory
(``deploy/sandbox/session-command.sh``) but never the run id, and neither the
sandbox nor the credential broker can read the board. The worker process has
both: ``HERMES_KANBAN_TASK`` and ``HERMES_KANBAN_RUN_ID`` from the dispatcher,
and the board on its own volume. A worker whose card is no longer ``running``
under its run is refused before the command crosses to the sandbox. Everything
else is left alone: a process that is not a dispatcher worker (the gateway's
own sessions, a cron run), a worker whose run is still live, and any case where
the board cannot be read, which keeps today's behaviour rather than taking the
write path from a live card. Same rule as
``deploy/docker/patches/kanban_ended_run.py``, which refuses a finished run's
``kanban_complete`` and ``kanban_block``.
"""

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone

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

#: The dispatcher's run id for that card, as it sets it beside ``TASK_ENV``.
RUN_ID_ENV = "HERMES_KANBAN_RUN_ID"

#: The tool whose commands reach the shell sandbox.
TERMINAL_TOOL = "terminal"

#: ``submit_suggestion.py`` and the first argument after it, if any. The script
#: reads any first argument that isn't a command as ``submit`` (its
#: ``normalise_argv``: the old bare ``--branch/--title/--body`` shape), so every
#: invocation runs ``prepare`` or ``submit`` except a help request and a retired
#: command, which errors on its own.
SUBMIT_SUGGESTION_ARG = re.compile(r"submit_suggestion\.py[\"']?(?:\s+([^\s;|&]+))?")

#: First arguments that don't run ``prepare`` or ``submit``
#: (``submit_suggestion.RETIRED_COMMANDS`` and the help flags).
NOT_A_RUN = frozenset({"-h", "--help", "list", "fetch"})


def runs_submit_suggestion(command):
    """True when ``command`` runs the submit-suggestion skill's ``prepare`` or ``submit``, in any form."""
    for match in SUBMIT_SUGGESTION_ARG.finditer(command):
        first = (match.group(1) or "").strip("\"'")
        if first and first not in NOT_A_RUN:
            return True
    return False

#: The card status a live run holds.
RUNNING_STATUS = "running"

#: The card's status and current run, as ``kanban_ended_run.ended_run_refusal`` reads them.
CARD_RUN_SQL = "SELECT status, current_run_id FROM tasks WHERE id = ?"

#: The longest title kept on the event; the thread shows it clipped shorter.
TITLE_MAX = 300

#: What a record line starts with once stripped.
JSON_OBJECT_START = "{"

#: Where the agent pod's shared scripts (``forge.py`` and what it imports) are
#: staged, as ``vcs_client`` finds its siblings there.
SCRIPTS_DIRS = ("/opt/defaults/scripts", "/opt/data/scripts")

#: A pull request url on any forge (``…/pull/<n>``) or a merge request url
#: (``…/-/merge_requests/<n>``): the repository url before it, and the number.
PROPOSAL_URL = re.compile(r"^(https://[^/\s]+/\S+?)(?:/pull/|/-/merge_requests/)(\d+)(?:[/?#]\S*)?$")

#: The verb that reads one proposal back from the broker.
PROPOSAL_VIEW_VERB = "proposal-view"

#: The neutral state a forge reports for a proposal still open (``providers/*/translate.py``).
OPEN_STATE = "open"

#: How long before the check a proposal may have been created and still be the
#: one this command opened. The hook runs as the command returns, but one
#: command can run for a while before its last proposal: a fleet audit lands
#: one remediation pull request per group. A pull request quoted from anywhere
#: older than this (a log, an issue body, a file) is refused.
CREATED_WITHIN_SECONDS = 900

#: How far ahead of this pod's clock the forge's ``created`` may read.
CLOCK_SKEW_SECONDS = 60

#: The most distinct record urls one output is checked for. Each check is two
#: broker reads through the sandbox, and terminal output can carry text the
#: worker read rather than wrote, so the count is bounded. An ordinary
#: ``proposal create`` prints one record; a fleet audit prints one per group
#: it opened, and a group past this count falls back to the worker's note.
RECORDS_CHECKED_MAX = 5

#: ``created`` as forges spell UTC, and the offset ``datetime.fromisoformat``
#: accepted before Python 3.11.
ISO_UTC_SUFFIX = "Z"
ISO_UTC_OFFSET = "+00:00"


def register(ctx):
    ctx.register_hook("transform_terminal_output", on_terminal_output)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)


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


def _forge():
    """The agent pod's ``forge`` module, which reaches the broker through the sandbox."""
    for directory in SCRIPTS_DIRS:
        if directory not in sys.path:
            sys.path.append(directory)
    import forge

    return forge


def _age_seconds(created, now):
    """Seconds since the forge's ``created`` timestamp, or None when it cannot be read."""
    try:
        when = datetime.fromisoformat(str(created).replace(ISO_UTC_SUFFIX, ISO_UTC_OFFSET))
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return (now - when).total_seconds()


class Checker:
    """Asks the forge about record-shaped lines, sharing one provider so the credential's login is read once."""

    def __init__(self):
        self.forge = _forge()
        self.provider = self.forge.BrokerProvider()

    def ours(self, proposal, now=None):
        """The forge's ``{url, number, title}`` when it says the agent just opened ``proposal``, else None.

        The broker must answer ``proposal-view`` for that number on the
        repository in its url (a repository this install does not manage is
        refused there), the answer must carry the same url, be open, have been
        created within :data:`CREATED_WITHIN_SECONDS`, and its author must be the
        account the broker's credential authenticates as on that forge. What is
        recorded is the forge's answer, never the line's own fields.
        """
        match = PROPOSAL_URL.match(proposal["url"])
        if match is None:
            return None
        repository, number = match.group(1), int(match.group(2))
        viewer = self.provider.viewer_login(repository)
        answer = self.forge.call(PROPOSAL_VIEW_VERB, {"number": number}, repository, retry_transient=True)
        view = answer.get("proposal") or {}
        age = _age_seconds(view.get("created"), now or datetime.now(timezone.utc))
        if not (
            viewer
            and view.get("url") == proposal["url"]
            and view.get("number") == number
            and view.get("state") == OPEN_STATE
            and age is not None
            and -CLOCK_SKEW_SECONDS <= age <= CREATED_WITHIN_SECONDS
            and self.forge.normalise_login(view.get("author")) == self.forge.normalise_login(viewer)
        ):
            return None
        return {"url": view["url"], "number": number, "title": str(view.get("title") or "")[:TITLE_MAX]}


def record(task_id, proposal):
    """Append a ``pr_opened`` event for ``proposal`` to card ``task_id``, in its current run."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn, kb.write_txn(conn):
        kb._append_event(conn, task_id, PR_OPENED_KIND, proposal, run_id=kb._current_run_id(conn, task_id))


def _recorded(checker, task_id, proposal):
    """Check ``proposal`` with the forge and record the forge's answer on the card.

    True when recorded, False when the forge says it is not one the agent just
    opened, None when the check or the record failed (the broker or the sandbox
    hop did not answer, or the board refused the write).
    """
    try:
        confirmed = checker.ours(proposal)
        if confirmed is None:
            logger.warning("pr_opened: %s is not a pull request this agent just opened; not recorded", proposal["url"])
            return False
        record(task_id, confirmed)
        return True
    except Exception as exc:  # noqa: BLE001 — the PR is open; the thread falls back to the note
        logger.warning("pr_opened: recording %s on %s failed: %s", proposal.get("url"), task_id, exc)
        return None


def on_terminal_output(command="", output="", **_):
    """Record each proposal the agent opened on the worker's card, and take its line out of ``output``.

    At most :data:`RECORDS_CHECKED_MAX` distinct urls in one output are asked
    about; a line repeating a url already asked about follows that answer, and
    lines past the cap are left as they are, unasked. The first check that fails
    rather than answers ends the asking for this output, so a broker or sandbox
    hop that is down costs one timeout, not one per url. Returns the output
    without the recorded lines, or None (unchanged) when none was recorded.
    """
    if not isinstance(output, str) or PROPOSAL_OPENED_KEY not in output:
        return None
    task_id = os.environ.get(TASK_ENV, "").strip()
    if not task_id:
        return None
    checker, verdicts, kept, failed = None, {}, [], False
    for line in output.splitlines(keepends=True):
        proposal = opened(line)
        url = proposal["url"] if proposal else None
        if url is not None and url not in verdicts and len(verdicts) < RECORDS_CHECKED_MAX and not failed:
            try:
                checker = checker or Checker()
            except Exception as exc:  # noqa: BLE001 — no forge here; the thread falls back to the note
                logger.warning("pr_opened: the forge is not reachable from this process: %s", exc)
                return None
            verdict = _recorded(checker, task_id, proposal)
            failed = verdict is None
            verdicts[url] = bool(verdict)
        if not verdicts.get(url):
            kept.append(line)
    return "".join(kept) if any(verdicts.values()) else None


def _own_run_id():
    """``(task_id, run_id)`` when this process is a dispatcher worker with both set, else None."""
    task_id = os.environ.get(TASK_ENV, "").strip()
    raw = os.environ.get(RUN_ID_ENV, "").strip()
    if not task_id or not raw:
        return None
    try:
        return task_id, int(raw)
    except ValueError:
        return None


def _card_run(task_id):
    """``(status, current_run_id)`` of card ``task_id`` on this process's board, or None when it has no row.

    The read waits at most CARD_READ_BUSY_MS on a locked board, not Hermes' 120 s writer timeout: Hermes
    gives a pre_tool_call hook 30 s and blocks the call when it runs over, which would refuse a live
    card's submit. A board still locked after that raises, and the caller lets the call through.
    """
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        conn.execute(f"PRAGMA busy_timeout={CARD_READ_BUSY_MS}")
        row = conn.execute(CARD_RUN_SQL, (task_id,)).fetchone()
    return None if row is None else (row[0], row[1])


# How long the guard's card read waits on a locked board (see _card_run).
CARD_READ_BUSY_MS = 2000


def ended_run_text(task_id, status):
    """What the worker reads in place of the command's output."""
    return (
        f"Refused: card {task_id} is {status} and your run on it has ended, so this session may not run "
        "submit_suggestion.py. A proposal you put in your result is the answer: the requester reads it, and a "
        "[next: ...] line in it is theirs to send, never yours to carry out. If they want the pull request, "
        "they will ask, and that request carries it. Do not retry this another way; end your turn."
    )


def on_pre_tool_call(tool_name="", args=None, **_):
    """Block ``submit_suggestion.py prepare``/``submit`` from a worker whose own card run has ended.

    Returns Hermes' ``{"action": "block", "message": ...}`` directive, or None to let the call through.
    Only a board that answers, for this worker's own card, a status other than ``running`` or a current
    run other than this worker's, blocks; every other case (not a terminal call, not this script, not a
    dispatcher worker, no row, a board that cannot be read) passes, as it did before this check existed.
    """
    if tool_name != TERMINAL_TOOL or not isinstance(args, dict):
        return None
    command = args.get("command")
    if not isinstance(command, str) or not runs_submit_suggestion(command):
        return None
    own = _own_run_id()
    if own is None:
        return None
    task_id, run_id = own
    try:
        card = _card_run(task_id)
    except Exception as exc:  # noqa: BLE001 -- an unreadable board keeps today's behaviour
        logger.warning("pr_opened: could not read card %s before submit_suggestion.py: %s", task_id, exc)
        return None
    if card is None:
        return None
    status, current = card
    if status == RUNNING_STATUS and current == run_id:
        return None
    logger.warning(
        "pr_opened: refused submit_suggestion.py from run %s on %s (status %s, current run %s)",
        run_id, task_id, status, current,
    )
    return {"action": "block", "message": ended_run_text(task_id, status)}
