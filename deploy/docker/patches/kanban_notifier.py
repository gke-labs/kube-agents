"""Everything the kanban notifier does with a card's terminal event.

Installed into the image at ``/opt/hermes/gateway/kanban_notifier.py`` and
wired into ``gateway/kanban_watchers.py`` by
``deploy/docker/patches/apply_kanban_notifier.py``.

One module because it is one code path. When a card reaches a terminal state
the notifier builds a chat line, sends it, and then decides whether to spend a
model turn waking the agent that created the card. Three separate patches used
to rewrite that path — a clip, a result delivery, and a wake filter — each with
its own applier anchored into the same function, each its own way for a
base-image bump to break the build for a reason that has nothing to do with the
other two. They are merged here: two anchors, one applier, one verifier.

The three concerns, in the order the notifier reaches them:

1. **Clip** the status line. ``clip_handoff`` is re-exported from
   ``gateway/kanban_handoff_clip.py``, which stays a module of its own because
   ``tools/cron_run_scope.py`` imports it too and needs it earlier in the
   build. The *wiring* of it into the notifier lives here.
2. **Deliver** the report: :func:`handoff_with_result` puts the card's
   ``result`` into the message the notifier already builds.
3. **Wake**, or not: :func:`wake_kinds_for` decides which terminal kinds are
   worth a model turn now that steps 1 and 2 have made the message carry the
   answer.
4. **Record** what step 3 chose not to announce:
   :func:`note_suppressed_completion` writes a one-shot marker into the
   creator's session state so the *next* turn knows the card finished, without
   spending a turn to find out.

Step 3 is only defensible because steps 1 and 2 happened, which is the clearest
argument for keeping them together: ``kanban.wake_on_events`` may drop
``completed`` on the grounds that the answer is already in the thread, and it is
this module that put it there. Step 4 is the other half of that bargain — the
answer being in the thread is a fact about the *user's* screen, not about the
agent's transcript, and step 3 is only safe once something has told the agent.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Iterable, List, Optional, Tuple

try:  # in-image: both modules live in the gateway package
    from gateway.kanban_handoff_clip import DEFAULT_LIMIT, ELLIPSIS, clip_handoff
except ImportError:  # host-side unit tests: siblings in deploy/docker/patches
    from kanban_handoff_clip import DEFAULT_LIMIT, ELLIPSIS, clip_handoff

logger = logging.getLogger("gateway.run")

__all__ = [
    "DEFAULT_LIMIT",
    "ELLIPSIS",
    "clip_handoff",
    "RESULT_LIMIT",
    "SEPARATOR",
    "result_block",
    "handoff_with_result",
    "DEFAULT_WAKE_KINDS",
    "CONFIG_KEY",
    "resolve_wake_kinds",
    "wake_kinds_for",
    "NOTE_SIGNATURE",
    "NOTE_TITLE_LIMIT",
    "MAX_NOTES",
    "suppressed_kinds",
    "completion_note",
    "creator_session_key",
    "stage_note",
    "note_suppressed_completion",
]


# ---------------------------------------------------------------------------
# 2. Delivering the card's ``result`` into the chat that asked for it
# ---------------------------------------------------------------------------
#
# The companion patch ``tools/kanban_result_required.py`` makes sure a card
# closes with its answer in ``result``. That makes the answer *durable* — it is
# on the card and ``kanban_show`` returns it whole. It does not make it
# *arrive*, because nothing upstream posts ``result`` to chat.
#
# The chat line for a completed card is built from ``ev.payload["summary"]``,
# which the kernel writes as (``hermes_cli/kanban_db.py``)::
#
#     ev_summary = (summary if summary is not None else result) or ""
#     ev_summary = ev_summary.strip().splitlines()[0][:400] if ev_summary else ""
#
# One line, 400 characters. The summary channel is therefore not merely the
# wrong place for a report — it is structurally incapable of carrying one. A
# worker that does exactly what the schema now asks (status line in ``summary``,
# the deliverable in ``result``) would still send a chat message announcing a
# catalogue it never shows.
#
# This section supplies the missing text. It goes into **the completion message
# the notifier already builds**, rather than a second message, and that is
# deliberate: the existing send site is wrapped in the notifier's failure
# counter, cursor rewind, and subscription-drop logic
# (``gateway/kanban_watchers.py``). One message inherits all of it. A follow-up
# ``adapter.send()`` would sit outside that machinery, after the cursor has
# advanced, and would need its own — a second failure path guarding the payload
# that matters most.
#
# The notifier's own clip gives way
# ---------------------------------
# :func:`handoff_with_result` replaces the notifier's ``handoff`` rather than
# appending to it, and it has to. Where the completion event carries no
# ``summary``, ``kanban_watchers.py`` builds the status line out of the very
# field this code exists to deliver::
#
#     elif task and task.result:
#         r = _clip_handoff(task.result)
#         handoff = f"\n{r}"
#
# ``delivered`` is then a 1200-character clip of ``result``, so asking whether
# ``result`` already appears inside it — the containment test
# :func:`result_block` does — is asking whether a report fits inside its own
# prefix. Under ``kanban_handoff_clip.DEFAULT_LIMIT`` it does, and the block
# correctly stays empty. Over it, it never does: the message went out carrying
# the first 1200 characters of the report, the ``[…]`` marker, a blank line,
# and then the same report over again from the top. Measured on a 60-line cron
# catalogue, jobs 1 to 19 arrived twice. Every result long enough to need this
# code at all was delivered doubled, because ``RESULT_LIMIT`` is 30000 precisely
# for reports that outgrow a status line.
#
# Appending cannot fix that — only the caller of the clip can decide the clip
# was a mistake — so the hook returns the finished tail instead. When the status
# line is merely a clipped prefix of the report, it is dropped and the report is
# sent once, whole. That branch got *more* reachable, not less, when
# ``tools/kanban_result_required.py`` began folding a whitespace-only
# ``summary`` to ``None`` to stop ``complete_task`` indexing line zero of a
# blank string and wedging the card.
#
# Length is safe on both platforms this harness ships to. The notifier calls
# ``adapter.send()`` directly, and ``send()`` chunks: the Slack adapter declares
# ``splits_long_messages = True`` with ``MAX_MESSAGE_LENGTH = 39000`` and splits
# on code-block boundaries; the bundled ``google_chat`` adapter chunks at 4000.
# ``RESULT_LIMIT`` is well inside the smaller of those headrooms once the status
# line and title are accounted for, and exists to bound a worker that dumps a
# log rather than to fit a single message.

#: How much of ``result`` reaches chat. The status line's own budget is 1200
#: (``kanban_handoff_clip.DEFAULT_LIMIT``) because it is a status line; this is
#: the report and needs room. Sized far above the ~5.5 KB catalogue that card
#: t_8d1cf5cf should have delivered, and far enough below the 39000-character
#: Slack ceiling that the status line, the title, and the clip marker all fit
#: alongside it.
RESULT_LIMIT = 30000

#: Separates the status line from the report. A blank line is enough: the
#: status line is already on its own line under the ``✔ … done — <title>``
#: header, so the result reads as the body of the same message.
SEPARATOR = "\n\n"

CLIPPED_TAIL = (
    "\n\n[Result clipped at {limit} characters — ask for the full card "
    "to see the rest.]"
)


def _normalise(text: str) -> str:
    """Collapse whitespace and case, so two renderings of one report compare equal."""
    return " ".join(text.split()).casefold()


def result_block(
    delivered: object,
    result: object,
    limit: int = RESULT_LIMIT,
) -> str:
    """Return the text to append to a completion message, or ``""`` for none.

    ``delivered`` is the handoff the message already carries (the clipped
    status line). When the result is contained in it there is nothing new to
    say and a second copy is noise — which is exactly what happens when a
    worker puts one body of text in both fields, or when the require-result
    gate promoted ``summary`` into ``result`` to let a card close.
    """
    if result is None:
        return ""
    body = str(result).strip()
    if not body:
        return ""
    normalised = _normalise(body)
    if delivered and normalised in _normalise(str(delivered)):
        return ""
    clipped = clip_handoff(body, limit)
    if len(clipped) < len(body):
        return SEPARATOR + clipped + CLIPPED_TAIL.format(limit=limit)
    return SEPARATOR + clipped


def _is_clipped_prefix_of(delivered: str, body: str) -> bool:
    """Whether ``delivered`` is just the opening of ``body``, possibly clipped.

    Written as a prefix test rather than an equality test against
    ``clip_handoff(body)`` so it still holds if the notifier's status line is
    built some other way. Upstream's own version of that line was a raw
    ``lines[0][:160]`` slice before the clip wiring replaced it, and either
    shape is the same fact about the message: the reader has seen this text
    already, and is about to see all of it.
    """
    head = _normalise(delivered)
    marker = _normalise(ELLIPSIS)
    if marker and head.endswith(marker):
        head = head[: -len(marker)].rstrip()
    return bool(head) and _normalise(body).startswith(head)


def handoff_with_result(delivered: object, task: object) -> str:
    """Return the completion message's whole tail: status line and report.

    Replaces the notifier's ``handoff`` — see the comment block above for why
    appending to it cannot work. ``delivered`` is what the notifier built,
    ``task`` is whatever ``_kb.get_task`` returned, which is ``None`` for a row
    that vanished between the claim and the send.

    Fails to ``delivered`` unchanged rather than raising. This runs on the
    delivery path: a completion notification that loses its report is bad, one
    that raises, rewinds the cursor and re-sends forever is worse, and one that
    drops the status line it already had is worse again.
    """
    text = "" if delivered is None else str(delivered)
    try:
        result = getattr(task, "result", None)
        block = result_block(text, result)
        if not block:
            return text
        if _is_clipped_prefix_of(text, str(result).strip()):
            return block
        return text + block
    except Exception:  # pragma: no cover - defensive
        return text


# ---------------------------------------------------------------------------
# 3. Making the agent wake configurable
# ---------------------------------------------------------------------------
#
# When a card reaches a terminal state the notifier does two separate things
# for the same event:
#
# 1. ``adapter.send(...)`` posts the completion line — the worker's own summary,
#    plus whatever :func:`handoff_with_result` added to it — straight into the
#    originating chat thread. The user has the answer at this point.
# 2. ``adapter.handle_message(...)`` then injects a synthetic ``MessageEvent``
#    to *wake the agent that created the card*, which costs a full model turn.
#
# Upstream hardcodes which event kinds trigger step 2::
#
#     _WAKE_KINDS = ("completed", "gave_up", "crashed", "timed_out", "blocked")
#
# There is no config key for it anywhere in Hermes. For the Chat Agent front
# door that makes ``completed`` pure overhead: the summary has already been
# delivered, so the woken turn re-reads the card with ``kanban_show`` and
# paraphrases a message the user is already looking at. Measured on task
# ``t_c31a1f00`` (2026-08-05): **5.9 s and 32,460 input tokens** for that third
# hop, on a request whose actual work was a single 477 ms ``list_clusters``
# call.
#
# The failure kinds are a different matter. ``gave_up`` / ``crashed`` /
# ``timed_out`` / ``blocked`` produce a terse status line and nothing else, and
# the front door genuinely should react — retry, escalate, or tell the user what
# broke. So this is not "turn the wake off", it is "wake for the events that
# need a decision, not for the one that already answered itself".
#
# :func:`resolve_wake_kinds` reads ``kanban.wake_on_events`` from config and
# falls back to the upstream tuple, so an image built without that key set
# behaves exactly as upstream does.
#
# All of that reasoning is conditional on step 1 having happened, which is why
# :func:`wake_kinds_for` takes the adapter and leaves a non-push one alone:
# where the notifier skips the send, the wake is not a third hop over a
# delivered answer, it is the only delivery there is.

#: The upstream hardcoded set, and the fallback for any config that does not
#: say otherwise. Also the whitelist: a kind outside this set can never match
#: ``ev.kind`` for a terminal event, so allowing it through would only hide a
#: typo.
DEFAULT_WAKE_KINDS: Tuple[str, ...] = (
    "completed",
    "gave_up",
    "crashed",
    "timed_out",
    "blocked",
)

CONFIG_KEY = "wake_on_events"

#: Reasons :func:`_load_kanban_config` has already reported this process.
#: The notifier reaches it on every delivery, so a config that is permanently
#: unreadable would otherwise warn every five seconds for the life of the
#: gateway; one line per distinct cause is enough to explain the behaviour.
#: Tests clear it between cases.
_warned_config: set = set()


def _warn_config_once(cause: str, message: str, *args: object) -> None:
    if cause in _warned_config:
        return
    _warned_config.add(cause)
    logger.warning(message, *args)


def _load_kanban_config(load_config: Optional[Callable[[], object]]) -> Optional[dict]:
    """Return the ``kanban`` config subtree, or None if it cannot be read.

    Returning None sends :func:`resolve_wake_kinds` back to
    :data:`DEFAULT_WAKE_KINDS`, which is the safe answer but an invisible one:
    it is byte-for-byte what an operator who never set ``kanban.wake_on_events``
    gets, so a loader that has genuinely broken presents as a key that was
    never configured, and the redundant turn comes back with nothing in the
    logs. Each failure therefore says so once before degrading.
    """
    if load_config is None:
        try:
            from hermes_cli.config import load_config as _lc
        except Exception as exc:
            _warn_config_once(
                "import",
                "kanban notifier: hermes_cli.config is not importable (%s); "
                "kanban.%s cannot be read and the upstream wake set applies to "
                "every card",
                exc,
                CONFIG_KEY,
            )
            return None
        load_config = _lc
    try:
        cfg = load_config()
    except Exception as exc:
        _warn_config_once(
            "read",
            "kanban notifier: reading the Hermes config failed (%s); "
            "kanban.%s is being ignored and the upstream wake set applies",
            exc,
            CONFIG_KEY,
        )
        return None
    if not isinstance(cfg, dict):
        _warn_config_once(
            "shape",
            "kanban notifier: load_config() returned %s rather than a mapping; "
            "kanban.%s is being ignored and the upstream wake set applies",
            type(cfg).__name__,
            CONFIG_KEY,
        )
        return None
    kcfg = cfg.get("kanban", {})
    return kcfg if isinstance(kcfg, dict) else {}


def resolve_wake_kinds(
    load_config: Optional[Callable[[], object]] = None,
) -> Tuple[str, ...]:
    """Return the event kinds that should wake the card's creator.

    Read fresh on every delivery rather than captured at gateway boot, so
    changing ``kanban.wake_on_events`` takes effect on the next tick instead of
    requiring a restart. The config read is cheap: ``load_config()`` is
    mtime-cached upstream.

    Fails **towards upstream behaviour**, loudly. A missing key, an unreadable
    config, or a value of the wrong shape all yield :data:`DEFAULT_WAKE_KINDS`
    — a transient read error must not stop waking an agent on a crash — but
    every case except the missing key logs its reason first, so a degraded
    read is distinguishable from a key nobody set.
    Only an explicit, well-formed value narrows the set; an explicit empty list
    disables the wake entirely, which is a deliberate choice a user can make.
    """
    kcfg = _load_kanban_config(load_config)
    if kcfg is None or CONFIG_KEY not in kcfg:
        return DEFAULT_WAKE_KINDS

    raw = kcfg.get(CONFIG_KEY)
    if raw is None:
        # `wake_on_events:` with nothing after it parses as None. Read that as
        # "no wake", matching the explicit empty list rather than falling back
        # to the default the user was plainly trying to override.
        return ()
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple, set, frozenset)):
        logger.warning(
            "kanban notifier: kanban.%s must be a list of event kinds, got %r; "
            "using the default wake set",
            CONFIG_KEY,
            type(raw).__name__,
        )
        return DEFAULT_WAKE_KINDS

    kinds: list[str] = []
    unknown: list[str] = []
    for item in raw:
        kind = str(item).strip()
        if not kind:
            continue
        if kind not in DEFAULT_WAKE_KINDS:
            unknown.append(kind)
            continue
        if kind not in kinds:
            kinds.append(kind)
    if unknown:
        # Loud, because the failure mode is silent: an unknown kind never
        # matches a real event, so a typo reads as "the wake just stopped
        # working" with nothing in the logs to explain it.
        logger.warning(
            "kanban notifier: ignoring unknown kanban.%s value(s) %s; "
            "valid kinds are %s",
            CONFIG_KEY,
            ", ".join(sorted(unknown)),
            ", ".join(DEFAULT_WAKE_KINDS),
        )
    return tuple(kinds)


def _adapter_can_push(adapter: object) -> bool:
    """Whether *adapter* has a push channel.

    Defers to ``gateway.wake.adapter_supports_push`` so this stays correct if
    upstream ever makes the capability something richer than one attribute. That
    module is not importable outside the image, so the fallback re-states its
    current one-line contract rather than guessing: an adapter that does not
    declare the flag is push-capable.
    """
    try:
        from gateway.wake import adapter_supports_push
    except Exception:
        # Silent on purpose: outside the image this import is *expected* to
        # fail, so warning here would fire on every host-side unit test while
        # saying nothing about the deployed gateway. That the real module is
        # reachable in the image is asserted by verify_kanban_notifier.py,
        # which drives this against the actual APIServerAdapter.
        return bool(getattr(adapter, "supports_async_delivery", True))
    try:
        return bool(adapter_supports_push(adapter))
    except Exception:
        logger.warning(
            "kanban notifier: adapter_supports_push(%s) raised; treating it as "
            "push-capable and applying kanban.%s as configured",
            type(adapter).__name__,
            CONFIG_KEY,
        )
        return True


def wake_kinds_for(
    events: Iterable[object],
    load_config: Optional[Callable[[], object]] = None,
    adapter: object = None,
) -> set:
    """Return the subset of ``events``' kinds that should wake the creator.

    Mirrors the upstream expression it replaces::

        {ev.kind for ev in d["events"] if ev.kind in _WAKE_KINDS}

    ``adapter`` opts a non-push adapter out of the narrowing entirely, and
    passing it is not optional in the notifier. The whole argument for dropping
    ``completed`` is that ``adapter.send()`` already put the worker's summary in
    the thread, so the wake is a third hop over an answer the user is looking
    at. On an adapter with no push channel — the API server, whose ``send()``
    returns ``SendResult(success=False)`` by design — the notifier skips that
    send and says so in its own comment: *"the wake self-post below IS the
    delivery"*. Narrow the set there and a card that completes successfully is
    never announced to anyone; upstream added that self-post to fix the
    api_server wrong-session bug, and dropping ``completed`` re-breaks it.

    So the config key governs the push path only. On the non-push path the full
    upstream set always applies, including an explicit ``wake_on_events: []``:
    that key means "do not spend a turn re-reading an answer already
    delivered", which is not a thing anyone can be asking for where nothing was
    delivered.
    """
    allowed = resolve_wake_kinds(load_config)
    if adapter is not None and not _adapter_can_push(adapter):
        allowed = DEFAULT_WAKE_KINDS
    return {ev.kind for ev in events if getattr(ev, "kind", None) in allowed}


# ---------------------------------------------------------------------------
# 4. Recording the completion the wake no longer announces
# ---------------------------------------------------------------------------
#
# Section 3 saves a model turn by not waking the creator for ``completed``. The
# saving is real and this section does not give it back. What section 3 did not
# account for is that the wake was doing two jobs at once, and only one of them
# was redundant:
#
# * it *told the user* — redundant, because ``adapter.send()`` already put the
#   whole report in the thread;
# * it *told the agent* — not redundant at all, because the synthetic
#   ``MessageEvent`` was the only thing that ever entered the creator's
#   transcript to say the card had finished.
#
# Dropping the wake dropped both. The creator's session is left with
# ``kanban_create → subscribed: true`` as the last thing it knows, and no event
# of any kind afterwards, so on its next turn the front door does not conclude
# "the card is done" — it concludes "the card is still running", because that
# is what its context says.
#
# Observed end to end on task ``t_a8f58a2a`` (2026-08-08):
#
#   19:08:01  front door files the card, subscribed: true
#   19:09:43  worker completes; 6,191-character report
#   19:09:44  notifier delivers the report to the Slack thread — succeeded
#   19:11:30  user follows up. The front door calls ``kanban_comment`` (not
#             ``kanban_show``) and promises "You'll see the results post here
#             as soon as the agent completes". They had posted 106 s earlier.
#   19:20:59  user asks whether it is still running
#   19:21:12  front door finally calls ``kanban_show``, sees ``done``, pastes
#             the answer it had had all along.
#
# Nine minutes forty-six seconds of dead wait, caused by an absence rather than
# an error — which is why nothing in the logs or the card looked wrong.
#
# The marker, and why it is free
# ------------------------------
# The gateway already has a channel for exactly this: per-turn "must-deliver
# notes". ``GatewayRunner._set_pending_turn_sidecar_notes(session_key, notes)``
# parks strings on ``SessionState.conversation.sidecar_notes``; the next turn's
# agent setup drains them into ``agent._gateway_turn_context_notes``
# (``gateway/run.py``), and ``agent/turn_context.py`` appends them to *that
# turn's user message* through the ``api_content`` sidecar. Upstream uses it for
# the auto-reset notice, the first-contact intro, and Discord voice-channel
# changes.
#
# It is the right channel here for three reasons:
#
# 1. **It costs no turn.** The note is not delivered *to* the model, it is
#    waiting *for* the model, and it rides a user message that was going to be
#    sent anyway. Where the wake cost 5.9 s and 32,460 input tokens (task
#    ``t_c31a1f00``), this costs about seventy tokens on one turn.
# 2. **It is one-shot.** ``_consume_pending_turn_sidecar_notes`` clears the
#    list as it reads it, and ``consume_gateway_turn_context_notes`` clears the
#    agent-side copy again, so a completion is announced to the agent exactly
#    once and never replays out of a cached agent.
# 3. **It rides the user message, not the system prompt.** Upstream moved these
#    notes off the ephemeral system prompt deliberately: a note in the system
#    prompt guarantees a turn-1/turn-2 prompt diff, which forces a full agent
#    rebuild and re-keys the prompt cache. Writing the marker into
#    ``ConversationState.ephemeral_pin`` — the other per-session string that
#    reaches the model — would therefore have cost *more* than the wake it is
#    saving, on top of being a cache slot that the next key miss overwrites.
#
# Not the transcript
# ------------------
# ``session_store.append_to_transcript`` would be durable across a gateway
# restart, which the marker is not. It was rejected anyway: a transcript row is
# replayed on *every* later turn rather than one, it inserts a message with no
# matching assistant turn into a history other code assumes alternates, and it
# is a SQLite write on the notifier's poll loop. Durability is not worth those;
# a marker lost to a restart just restores today's behaviour.
#
# Independent of the subscription
# -------------------------------
# The note has to outlive the thing that triggered it. On a terminal event the
# notifier calls ``self._kanban_unsub(sub, board_slug)`` and the subscription
# row is deleted from the kanban database moments later. The marker never
# touches that row: it lives in the gateway's in-memory
# ``SessionState.conversation`` for a session key resolved from the session
# store. Unsubscribing cannot reach it.
#
# Which completions get one
# -------------------------
# Exactly the ones the narrowing suppressed: the terminal kinds upstream would
# have woken for, minus the ones this delivery is waking for. A kind that still
# wakes needs no marker (the wake enters the transcript itself), and a gateway
# with ``kanban.wake_on_events`` unset suppresses nothing, so it writes no notes
# and behaves exactly as before. That makes the existing config key the on
# switch and means this section needs no key of its own.

#: Opening of every note this module writes. Doubles as the marker that tells
#: :func:`stage_note` which of a session's staged notes are ours, so trimming
#: to :data:`MAX_NOTES` can never evict upstream's auto-reset notice or
#: first-contact intro.
NOTE_SIGNATURE = "[System note: Kanban card "

#: Card titles are user-supplied and can be a paragraph. Clipped with
#: :func:`clip_handoff` rather than sliced, for the same reason the status line
#: is: a title ending in a severed URL is worse than a title ending early.
NOTE_TITLE_LIMIT = 120

#: How many of *our* notes a session may accumulate before the oldest is
#: dropped. Reached only when several cards finish between two user messages;
#: the alternative is an unbounded list appended to the next user message.
MAX_NOTES = 8


def suppressed_kinds(events: Iterable[object], wake_kinds: object) -> set:
    """Terminal kinds that fired but will not wake the creator.

    Computed as "what upstream's hardcoded tuple would have woken for" minus
    "what this delivery is actually waking for", which needs no second config
    read and stays correct however :func:`resolve_wake_kinds` narrowed the set
    — including the non-push carve-out, where nothing is narrowed and this
    returns the empty set.
    """
    try:
        woken = set(wake_kinds or ())
    except TypeError:  # pragma: no cover - defensive
        woken = set()
    fired = set()
    for ev in events or ():
        kind = getattr(ev, "kind", None)
        if kind in DEFAULT_WAKE_KINDS:
            fired.add(kind)
    return fired - woken


def _utc_stamp(now: Optional[float] = None) -> str:
    """``2026-08-08 19:09:44 UTC``, or ``""`` if the clock cannot be read."""
    try:
        ts = time.time() if now is None else float(now)
        return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))
    except Exception:  # pragma: no cover - defensive
        return ""


def _task_id(task: object, sub: object) -> str:
    """The card id, preferring the subscription row the notifier is iterating.

    ``sub["task_id"]`` is what every log line and chat message in this delivery
    already used, so the marker names the card by the same id even if the task
    row went missing between the claim and the send.
    """
    if isinstance(sub, dict):
        tid = str(sub.get("task_id") or "").strip()
        if tid:
            return tid
    return str(getattr(task, "id", "") or "").strip()


def completion_note(
    task_id: str,
    title: object = "",
    status: object = "",
    kinds: Iterable[str] = (),
    board: object = "",
    now: Optional[float] = None,
) -> str:
    """Render the marker the creator's next turn will read.

    Written to defeat one specific wrong inference. The front door's context
    ends at ``subscribed: true``, so its default guess is "still running"; the
    note therefore states the outcome, states that the result has already been
    delivered (so it does not re-post it), states *why* there is no record of
    the finish earlier in the transcript (so the absence is not read as
    evidence), and says what to call if it wants the content back.
    """
    tid = str(task_id or "").strip() or "(unknown)"
    head = NOTE_SIGNATURE + tid
    clipped_title = clip_handoff(title, NOTE_TITLE_LIMIT)
    if clipped_title:
        head += f' ("{clipped_title}")'
    fired = ", ".join(sorted(str(k) for k in kinds if k)) or "finished"
    head += f" fired {fired}"
    stamp = _utc_stamp(now)
    if stamp:
        head += f" at {stamp}"
    card_status = str(status or "").strip()
    if card_status:
        head += f"; the card's status is now {card_status}"
    where = ""
    board_name = str(board or "").strip()
    if board_name:
        where = f" (board {board_name})"
    return (
        head + ". Its result was already delivered to this conversation, so no "
        "agent turn was spent announcing it and nothing about the finish "
        "appears earlier in this transcript. The card is NOT still running — "
        f"read it back with kanban_show{where} if you need the content again.]"
    )


def creator_session_key(runner: object, task: object) -> str:
    """Resolve the gateway session key of the agent that created the card.

    Two different identifiers are in play and conflating them is the whole
    difficulty. ``task.session_id`` is the *persisted session id*
    (``20260808_190725_6714054d``) — it is what the notifier hands
    ``deliver_wake`` — whereas per-turn state is keyed by the *session key*
    (``agent:main:slack:dm:T0BLH2UB516:D0BKGRBM6RH:1786216044.637229``), which
    is what ``_set_pending_turn_sidecar_notes`` and the next turn's
    ``ctx.session_key`` both use. ``SessionStore.lookup_by_session_id`` is
    upstream's public, lock-held accessor for exactly that mapping and returns
    a ``SessionEntry`` carrying ``session_key``.

    Deriving the key instead from the subscription row — the way the push wake
    path builds its ``SessionSource`` — was rejected. That derivation is
    allowed to be approximate because ``handle_message`` get-or-creates the
    target, so a near miss costs the wake a fresh session and nothing else;
    here a near miss parks the note on a key nobody will ever read, which is a
    silent no-op. The store lookup either finds the exact key the creator's
    turns run under or finds nothing, and finding nothing is reported.

    Returns ``""`` when the key cannot be resolved, which the caller treats as
    "no marker" — the pre-existing behaviour, not a failure.
    """
    session_id = str(getattr(task, "session_id", "") or "").strip()
    if not session_id:
        # Cards filed outside a gateway session (cron, CLI) have no creator to
        # tell. Ordinary, so debug.
        logger.debug(
            "kanban notifier: card has no creator session id; no completion "
            "marker to write"
        )
        return ""
    store = getattr(runner, "session_store", None)
    lookup = getattr(store, "lookup_by_session_id", None)
    if not callable(lookup):
        _warn_config_once(
            "session-store",
            "kanban notifier: the gateway session store has no "
            "lookup_by_session_id(); a suppressed completion cannot be "
            "recorded on the creator's session and the next turn will not "
            "know the card finished",
        )
        return ""
    entry = lookup(session_id)
    if entry is None:
        # The session rotated (compression, /new, expiry) or was never a
        # gateway session. Its conversation has moved on, so there is nothing
        # this marker would usefully be attached to.
        logger.debug(
            "kanban notifier: no live session is bound to creator session id "
            "%s; skipping the completion marker",
            session_id,
        )
        return ""
    return str(getattr(entry, "session_key", "") or "").strip()


def _staged_notes(runner: object, session_key: str) -> List[str]:
    """The notes already parked on this session, or ``[]``.

    Read through ``_peek_session_state`` rather than ``_session_state`` so
    merely inspecting a session cannot conjure a ``SessionState`` for it; the
    setter creates one when there is genuinely something to store.
    """
    peek = getattr(runner, "_peek_session_state", None)
    if not callable(peek):
        return []
    conversation = getattr(peek(session_key), "conversation", None)
    staged = getattr(conversation, "sidecar_notes", None)
    if not isinstance(staged, list):
        return []
    return [note for note in staged if isinstance(note, str)]


def stage_note(runner: object, session_key: str, task_id: str, note: str) -> bool:
    """Park ``note`` on the creator's session. True when it was added.

    Appends rather than assigns. ``_set_pending_turn_sidecar_notes`` replaces
    the whole list, and the list is shared with upstream's own notes — an
    auto-reset notice staged by a turn starting in the same instant would be
    overwritten by a blind assignment, and that notice tells the agent its
    history is gone.

    Our notes are trimmed to :data:`MAX_NOTES` and upstream's are never
    trimmed, which is why :data:`NOTE_SIGNATURE` has to be recognisable.
    """
    setter = getattr(runner, "_set_pending_turn_sidecar_notes", None)
    if not callable(setter):
        _warn_config_once(
            "sidecar",
            "kanban notifier: the gateway has no "
            "_set_pending_turn_sidecar_notes(); suppressed completions cannot "
            "be recorded and the creator's next turn will not know the card "
            "finished",
        )
        return False
    staged = _staged_notes(runner, session_key)
    # The id plus its trailing space: without the space, card ``t_a8`` would
    # suppress the marker for ``t_a8f58a2a``.
    already = NOTE_SIGNATURE + str(task_id) + " "
    if any(existing.startswith(already) for existing in staged):
        return False
    ours = [n for n in staged if n.startswith(NOTE_SIGNATURE)]
    others = [n for n in staged if not n.startswith(NOTE_SIGNATURE)]
    ours.append(note)
    setter(session_key, others + ours[-MAX_NOTES:])
    return True


def note_suppressed_completion(
    runner: object,
    events: Iterable[object],
    wake_kinds: object,
    task: object = None,
    sub: object = None,
    board: object = "",
    now: Optional[float] = None,
) -> bool:
    """Record a terminal event that :func:`wake_kinds_for` chose not to wake for.

    Called from ``gateway/kanban_watchers.py`` immediately after the wake set
    is computed, on the path where every text ping for this delivery has
    already been sent — so "the result is in the thread", which is what the
    note asserts, is true by the time it is written.

    Returns whether a note was staged; the notifier ignores it, but it makes
    the behaviour testable and gives the verifier something that a silent
    no-op cannot fake.

    Fails towards the pre-existing behaviour, loudly. Nothing here is allowed
    to disturb the delivery path: the report reaching the user is the outcome
    that matters and this is an optimisation layered on top of it, so every
    failure degrades to "no marker" — the exact state this code was written to
    improve on — rather than raising into a loop that would rewind the cursor
    and re-send the report.
    """
    task_id = ""
    try:
        task_id = _task_id(task, sub)
        kinds = suppressed_kinds(events, wake_kinds)
        if not kinds:
            return False
        session_key = creator_session_key(runner, task)
        if not session_key:
            return False
        note = completion_note(
            task_id,
            title=getattr(task, "title", "") or "",
            status=getattr(task, "status", "") or "",
            kinds=kinds,
            board=board,
            now=now,
        )
        if not stage_note(runner, session_key, task_id, note):
            return False
        # Info, matching the "woke agent for %s" line this path replaces: on a
        # gateway with the narrowing configured it is the only evidence that
        # the creator was told anything at all.
        logger.info(
            "kanban notifier: recorded %s for %s on the creator's session %s "
            "instead of spending a wake turn",
            ", ".join(sorted(kinds)),
            task_id or "(unknown card)",
            session_key,
        )
        return True
    except Exception:
        logger.warning(
            "kanban notifier: could not record the suppressed completion of "
            "%s on the creator's session; the report was still delivered, but "
            "the creator's next turn will not know the card finished",
            task_id or "(unknown card)",
            exc_info=True,
        )
        return False
