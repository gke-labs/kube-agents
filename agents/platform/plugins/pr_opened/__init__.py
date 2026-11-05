"""Claim each proposal the agent opens, count it per origin, and record a worker's on its card.

The agent opens a pull request or merge request only through the credential
broker's ``proposal-create`` verb: the sandbox's ``/v1/exec`` runs neither
``git`` nor a forge CLI, and every caller (``vcs.py``, submit-suggestion, the
fleet audit) reaches the broker through ``vcs_client.call``. When the broker
answers with a new proposal, ``call`` writes one JSON line to stderr naming it
(``vcs_client.PROPOSAL_OPENED_KEY``), with the one-time claim token the broker
minted for that answer in its ``claim`` field and the managed repository the
broker resolved in its ``repo`` field. That line comes back as part of the
terminal output of the process that ran the command.

This hook sees that output before the model does. A line that has the record's
shape is not proof by itself: terminal output also carries text nobody here
wrote (a commit message, a file, a pod's log, another session's record). So each
record's token is presented to the broker's ``proposal-claim`` verb
(:func:`claim`), addressed to the record's ``repo`` whatever shape the forge
gives its url (``…/pull/7``, ``…/pulls/7``, ``…/pull-requests/7``,
``…/-/merge_requests/7``). The broker burns the token, checks with the forge
that the proposal the token was minted for is open, recent and authored by the
broker's own account, and answers the forge's ``url``, ``number`` and ``title``. Only a claimed
proposal counts: it is written to the Session KV ledger
(``POST /v1/proposals/opened``) with the origin of this process
(:func:`resolve_origin`), and in a kanban worker it is also appended to the card
as a ``pr_opened`` event, carrying the forge's fields and never the line's, and
the line is taken out of the output. Outside a worker the line stays, without
its token. The gateway's notifier claims the event kind and, with
``KAGE_SLACK_UX`` on, posts the PR as its own message in the thread the card
reports to (``gateway/kanban_progress_lines.py``, ``gateway/slack_ux_moments.py``).

At most :data:`RECORDS_CHECKED_MAX` distinct urls in one output are claimed,
and none once :data:`CLAIMS_BUDGET_SECONDS` have passed since the hook began on
that output: Hermes skips a ``transform_terminal_output`` hook that runs past
its 30 s callback timeout and hands the model the original output, tokens
included. A record with no ``repo`` and a url :data:`PROPOSAL_URL` cannot read
names no repository to send the claim to and takes none of those slots. Every
record line the hook does not claim (no repository, past the cap or the
budget, a repeated url, a refused claim, a broker hop that is down) keeps its
text but loses its ``claim`` field, so no live token reaches what the model
reads, unless one claim alone outlasts Hermes' timeout. The claim is one broker
call through the sandbox (``forge.call``), made in line rather than on a thread
because a worker leaves through ``os._exit``, which would drop a thread still
waiting on the broker. A claim or a write that fails is logged: the PR is open
either way, and it counts as missed. Background processes are not covered,
since this hook runs on foreground output only. Design:
``docs/designs/proposal-origin.md`` §2.2.

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

import importlib
import json
import logging
import os
import re
import sys
import time
import urllib.request

logger = logging.getLogger(__name__)

#: ``vcs_client.PROPOSAL_OPENED_KEY`` / ``_EVENT`` / ``_FIELDS``, held here because a
#: plugin is loaded by file path and the sandbox's client is not on its import
#: path. ``test_pr_opened.py`` fails when the two drift.
PROPOSAL_OPENED_KEY = "kube-agents.event"
PROPOSAL_OPENED_EVENT = "proposal-opened"
PROPOSAL_OPENED_FIELDS = ("url", "number", "title", "claim", "repo")

#: The record's field that holds the broker's one-time claim token
#: (``vcs_client.CLAIM_TOKEN_FIELD``).
CLAIM_FIELD = "claim"

#: The record's field that names the managed repository the broker resolved for
#: the create (``vcs_client.REPO_FIELD``). The claim is sent to it; the broker
#: checks what it kept with the token, never this.
REPO_FIELD = "repo"
#: A record's ``repo`` travels as one argv element of the sandbox hop, so a
#: value an option parser could read as a flag, or one with whitespace, is
#: not a repository and the record falls back to its url, or takes no claim.
REPO_ARGV_SAFE = re.compile(r"^[^-\s][^\s]*$")

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

#: How a record line is written back once its token is taken out, as
#: ``vcs_client._say_opened`` writes it.
RECORD_SEPARATORS = (",", ":")

#: Where the agent pod's shared scripts (``forge.py`` and what it imports) are
#: staged, as ``vcs_client`` finds its siblings there.
SCRIPTS_DIRS = ("/opt/defaults/scripts", "/opt/data/scripts")

#: A GitHub-shaped pull request url (``…/pull/<n>``) or a merge request url
#: (``…/-/merge_requests/<n>``): the repository url before it, and the number.
#: Read only for a record with no ``repo`` (one an older client wrote), and for
#: the card event's number when the broker's answer has none. The Session KV
#: server pins its own pattern equal to this one.
PROPOSAL_URL = re.compile(r"^(https://[^/\s]+/\S+?)(?:/pull/|/-/merge_requests/)(\d+)(?:[/?#]\S*)?$")

#: A ``"claim": "<token>"`` member wherever it sits on a line, for a record that
#: does not parse as a whole line (one ``2>&1`` merged after other text). Its
#: value is blanked, so the line keeps its shape and loses the token.
CLAIM_MEMBER = re.compile(r'("claim"\s*:\s*)"[^"]*"')
CLAIM_MEMBER_BLANKED = r'\1""'

#: The broker verb that burns a claim token and confirms its proposal with the forge.
PROPOSAL_CLAIM_VERB = "proposal-claim"

#: ``forge.REASON_SANDBOX_UNREACHABLE`` and ``forge.REASON_UNREACHABLE``: the
#: reasons that mean the claim never reached the broker, rather than the broker
#: refusing it. An exception without a reason is read the same way.
HOP_DOWN_REASONS = frozenset({"SANDBOX_UNREACHABLE", "BROKER_UNREACHABLE"})

#: The most distinct record urls one output is claimed for. Each claim is one
#: broker call through the sandbox, and terminal output can carry text the
#: process read rather than wrote, so the count is bounded. An ordinary
#: ``proposal create`` prints one record; a fleet audit prints one per group
#: it opened, and a group past this count is missed.
RECORDS_CHECKED_MAX = 5

#: No claim starts once this long has passed since the hook began on one
#: output. Hermes skips the hook at 30 s (``hook_callback_timeout``) and hands
#: the model the raw output, so the budget leaves room for the ledger writes,
#: the card record and the strip that follow the last claim.
CLAIMS_BUDGET_SECONDS = 20

#: Where Hermes installs ``deploy/docker/patches/kanban_origin.py``, and the name
#: a test loads it under.
KANBAN_ORIGIN_MODULES = ("hermes_cli.kanban_origin", "kanban_origin")

#: The origin when nothing resolves one (``kanban_origin.NO_ORIGIN``).
NO_ORIGIN = ("other", "")

#: The Session KV server's loopback address, key and ledger route, as
#: ``kanban_origin`` spells the first two. The server binds 127.0.0.1 only.
SESSION_KV_URL = "http://127.0.0.1:8699"
SESSION_KV_KEY_ENV = "SESSION_KV_API_KEY"
LEDGER_PATH = "/v1/proposals/opened"

#: The ledger write waits at most this long, on loopback.
LEDGER_TIMEOUT_SECONDS = 2.0


def register(ctx):
    ctx.register_hook("transform_terminal_output", on_terminal_output)
    ctx.register_hook("pre_tool_call", on_pre_tool_call)


def _record_of(line):
    """The proposal-opened record object on ``line``, else None."""
    text = line.strip()
    if not text.startswith(JSON_OBJECT_START) or PROPOSAL_OPENED_KEY not in text:
        return None
    try:
        record = json.loads(text)
    except (ValueError, RecursionError):  # not JSON, or nested past the parser's depth
        return None
    if not isinstance(record, dict) or record.get(PROPOSAL_OPENED_KEY) != PROPOSAL_OPENED_EVENT:
        return None
    proposal = record.get("proposal")
    if not isinstance(proposal, dict) or not isinstance(proposal.get("url"), str) or not proposal["url"]:
        return None
    return record


def opened(line):
    """The ``{url, number, title, claim, repo}`` a proposal-opened record on ``line`` names, else None.

    ``claim`` is None when the line carries no token (a broker older than the
    token); ``repo`` is None when the line names no repository (an older client).
    """
    record = _record_of(line)
    if record is None:
        return None
    found = {field: record["proposal"].get(field) for field in PROPOSAL_OPENED_FIELDS}
    found["title"] = str(found.get("title") or "")[:TITLE_MAX]
    return found


def without_claim(line):
    """``line`` with the record's ``claim`` field taken out; ``line`` itself when it has none."""
    record = _record_of(line)
    if record is None or CLAIM_FIELD not in record["proposal"]:
        return line
    record["proposal"].pop(CLAIM_FIELD)
    lead = line[: len(line) - len(line.lstrip())]
    trail = line[len(line.rstrip()):]
    return lead + json.dumps(record, separators=RECORD_SEPARATORS) + trail


def strip_tokens(line):
    """``line`` with every claim token taken out: the record's field, else any ``"claim"`` member."""
    stripped = without_claim(line)
    if PROPOSAL_OPENED_KEY in stripped:
        stripped = CLAIM_MEMBER.sub(CLAIM_MEMBER_BLANKED, stripped)
    return stripped


def _strip_all(output):
    """``output`` with :func:`strip_tokens` applied to every line naming a record. Never raises."""
    kept = []
    for line in output.splitlines(keepends=True):
        if PROPOSAL_OPENED_KEY in line:
            try:
                line = strip_tokens(line)
            except Exception:  # noqa: BLE001 — the member regex alone still takes the token out
                line = CLAIM_MEMBER.sub(CLAIM_MEMBER_BLANKED, line)
        kept.append(line)
    return "".join(kept)


def _forge():
    """The agent pod's ``forge`` module, which reaches the broker through the sandbox."""
    for directory in SCRIPTS_DIRS:
        if directory not in sys.path:
            sys.path.append(directory)
    import forge

    return forge


def claim_repository(record):
    """The repository a claim for ``record`` is sent to, else None.

    The record's ``repo``, whatever the url looks like, when it can travel as
    one argv element (:data:`REPO_ARGV_SAFE`); for a record with none, or one a
    parser could read as a flag (an older client, or a forged line), the
    repository url :data:`PROPOSAL_URL` reads off its url. A record with neither
    takes no claim.
    """
    repo = record.get(REPO_FIELD)
    if isinstance(repo, str) and REPO_ARGV_SAFE.match(repo):
        return repo
    match = PROPOSAL_URL.match(record.get("url") or "")
    return match.group(1) if match else None


def claimable(record):
    """True when ``record`` carries a token and names a repository to send its claim to."""
    token = record.get(CLAIM_FIELD)
    return isinstance(token, str) and bool(token) and claim_repository(record) is not None


class ClaimUnanswered(Exception):
    """The claim did not reach the broker: no forge module here, or the hop is down."""


def claim(record):
    """The forge's ``{url, number, title}`` when the broker accepts ``record``'s token, else None.

    The broker burns the token whatever it then finds, so a claim is never
    retried here; the broker itself asks the forge once more on a transient
    failure. A refusal (an unknown, replayed or expired token, a proposal the
    forge does not confirm, a repository no longer managed) is logged by its code
    alone, never with the token or the url. Raises :class:`ClaimUnanswered` when
    the broker could not be asked.
    """
    if not claimable(record):
        return None
    try:
        forge = _forge()
    except Exception as exc:  # noqa: BLE001 — no forge here; the proposal is missed
        raise ClaimUnanswered(type(exc).__name__) from None
    try:
        answer = forge.call(
            PROPOSAL_CLAIM_VERB,
            {CLAIM_FIELD: record[CLAIM_FIELD], "url": record["url"]},
            claim_repository(record),
            retry_transient=False,
        )
    except Exception as exc:  # noqa: BLE001 — sorted into a refusal or an unanswered hop below
        reason = getattr(exc, "reason", None)
        if not reason or reason in HOP_DOWN_REASONS:
            raise ClaimUnanswered(reason or type(exc).__name__) from None
        logger.warning("pr_opened: %s refused: %s", PROPOSAL_CLAIM_VERB, reason)
        return None
    if not isinstance(answer, dict) or not isinstance(answer.get("url"), str) or not answer["url"]:
        logger.warning("pr_opened: %s answered without a url", PROPOSAL_CLAIM_VERB)
        return None
    number = answer.get("number")
    if number is None:
        match = PROPOSAL_URL.match(answer["url"])
        number = int(match.group(2)) if match else None
    return {"url": answer["url"], "number": number, "title": str(answer.get("title") or "")[:TITLE_MAX]}


def _first_module(names):
    for name in names:
        try:
            return importlib.import_module(name)
        except Exception:  # noqa: BLE001 — absent, or failing at import: either way not here
            continue
    return None


def resolve_origin():
    """This process's ``(origin, platform)``; ``("other", "")`` when nothing resolves one. Never raises.

    ``kanban_origin.turn_origin``, the one resolver the board and the cron
    patches share: the worker's card stamp, then the tick-started run's
    context, then the session record for the trusted Hermes session id, then
    the console id convention on that id.
    """
    origins = _first_module(KANBAN_ORIGIN_MODULES)
    if origins is None:
        return NO_ORIGIN
    try:
        found = origins.turn_origin()
        return tuple(found) if found else NO_ORIGIN
    except Exception as exc:  # noqa: BLE001 — never fail the command over its origin
        logger.warning("pr_opened: the turn's origin is unreadable: %r", exc)
        return NO_ORIGIN


def _session_kv_post(path, payload):
    """POST ``payload`` to the Session KV server with the pod's key; the JSON answer. Raises on failure."""
    token = (os.environ.get(SESSION_KV_KEY_ENV) or "").strip()
    if not token:
        raise PermissionError(f"no {SESSION_KV_KEY_ENV}")
    request = urllib.request.Request(
        SESSION_KV_URL + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=LEDGER_TIMEOUT_SECONDS) as response:
        loaded = json.loads(response.read() or b"{}")
    return loaded if isinstance(loaded, dict) else {}


def write_ledger(url, origin):
    """Write the claimed proposal at ``url`` to the ledger under ``origin``. Logged, never raised."""
    try:
        _session_kv_post(LEDGER_PATH, {"url": url, "origin": origin[0], "platform": origin[1]})
    except Exception as exc:  # noqa: BLE001 — the PR is open; this url's count is lost
        logger.warning("pr_opened: the ledger did not record %s: %r", url, exc)


def record(task_id, proposal):
    """Append a ``pr_opened`` event for ``proposal`` to card ``task_id``, in its current run."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn, kb.write_txn(conn):
        kb._append_event(conn, task_id, PR_OPENED_KIND, proposal, run_id=kb._current_run_id(conn, task_id))


def _recorded(task_id, proposal):
    """Record the claimed ``proposal`` on card ``task_id``; True when written, False (logged) when not."""
    try:
        record(task_id, proposal)
        return True
    except Exception as exc:  # noqa: BLE001 — the PR is open; the thread falls back to the note
        logger.warning("pr_opened: recording %s on %s failed: %s", proposal.get("url"), task_id, exc)
        return False


def on_terminal_output(command="", output="", **_):
    """Claim each proposal record in ``output``, count it in the ledger, and strip every token.

    At most :data:`RECORDS_CHECKED_MAX` distinct urls carrying a token are
    claimed, and no claim starts once :data:`CLAIMS_BUDGET_SECONDS` have passed.
    A claimed proposal goes to the ledger under :func:`resolve_origin`;
    in a kanban worker it also goes on the card and its line, with every line
    repeating its url, leaves the output. Every other line naming a record stays
    without its token. The first claim the broker does not answer ends the
    claiming for this output, so a hop that is down costs one timeout, not one
    per url. Returns the changed output, or None (unchanged) when no line was
    removed or stripped. Once a record is seen, nothing raises: a fault returns
    the output with every token stripped, since the seam would otherwise hand
    the model the original.
    """
    if not isinstance(output, str) or PROPOSAL_OPENED_KEY not in output:
        return None
    try:
        return _claim_output(output)
    except Exception as exc:  # noqa: BLE001 — the seam would hand the model the original, tokens and all
        logger.warning("pr_opened: claiming this output failed (%s); its tokens are stripped", type(exc).__name__)
        return _strip_all(output)


def _claim_output(output):
    """:func:`on_terminal_output`'s work, for an output that names a record. May raise."""
    task_id = os.environ.get(TASK_ENV, "").strip()
    asked, removed, kept, unanswered, origin, changed = set(), set(), [], False, None, False
    deadline = time.monotonic() + CLAIMS_BUDGET_SECONDS
    for line in output.splitlines(keepends=True):
        proposal = opened(line)
        if proposal is not None and proposal["url"] in removed:
            changed = True
            continue
        if (
            proposal is not None
            and claimable(proposal)
            and proposal["url"] not in asked
            and len(asked) < RECORDS_CHECKED_MAX
            and not unanswered
            and time.monotonic() < deadline
        ):
            asked.add(proposal["url"])
            try:
                confirmed = claim(proposal)
            except ClaimUnanswered as exc:
                logger.warning("pr_opened: %s was not answered (%s); this output is not claimed further",
                               PROPOSAL_CLAIM_VERB, exc)
                unanswered, confirmed = True, None
            if confirmed is not None:
                origin = origin or resolve_origin()
                write_ledger(confirmed["url"], origin)
                if task_id and _recorded(task_id, confirmed):
                    removed.add(proposal["url"])
                    changed = True
                    continue
        stripped = strip_tokens(line) if PROPOSAL_OPENED_KEY in line else line
        changed = changed or stripped != line
        kept.append(stripped)
    return "".join(kept) if changed else None


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
