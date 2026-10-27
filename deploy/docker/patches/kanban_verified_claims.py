"""Back every claim a worker marks "verified" with a command that ran in its card.

Installed into the image at ``/opt/hermes/tools/kanban_verified_claims.py`` and
wired in by ``deploy/docker/patches/apply_kanban_verified_claims.py``:

``tools/terminal_tool_result.py``
    :func:`record_terminal_call` keeps each foreground terminal call of a
    kanban worker (command, exit code, the output the model saw, the time) in
    memory, keyed by the card in ``HERMES_KANBAN_TASK``.

``tools/kanban_tools.py``
    :func:`apply_completion_rules` runs each rule in :data:`RULES` before the
    card closes. One, :func:`verified_claims_rule`, checks
    ``kanban_complete``'s new ``verified_claims`` parameter against that
    record, or against the claims the gate already accepted on the cards this
    card created. Another rule can read the record through
    :func:`card_terminal_calls`, find a card's descendants through
    :func:`descendant_card_ids`, and share the one refusal.

Why
---
An external pilot on 0.9.0 asked a cost review to verify its claims. The report
came back with "Verification / Falsification check" blocks and "verified"
labels, and no command or output behind any of them (#3200). A "verified"
label with nothing behind it tells the reader they do not need to check.

The fleet-audit findings file already carries ``evidence.command``, but
``audit_report.py`` runs as a subprocess and cannot see the agent's tool calls,
so it can only require the field, not check it. The worker process can: every
terminal call passes through it, and ``kanban_complete`` runs in it.

Upstream's ``agent/verification_evidence.py`` is not a substitute. It records
only test, lint and build commands inside a code project, and only when
verify-on-stop is on; a ``kubectl`` or ``gcloud`` read is never in it.

What a claim is
---------------
Only an entry in ``verified_claims`` is a verified claim. Prose in ``result``
is not parsed. Each entry gives ``claim``, ``command`` and ``excerpt``:

- ``command`` must equal, after whitespace is collapsed, a terminal command
  this card ran;
- ``excerpt`` must appear, after whitespace is collapsed, in the output of a
  run of that command, starting and ending on a word edge, and hold at least
  :data:`MIN_EXCERPT_CHARS` characters besides whitespace unless it is the
  command's whole output. So ``Ready`` does not match inside ``NotReady``,
  ``1`` picked out of a longer output backs nothing, and a precise query whose
  whole output is ``137`` still can.

None of ``claim``, ``command`` and ``excerpt`` may hold a code fence (``` or
~~~), and ``claim`` is rendered on one line, so the rendered section cannot
break.

A card that delegated the work did not run the commands itself. Its claim is
also accepted when it matches a claim the gate already accepted on a card this
card created, directly or through its own children (``kanban_worker_children``,
which ``kanban_children_settled.py`` records): the same command, and an excerpt
inside that claim's excerpt. So a "verified" label on the reply is always one
the gate checked, on the card that ran the command. No other card's claims
count.

The gate copies ``exit_code`` and ``ran_at`` from the recorded call, or from
the child card's accepted claim; the model does not supply them. Accepted
claims are stored in ``metadata.verified_claims``, which the gate always
rewrites, so a ``kanban_complete`` call cannot put unchecked claims there.
Other writers of a completed run's metadata (``hermes kanban edit`` or
``complete --metadata``, the dashboard) bypass the gate, and a parent reads
what they wrote as accepted. They are also
rendered as an evidence section at the end of ``result``, the text the notifier
posts to chat. The notifier keeps the head of ``result`` and clips it at
``kanban_notifier.RESULT_LIMIT``: a body near that limit pushes the section out
of the chat message, though the card still holds it.

Never wedging the card
----------------------
Same policy as ``kanban_result_required.py``, for the same reasons: the first
completion with a problem is refused with every rule's reasons in one message;
the retry inside :data:`NUDGE_TTL_SECONDS` is accepted, the reasons are logged
at WARNING, and the card closes. The refusal is shared by all rules in
:data:`RULES`, so two rules cannot refuse one card twice. The other
``kanban_complete`` gates (``kanban_result_required``,
``kanban_children_settled``) keep their own refusal: a completion that fails
one of them and a rule, fixed one problem per call, is refused once by each
and closes on its third call at worst. On the retry the bad claims are
dropped, so a dropped claim is never shown as verified.

Limits
------
- The record is per process. A caller with no ``HERMES_KANBAN_TASK`` (the
  gateway completing a card) has no record of its own: only claims that match
  one the gate accepted on a descendant pass, and the rest are refused once
  and then dropped.
- The record holds the output the model saw: after the head-and-tail cut. An
  excerpt read later from a spill file is not in it.
- Background processes are not recorded.
- A child card's claims count only once that card completed with them.
- The gate catches evidence a model made up or rewrote, not deliberate
  deception. It proves that a command string ran and printed the excerpt, not
  where its output came from: a worker can change ``PATH`` or ``KUBECONFIG``
  in the sandbox between calls.
- Prose in ``result`` is not checked. A card can skip ``verified_claims`` and
  write the evidence heading by hand; the platform and cluster ``SOUL.md``
  rules, not this gate, cover that. A card that quotes a child card's result
  carries that child's real heading, so the heading alone proves nothing.
- A claim backed by a command that failed is accepted, since "not found" is
  evidence, and renders as ``exit N, the command failed``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

#: The card a dispatcher-spawned worker is running, as the dispatcher sets it.
TASK_ENV = "HERMES_KANBAN_TASK"

#: The parameter ``kanban_complete`` takes, and the ``metadata`` key it fills.
PARAM = "verified_claims"
METADATA_KEY = "verified_claims"

#: The most terminal calls kept per card. Each holds output already cut to
#: Hermes' terminal output limit; a worker that runs more than this has its
#: oldest calls forgotten, and a claim citing one is refused like any other.
MAX_RECORDED_CALLS = 200

#: The most claims one completion may carry, and the longest claim, command
#: and excerpt. Together they keep the evidence section under about 15,000
#: characters, half of ``kanban_notifier.RESULT_LIMIT`` (30000).
MAX_CLAIMS = 10
MAX_CLAIM_CHARS = 300
MAX_COMMAND_CHARS = 500
MAX_EXCERPT_CHARS = 600

#: The fewest characters besides whitespace an excerpt may hold, unless it is
#: the command's whole output. Shorter, and it matches by chance: ``1``,
#: ``Yes``, ``Ready``.
MIN_EXCERPT_CHARS = 8

#: ``kanban_children_settled.CHILDREN_TABLE``: which card's worker created
#: which card. Created on the first fan-out, so it can be absent.
CHILDREN_TABLE = "kanban_worker_children"

#: Every card created by ``task_id``, by its children, and so on down.
#: ``UNION`` drops repeats, so a cycle in the table cannot loop.
DESCENDANTS_SQL = (
    f"WITH RECURSIVE descendants(id) AS ("
    f" SELECT child_id FROM {CHILDREN_TABLE} WHERE creator_id = ?"
    f" UNION SELECT c.child_id FROM {CHILDREN_TABLE} c"
    f" JOIN descendants d ON c.creator_id = d.id"
    f") SELECT id FROM descendants"
)

#: The ``task_runs.outcome`` of a run that closed through ``kanban_complete``.
COMPLETED_OUTCOME = "completed"

#: How long a refusal waits for its retry. The same window as
#: ``kanban_result_required.NUDGE_TTL_SECONDS``; see that module for why the
#: memory expires.
NUDGE_TTL_SECONDS = 15 * 60

#: The longest piece of a bad claim quoted back in the refusal.
QUOTE_CHARS = 80

#: ``ran_at``'s format: UTC, to the second.
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

EVIDENCE_HEADING = "**Evidence for verified claims**"
FENCE = "```"
#: The other fence opener Markdown and the Slack renderers read.
TILDE_FENCE = "~~~"
WHITESPACE = re.compile(r"\s+")

PARAM_DESCRIPTION = (
    "Optional. Every claim you present as verified, checked or confirmed, with "
    f"the command that proves it: at most {MAX_CLAIMS} claims. Each item is "
    f"{{claim, command, excerpt}}: ``claim`` is one sentence (at most "
    f"{MAX_CLAIM_CHARS} characters); ``command`` is a terminal command you ran "
    f"in this card (at most {MAX_COMMAND_CHARS} characters), copied exactly; "
    f"``excerpt`` is the lines of its output that prove the claim, copied "
    f"exactly (whole words, at most {MAX_EXCERPT_CHARS} characters, and at least "
    f"{MIN_EXCERPT_CHARS} unless it is the command's whole output). None of the three may hold a {FENCE} or "
    f"{TILDE_FENCE} fence. A claim backed by a command that failed must say what "
    f"failed. "
    "You may also repeat a verified claim from a card you created, with its "
    "command and excerpt as that card gave them. The tool checks each claim, "
    "adds the exit code and the time the command ran, and appends the evidence "
    "to ``result``. A claim that fails the check is refused, and is dropped on "
    "the retry. Do not call anything verified in ``result`` unless it is listed "
    "here."
)

PARAM_SCHEMA = {
    "type": "array",
    "maxItems": MAX_CLAIMS,
    "items": {
        "type": "object",
        "properties": {
            "claim": {"type": "string", "description": "The claim, in one sentence."},
            "command": {
                "type": "string",
                "description": "The terminal command that proves it, exactly as you ran it.",
            },
            "excerpt": {
                "type": "string",
                "description": "The lines of that command's output that prove it, copied exactly.",
            },
        },
        "required": ["claim", "command", "excerpt"],
    },
    "description": PARAM_DESCRIPTION,
}

REFUSAL_HEAD = (
    "kanban_complete refused: {count} problem(s) below. Your task is still in "
    "flight. Fix each one and call kanban_complete again with the same result. "
    "This check accepts the next call: a verified claim that still fails is "
    "dropped, not shown as verified."
)

#: Card id to the calls it ran, oldest first.
_calls: dict[str, deque] = {}

#: Card id to ``time.monotonic()`` of its last refusal.
_refused_at: dict[str, float] = {}


def _collapse(text: str) -> str:
    return WHITESPACE.sub(" ", text).strip()


def _quote(text: object) -> str:
    text = _collapse(str(text))
    return text if len(text) <= QUOTE_CHARS else text[: QUOTE_CHARS - 1] + "…"


def record_terminal_call(command: object, exit_code: object, output: object, now: float | None = None) -> None:
    """Remember one foreground terminal call of the current card, if there is one."""
    task_id = os.environ.get(TASK_ENV)
    if not task_id or not isinstance(command, str) or not command.strip():
        return
    calls = _calls.setdefault(task_id, deque(maxlen=MAX_RECORDED_CALLS))
    calls.append(
        {
            "command": _collapse(command),
            "exit_code": exit_code,
            "output": _collapse(str(output or "")),
            "ran_at": datetime.fromtimestamp(time.time() if now is None else now, timezone.utc).strftime(TIME_FORMAT),
        }
    )


def descendant_card_ids(conn, task_id: object) -> list[str]:
    """Every card that ``task_id``'s worker created, and their descendants, sorted.

    ``conn`` is an open kanban board connection. The shared lookup for a
    completion rule that reads child cards. Empty when no card has fanned out
    yet and the table does not exist.
    """
    try:
        rows = conn.execute(DESCENDANTS_SQL, (str(task_id),)).fetchall()
    except Exception as exc:  # noqa: BLE001 - most often: no such table yet
        logger.debug("[kanban] no descendants read for %s: %r", task_id, exc)
        return []
    return sorted({str(row[0]) for row in rows} - {str(task_id)})


def descendant_verified_claims(conn, task_id: object) -> list[dict]:
    """The claims the gate accepted on ``task_id``'s descendants, each with its ``card``."""
    ids = descendant_card_ids(conn, task_id)
    if not ids:
        return []
    marks = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT task_id, metadata FROM task_runs WHERE outcome = ? AND task_id IN ({marks})",
        (COMPLETED_OUTCOME, *ids),
    ).fetchall()
    claims = []
    for card, raw in rows:
        try:
            stored = (json.loads(raw) if raw else {}).get(METADATA_KEY) or []
        except (ValueError, AttributeError):
            continue
        for claim in stored if isinstance(stored, list) else []:
            if isinstance(claim, dict) and all(isinstance(claim.get(f), str) for f in ("command", "excerpt")):
                # A claim this descendant relayed keeps the card that ran the command.
                claims.append({**claim, "card": claim.get("card") or str(card)})
    return claims


def _load_child_claims(connect, task_id: str) -> list[dict]:
    """:func:`descendant_verified_claims` on a fresh connection; empty on any error."""
    if connect is None:
        return []
    try:
        conn = connect()
        try:
            return descendant_verified_claims(conn, task_id)
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - a failed read only means no child claims
        logger.warning("[kanban] card %s: child claims not read: %r", task_id, exc)
        return []


def _word_char(char: str) -> bool:
    return char.isalnum() or char == "_"


def _excerpt_in(needle: str, haystack: str) -> bool:
    """Whether ``needle`` appears in ``haystack`` starting and ending on a word edge.

    A side whose own end character is not a word character is an edge already,
    so ``"exitCode":137`` matches inside a longer JSON line.
    """
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        left = start == 0 or not (_word_char(needle[0]) and _word_char(haystack[start - 1]))
        right = end == len(haystack) or not (_word_char(needle[-1]) and _word_char(haystack[end]))
        if left and right:
            return True
        start = haystack.find(needle, start + 1)
    return False


def _is_short(needle: str) -> bool:
    return len(needle.replace(" ", "")) < MIN_EXCERPT_CHARS


def _backs(needle: str, text: str) -> bool:
    """Whether ``text`` backs the excerpt ``needle``: on word edges, or as all of it when the excerpt is short."""
    if _is_short(needle):
        return needle == text
    return _excerpt_in(needle, text)


def _from_child(child_claims, wanted: str, needle: str) -> dict | None:
    for child in child_claims():
        if _collapse(child["command"]) == wanted and _backs(needle, _collapse(child["excerpt"])):
            return child
    return None


def _check_claim(calls: list, item: object, child_claims) -> tuple[dict | None, str | None]:
    """Return ``(accepted, None)`` or ``(None, reason)`` for one claim.

    ``child_claims`` is called only when this card's own record does not back
    the claim, so a card with no delegated work never opens the board.
    """
    if not isinstance(item, dict):
        return None, "is not an object with claim, command and excerpt"
    for field in ("claim", "command", "excerpt"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            return None, f"has no {field}"
    # The claim is rendered as one line of prose, so it is kept on one.
    claim, command, excerpt = _collapse(item["claim"]), item["command"].strip(), item["excerpt"].strip("\n")
    if len(claim) > MAX_CLAIM_CHARS:
        return None, f"claim is longer than {MAX_CLAIM_CHARS} characters"
    if len(command) > MAX_COMMAND_CHARS:
        return None, f"command is longer than {MAX_COMMAND_CHARS} characters; cite a shorter command that shows the claim"
    if len(excerpt) > MAX_EXCERPT_CHARS:
        return None, f"excerpt is longer than {MAX_EXCERPT_CHARS} characters; keep the lines that prove the claim"
    if any(fence in part for part in (claim, command, excerpt) for fence in (FENCE, TILDE_FENCE)):
        return None, f"holds a {FENCE} or {TILDE_FENCE} fence; cite lines without one"
    wanted, needle = _collapse(command), _collapse(excerpt)
    accepted = {"claim": claim, "command": command, "excerpt": excerpt}
    runs = [call for call in calls if call["command"] == wanted]
    for call in reversed(runs):
        if _backs(needle, call["output"]):
            return {**accepted, "exit_code": call["exit_code"], "ran_at": call["ran_at"]}, None
    child = _from_child(child_claims, wanted, needle)
    if child is not None:
        return {**accepted, "exit_code": child.get("exit_code"), "ran_at": child.get("ran_at"), "card": child["card"]}, None
    if _is_short(needle) and (runs or any(_collapse(other["command"]) == wanted for other in child_claims())):
        return None, (
            f"excerpt {_quote(excerpt)!r} holds fewer than {MIN_EXCERPT_CHARS} characters and is not the whole "
            f"output of {_quote(command)!r}; cite the whole line that proves the claim"
        )
    if not runs and len(calls) >= MAX_RECORDED_CALLS:
        return None, (
            f"command {_quote(command)!r} is not in this card's record, which keeps only its last "
            f"{MAX_RECORDED_CALLS} commands; run it again, then cite that run"
        )
    if not runs:
        return None, (
            f"command {_quote(command)!r} did not run in this card, and no card it created "
            "verified it; copy it exactly as it ran"
        )
    return None, f"excerpt {_quote(excerpt)!r} is not in the output of {_quote(command)!r}; copy it exactly"


def render_evidence(claims: list) -> str:
    """The section appended to ``result``: each claim, its command in one fence, its excerpt in the next.

    Two fences keep a multi-line command apart from its output. No field can
    hold a fence of its own (:func:`_check_claim` refuses one), and the claim
    is one line.
    """
    parts = [EVIDENCE_HEADING]
    for claim in claims:
        where = f", on card {claim['card']}" if claim.get("card") else ""
        failed = ", the command failed" if claim["exit_code"] not in (0, None) else ""
        parts.append(f"{claim['claim']} (exit {claim['exit_code']}{failed}, {claim['ran_at']}{where})")
        parts.append(f"{FENCE}\n$ {claim['command']}\n{FENCE}\n{FENCE}\n{claim['excerpt']}\n{FENCE}")
    return "\n\n".join(parts)


def card_terminal_calls(task_id: object) -> list[dict]:
    """The terminal calls recorded for ``task_id``, oldest first.

    Each is ``{command, exit_code, output, ran_at}``, with whitespace in
    ``command`` and ``output`` collapsed. The read API for a completion rule.
    """
    return list(_calls.get(str(task_id), ()))


def verified_claims_rule(
    *, task_id: str, args: dict, result: object, metadata: object, calls: list, retry: bool, redact, connect=None, **_
) -> tuple[list, object, object]:
    """The completion rule for ``verified_claims``; see :data:`RULES` for the contract.

    On the retry the claims that fail are dropped and the rest are kept, so a
    dropped claim is never shown as verified. A ``verified_claims`` key the
    model put in ``metadata`` itself is always removed.
    """
    if isinstance(metadata, dict) and METADATA_KEY in metadata:
        metadata = {key: value for key, value in metadata.items() if key != METADATA_KEY}
    claims = args.get(PARAM)
    if claims is None or claims == []:
        return [], result, metadata
    items = claims if isinstance(claims, list) else [claims]
    loaded: list = []

    def child_claims() -> list:
        if not loaded:
            loaded.append(_load_child_claims(connect, task_id))
        return loaded[0]

    accepted, reasons = [], []
    for index, item in enumerate(items):
        if index >= MAX_CLAIMS:
            claim, reason = None, f"is past the limit of {MAX_CLAIMS} claims"
        else:
            claim, reason = _check_claim(calls, item, child_claims)
        if reason:
            label = _quote(item.get("claim", "")) if isinstance(item, dict) else ""
            reasons.append(f"verified_claims[{index}] {label!r}: {reason}")
        else:
            accepted.append(claim)

    if (reasons and not retry) or not accepted:
        return reasons, result, metadata
    if metadata is not None and not isinstance(metadata, dict):
        # Upstream's _require_dict_metadata refuses this next; nothing to add to.
        return reasons, result, metadata
    stored = [
        {**claim, **{field: redact(claim[field]) for field in ("claim", "command", "excerpt")}}
        for claim in accepted
    ]
    metadata = {**(metadata or {}), METADATA_KEY: stored}
    section = render_evidence(stored)
    body = str(result).rstrip() if result else ""
    result = f"{body}\n\n{section}" if body else section
    return reasons, result, metadata


#: The rules :func:`apply_completion_rules` runs, in order. Each is called with
#: keyword arguments ``task_id``, ``args`` (the ``kanban_complete`` arguments),
#: ``result``, ``metadata``, ``calls`` (:func:`card_terminal_calls`), ``retry``,
#: ``redact`` and ``connect`` (opens a kanban board connection, or ``None``;
#: the caller closes what it opens), and returns ``(reasons, result, metadata)``. ``reasons`` is
#: the list of problems, each one line the worker can act on. ``retry`` is true
#: when this card was refused inside :data:`NUDGE_TTL_SECONDS`: the completion
#: is then accepted whatever the reasons, so a rule must return a ``result``
#: and ``metadata`` that are safe to store as they are. A rule should accept
#: ``**kwargs`` so that a new argument does not break it.
RULES: list = [verified_claims_rule]


def apply_completion_rules(
    task_id: object,
    args: dict,
    result: object,
    metadata: object,
    redact=lambda value: value,
    connect=None,
) -> tuple[str | None, object, object]:
    """Run every rule in :data:`RULES` on one ``kanban_complete`` call.

    Returns ``(error, result, metadata)``. When ``error`` is set the completion
    must not be written. One refusal per card covers every rule: the first
    completion with a problem is refused with all the reasons in one message,
    and the retry is accepted. ``redact`` is applied to every string a rule
    stores, as ``kanban_complete`` applies it to ``result``. ``connect`` is
    ``hermes_cli.kanban_db_connect.connect`` in the image, passed in so this
    module imports outside it.
    """
    key = str(task_id)
    refused_at = _refused_at.pop(key, None)
    retry = refused_at is not None and time.monotonic() - refused_at <= NUDGE_TTL_SECONDS
    calls = card_terminal_calls(key)

    reasons, new_result, new_metadata = [], result, metadata
    for rule in RULES:
        found, new_result, new_metadata = rule(
            task_id=key, args=args, result=new_result, metadata=new_metadata,
            calls=calls, retry=retry, redact=redact, connect=connect,
        )
        reasons += found

    if reasons and not retry:
        _refused_at[key] = time.monotonic()
        lines = [REFUSAL_HEAD.format(count=len(reasons))] + [f"- {reason}" for reason in reasons]
        return "\n".join(lines), result, metadata
    for reason in reasons:
        logger.warning("[kanban] card %s: accepted on the retry with: %s", key, reason)
    return None, new_result, new_metadata


def apply_schema(schema: dict) -> dict:
    """Add the ``verified_claims`` parameter to ``KANBAN_COMPLETE_SCHEMA`` in place."""
    properties = schema["parameters"]["properties"]
    if PARAM in properties:
        raise RuntimeError(f"kanban_complete already has a {PARAM!r} parameter; re-derive this patch")
    properties[PARAM] = PARAM_SCHEMA
    return schema
