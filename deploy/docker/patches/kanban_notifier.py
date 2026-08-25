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

The concerns, in the order the notifier reaches them:

1. **Clip** the status line. ``clip_handoff`` is re-exported from
   ``gateway/kanban_handoff_clip.py``, which stays a module of its own because
   ``tools/cron_run_scope.py`` imports it too and needs it earlier in the
   build. The *wiring* of it into the notifier lives here.
2. **Deliver** the report: :func:`handoff_with_result` puts the card's
   ``result`` into the message the notifier already builds, and
   :func:`unstructured_result` records in the log when that report will render
   flat in Slack for want of Markdown structure. The note changes nothing about
   what is sent — it exists because a flattened report raises no error to see.
3. **Wake**, or not: :func:`wake_kinds_for` decides which terminal kinds are
   worth a model turn now that steps 1 and 2 have made the message carry the
   answer.
4. **Record** what step 3 chose not to announce:
   :func:`note_suppressed_completion` writes a one-shot marker into the
   creator's session state so the *next* turn knows the card finished, without
   spending a turn to find out.
5. **Store** the report the user just read: :func:`store_incident_report` puts
   it in the ``incidents`` table, keyed on the thread it was posted in, so a
   reply of ``apply Option A`` reaches an agent that can see what Option A was
   — or, where the report proposed a single unlettered fix, so that a bare
   ``apply`` reaches one that can see which fix it authorises.

Step 3 is only defensible because steps 1 and 2 happened, which is the clearest
argument for keeping them together: ``kanban.wake_on_events`` may drop
``completed`` on the grounds that the answer is already in the thread, and it is
this module that put it there. Step 4 is the other half of that bargain — the
answer being in the thread is a fact about the *user's* screen, not about the
agent's transcript, and step 3 is only safe once something has told the agent.
Step 5 is the third: the answer being on the user's screen is what makes them
reply to it, and until the row is written that reply arrives stripped of
everything it refers to.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.request
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
    "UNSTRUCTURED_MIN_CHARS",
    "unstructured_result",
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
    "SESSION_KV_URL",
    "INCIDENT_TIMEOUT_SECONDS",
    "actionable_report",
    "store_incident_report",
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


#: Below this, a report has nothing to structure and a flat answer is correct.
#: A one-line "no drift found" is the common completion and must stay silent.
#:
#: Was 600 until 2026-08-08, on the assumption that the reports worth checking
#: were thousands of characters. That assumption cost us the two cards that
#: prompted this whole line of work: ``t_88cdceb1`` (240 chars) and
#: ``t_c60439af`` (189) both rendered badly and neither could ever be measured,
#: because the floor sat above them. 150 is a heading plus three bullets — the
#: smallest report that can have a shape to get wrong.
UNSTRUCTURED_MIN_CHARS = 150

#: Block-level Markdown — the only things Block Kit turns into structure. An
#: ATX heading becomes a ``header``, a pipe row a native ``table``, a thematic
#: break a ``divider``, a fence a ``rich_text_preformatted``. Deliberately
#: excludes bullets: ``t_3ba2166a`` was all bullets and still collapsed into
#: one undifferentiated ``rich_text`` block, so their presence proves nothing.
_BLOCK_MARKDOWN = re.compile(
    r"^ {0,3}(?:#{1,6} +\S|\|.*\||(?:-{3,}|\*{3,}|_{3,}) *$|```)",
    re.MULTILINE,
)

#: Positive evidence that a report *meant* to have structure and expressed it
#: in a way Slack cannot see. Without this second test the warning would fire
#: on any long stretch of legitimate prose, which is noise, not a defect.
_ASCII_STRUCTURE = re.compile(
    r"^ *(?:={2,}[^=\n]+={2,} *|\d+[.)] +[A-Z][A-Z0-9 _/&'\"-]{3,}) *$",
    re.MULTILINE,
)


def unstructured_result(result: object, min_chars: int = UNSTRUCTURED_MIN_CHARS) -> bool:
    """Whether a long ``result`` will flatten when Slack renders it.

    True only when all three hold: the report is long enough for structure to
    matter, it carries no block-level Markdown for Block Kit to render, and it
    shows an ASCII substitute for the structure it is missing (``=== Title ===``
    or an ALL-CAPS numbered section). Requiring the third keeps a long plain
    narrative — a perfectly good answer that simply has no sections — quiet.

    Purely an observation. Nothing downstream branches on it; the message goes
    out identically either way. It exists because the flattening is otherwise
    invisible: the renderer *succeeded* on ``t_3ba2166a``, returning three
    valid blocks for a report that should have produced nine, so there was no
    error anywhere to notice.
    """
    if result is None:
        return False
    body = str(result).strip()
    if len(body) < min_chars:
        return False
    if _BLOCK_MARKDOWN.search(body):
        return False
    return bool(_ASCII_STRUCTURE.search(body))


def _log_result_shape(task: object, result: object) -> None:
    """Log the shape of a report as it is delivered.

    Never raises — this is the delivery path, and a report that renders badly is
    a far smaller problem than one that is not sent at all. Nothing here can
    stop a delivery; by the time this runs the card is already complete and the
    only question is what the log should say about it.

    Two levels, because a log line that argues about taste trains its reader to
    ignore it. The two defects in ``SERIOUS_DEFECTS`` always render wrongly, so
    they warn and the message carries the edit to make; ``heading-without-prose``
    and ``unquoted-numerics`` are matters of taste and go to INFO, where they are
    still there for anyone reading back a bad report but wake nobody.

    The defect list lives in ``tools/kanban_report_format.py`` so the stanza
    stapled to a card at creation, the schema wording and this log cannot
    disagree about what "well-shaped" means. It is imported lazily and
    optionally: ``gateway`` importing ``tools`` at module scope would put a
    second package on the delivery path's import graph for the sake of a log
    line. When the import fails we still report the one defect this module can
    detect on its own, which happens to be a serious one.
    """
    try:
        body = str(result).strip() if result is not None else ""
        advice: dict = {}
        try:
            from tools.kanban_report_format import (
                DEFECT_ADVICE,
                SERIOUS_DEFECTS,
                result_shape_defects,
            )

            defects = result_shape_defects(result, min_chars=UNSTRUCTURED_MIN_CHARS)
            serious = tuple(d for d in defects if d in SERIOUS_DEFECTS)
            advice = DEFECT_ADVICE
        except Exception:
            defects = ("ascii-substitute",) if unstructured_result(result) else ()
            serious = defects
        if not defects:
            return
        if not serious:
            logger.info(
                "[kanban] card %s completed with a %d-character result with "
                "cosmetic formatting defects: %s.",
                getattr(task, "id", "<unknown>"),
                len(body),
                ", ".join(defects),
            )
            return
        edits = " ".join(filter(None, (advice.get(d, "") for d in serious)))
        logger.warning(
            "[kanban] card %s completed with a %d-character result whose "
            "formatting will not render well in chat: %s. %sThe contract is in "
            "the card body's report-format stanza and in agents/platform/SOUL.md "
            "§0 / agents/cluster/SOUL.md §6.",
            getattr(task, "id", "<unknown>"),
            len(body),
            ", ".join(serious),
            f"{edits} " if edits else "",
        )
    except Exception:  # pragma: no cover - defensive
        logger.debug("[kanban] result-shape check failed", exc_info=True)


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
        _log_result_shape(task, result)
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
    passive_delivered: bool = True,
) -> set:
    """Return the subset of ``events``' kinds that should wake the creator.

    Mirrors the upstream expression it replaces::

        {ev.kind for ev in d["events"] if ev.kind in _WAKE_KINDS}

    The narrowing has exactly one precondition, and both keyword arguments
    exist to test it: **a text ping carrying the worker's summary has already
    reached this conversation**. The whole argument for dropping ``completed``
    is that ``adapter.send()`` already put the report in the thread, so the
    wake is a third hop over an answer the user is looking at. Where no send
    happened, the wake *is* the delivery, and narrowing it away means a card
    that completes successfully is never announced to anyone.

    There are two ways for that send not to happen, and neither implies the
    other:

    ``adapter``
        A non-push adapter — the API server, whose ``send()`` returns
        ``SendResult(success=False)`` by design — has no channel to ping. The
        notifier skips the send and says so in its own comment: *"the wake
        self-post below IS the delivery"*. Upstream added that self-post to fix
        the api_server wrong-session bug, and dropping ``completed`` re-breaks
        it.

    ``passive_delivered``
        v2026.8.13's ``delivery_mode``. A ``wake``-only subscription suppresses
        the text ping on a *push* adapter too (``send_passive = mode != "wake"``
        in the notifier), so the push path acquired a second way to reach the
        wake with nothing yet delivered. ``adapter`` cannot see this — it is a
        property of the subscription, not of the platform — which is why it is
        a separate argument rather than a refinement of ``_adapter_can_push``.
        Pass the notifier's own ``send_passive``.

    Neither is optional in the notifier. So the config key governs the
    ping-then-wake path only, and on either no-send path the full upstream set
    always applies — including an explicit ``wake_on_events: []``. That key
    means "do not spend a turn re-reading an answer already delivered", which
    is not a thing anyone can be asking for where nothing was delivered.
    """
    allowed = resolve_wake_kinds(load_config)
    if not passive_delivered or (adapter is not None and not _adapter_can_push(adapter)):
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
    wake_configured: bool = True,
) -> bool:
    """Record a terminal event that :func:`wake_kinds_for` chose not to wake for.

    Called from ``gateway/kanban_watchers.py`` immediately after the wake set
    is computed, on the path where every text ping for this delivery has
    already been sent — so "the result is in the thread", which is what the
    note asserts, is true by the time it is written.

    ``wake_configured`` is the subscription's own answer to "should this ever
    wake anybody" — upstream's ``delivery_mode`` gate, ``mode in ("notify+wake",
    "wake")``. It is passed in rather than inferred because ``wake_kinds`` is
    empty in both cases and they mean opposite things: this function's whole
    claim is that *the narrowing* spent no model turn on a completion the
    subscriber wanted woken for, and a ``notify``-only subscriber never wanted
    one. Without the gate every completion on every notify-only card would stage
    a note taking credit for a wake that was never going to happen. Defaults to
    ``True`` so a caller that predates the mode keeps the old behaviour.

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
        if not wake_configured:
            return False
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


# ---------------------------------------------------------------------------
# 5. Storing the delivered report so a reply to it can be acted on
# ---------------------------------------------------------------------------
#
# An event-triage report ends in two or three labelled remediation options. The
# reader's next move is to pick one — ``apply Option A`` — and that reply is
# ordinary chat ingress on the front door, a profile with no cluster access, no
# memory of the card, and no idea what Option A was.
#
# The mechanism that closes the gap already exists and works.
# ``agents/platform/plugins/incident_context/`` is a ``pre_gateway_dispatch``
# hook: when a message arrives in a thread it recognises, it prepends the stored
# report to the user's words before the agent sees them. It finds the thread
# with ``GET /v1/incidents/by-thread`` against the ``incidents`` table.
#
# Nothing wrote that table on this path. The rows a triage reply needs came from
# ``platform_mcp_server.send_notification`` — the egress call the card delivery
# in section 2 replaced (#738) — so ``_lookup`` returned ``None``, the reply was
# passed through unrewritten, and the front door received the bare word
# ``apply``. Nothing unsafe followed, because the front door holds no GitOps
# write path and simply cannot act; the report template stopped inviting the
# reply instead, and this section is what lets it be invited again (#802).
#
# Why here
# --------
# This is where the three values the row needs sit together in one place. The
# subscription carries the destination — ``sub["chat_id"]`` and
# ``sub["thread_id"]``, already substituted from the alert's real chat route by
# ``tools/kanban_event_routing.py`` — and ``task.result`` is the text that was
# just posted to it. Anywhere earlier, the address is still the undeliverable
# ``api_server`` one; anywhere later, the subscription row has been deleted.
#
# After the send, which is also where ``relay_cron_report`` puts its own store
# (#731) and for the same reason: "the routing registration and the incident
# store happen after the send because both need the thread the send resolves".
# The notifier's thread is resolved a step earlier — it is the substituted
# ``sub`` address rather than a value the send returns — but the other half of
# that ordering holds identically here. A row written before the post claims a
# reader has a report that has not been sent yet, and on a delivery that then
# fails to send, claims it forever: ``INSERT OR IGNORE`` will not let the next
# report replace it.
#
# Over loopback HTTP rather than SQLite, unlike
# ``session_kv_server._store_incident_report``. That function writes the table
# directly and its docstring says why: it *is* the server that owns the
# database. The notifier runs in the same container — ``docker-entrypoint.sh``
# starts the server in the background and then execs the gateway — but it is a
# different process, so it goes through the same authenticated
# ``POST /v1/incidents`` that ``send_notification`` used.
#
# What goes in the row is the specialist's report, ``task.result``, and not a
# summary of it. The reply this exists to resolve names a letter — ``Option
# B`` — so the stored text has to be the one the reader was looking at when they
# picked it. That is a third answer to a question the table's writers already
# answer two ways: the relay stores the Chat Agent's composed message rather
# than the specialist's finding, and ``send_notification`` stores the
# notification body. Nothing reads a row expecting a particular one of the
# three, and both readers are written for the union — ``incident_context``
# fences whatever it gets as untrusted, and ``list_recent_reports`` returns no
# preview text at all.
#
# Which cards get a row
# ---------------------
# Only the ones carrying something to act on: a ``What to do`` section holding
# either a lettered ``Option`` or the ``To authorize:`` call to action. That is
# not a proxy for "was this event triage" — it is the question itself. The row
# exists so ``apply Option A`` can be resolved, and a card whose result is a
# one-line status has neither an Option A nor anything to authorize.
#
# Two patterns rather than one because the template asks for two shapes. A
# report proposing a single fix does not letter it — a lone "Option A" reads as
# a list of one — so it carries no lettered option at all, and gating on the
# letter alone would deny a row to exactly the reports whose reply is the
# simplest, a bare ``apply``. The ``To authorize:`` bullet is the line both
# shapes end on, and it is the one this row serves: it is the sentence that
# invites the reply the row exists to resolve.
#
# Card metadata cannot answer it. By the time the notifier sees the
# subscription, ``kanban_event_routing`` has rewritten the row to look exactly
# like an ordinary chat card's, and the assignee and title are model-written.
# The artifact is the only reliable evidence, and it is the right evidence.
#
# Getting this wrong is sticky in one direction. ``POST /v1/incidents`` is
# ``INSERT OR IGNORE`` and deliberately keeps the FIRST report per thread — the
# one carrying the options — so a one-line status card stored in a thread that
# later receives a real report would shadow it for the table's whole retention
# window. Storing too little costs one reply; storing too much poisons the
# thread. The gate is set accordingly.
#
# Failing
# -------
# Never fatally: a report delivered and unstored is the behaviour this replaces,
# while an exception raised on the delivery path rewinds the notifier cursor and
# re-posts the report. But never silently either. A silent miss here is the
# same shape as the bug ``kanban_event_routing`` was written for — a board that
# reads healthy while the user gets a blank stare — and that patch settled it by
# logging a warning naming the session on every fall-through. This follows it:
# a report that had options and did not get stored says so, with the card and
# the thread named.

#: The Session KV server's loopback address, spelled as
#: ``platform_mcp_server.py`` and the ``incident_context`` plugin spell it. The
#: server binds 127.0.0.1 only, so this is reachable from the notifier's process
#: and from nowhere off the pod.
SESSION_KV_URL = "http://127.0.0.1:8699"

#: Matches the two other callers of this server. The notifier polls every five
#: seconds and this call happens after the report has already been sent, so a
#: slow server must cost the tick a moment, not the next delivery.
INCIDENT_TIMEOUT_SECONDS = 2.0

#: The report's ``What to do`` section, which is where the options live. Written
#: to tolerate the heading depth drifting (``##`` in the template, but §7 only
#: pins the words) while still requiring a heading rather than the phrase in
#: prose.
_WHAT_TO_DO_RE = re.compile(r"^ {0,3}#{1,6} +what to do\b", re.IGNORECASE | re.MULTILINE)

#: A lettered option — the thing ``apply Option A`` names. Case-sensitive on the
#: letter because the template says "Label them 'Option A', 'Option B', ... in
#: order", and a lowercase "option a" in prose is a sentence, not a label.
_OPTION_RE = re.compile(r"\bOption [A-Z]\b")

#: The call to action. The template's single-option shape has no lettered option
#: to match, and this bullet is what it ends on instead — see
#: ``session_kv_server._triage_task_body``, which owns both shapes. The colon is
#: load-bearing and the case is not incidental: this has to match a bullet's
#: label, not the phrase "to authorize the quota increase" in someone's prose,
#: which is a sentence rather than an offer to act on.
#:
#: ``[*_]*`` is what sits between the word and the colon, and it is the whole
#: reason this pattern is not simply ``To authorize:``. The template emits
#: ``**To authorize:**``, with the colon inside the emphasis — but a model
#: reproducing a ``**Label:**`` bullet will as readily close the emphasis first
#: and write ``**To authorize**:``, and italic and ``__``-bold do the same. Those
#: are the same bullet, and matching only the template's exact spelling fails
#: them silently: the single-option shape has no lettered option to fall back on,
#: so the report is delivered, no ``incidents`` row is written, and the ``apply``
#: it invites reaches the front door with nothing attached. That is the #802
#: failure, in the one shape this gate exists to cover.
#:
#: The lookbehind does the job ``\b`` would, and is here because ``\b`` cannot:
#: ``_`` is a word character, so ``\bTo`` finds no boundary in
#: ``__To authorize__:`` and the ``__``-bold spelling fails on the opening
#: marker rather than the closing one.
_AUTHORIZE_RE = re.compile(r"(?<![0-9A-Za-z])To authorize[*_]*:")


def actionable_report(result: object) -> bool:
    """Whether ``result`` is a report a reply could ask to act on.

    True only when both halves of the triage template's ``What to do`` section
    are present: the heading, and under it either a lettered option or the
    ``To authorize:`` call to action. Either half alone is not enough — a
    heading with neither offers nothing to apply, and the word "Option" in a
    paragraph is not a label.

    "Under it" is literal: the search starts at the end of the heading match,
    not at the top of the report. A ``What to do`` section holding only
    unlettered bullets, in a report whose ``Why`` quotes "Option A" from an
    earlier one, offers nothing to apply — and a row stored for it would hold
    the thread's one ``INSERT OR IGNORE`` slot against the report that does.
    """
    if result is None:
        return False
    body = str(result).strip()
    if not body:
        return False
    heading = _WHAT_TO_DO_RE.search(body)
    if not heading:
        return False
    return bool(
        _OPTION_RE.search(body, heading.end())
        or _AUTHORIZE_RE.search(body, heading.end())
    )


def _incident_address(sub: object) -> Tuple[str, str]:
    """The ``(chat_id, thread_id)`` the report was delivered to, or ``("", "")``.

    Both are required. ``chat_id`` alone keys nothing the by-thread lookup can
    find, and an unthreaded delivery is a message in the channel body that no
    reply will be attached to.
    """
    if not isinstance(sub, dict):
        return "", ""
    chat_id = str(sub.get("chat_id") or "").strip()
    thread_id = str(sub.get("thread_id") or "").strip()
    return chat_id, thread_id


def _post_incident(chat_id: str, thread_id: str, report: str) -> None:
    """POST one row to the Session KV server. Raises on any failure."""
    token = (os.environ.get("SESSION_KV_API_KEY") or "").strip()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        f"{SESSION_KV_URL}/v1/incidents",
        data=json.dumps(
            {"chat_id": chat_id, "thread_id": thread_id, "report": report}
        ).encode(),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=INCIDENT_TIMEOUT_SECONDS):
        pass


def store_incident_report(
    event: object, task: object, sub: object, posted: bool = True
) -> bool:
    """Key the delivered report to its chat thread. True when a row was posted.

    Called from ``gateway/kanban_watchers.py`` on the same path as
    :func:`note_suppressed_completion`, where every text ping for this delivery
    has already been sent — so the report this stores is one the reader has, and
    a reply to it is a reply to something.

    Takes the loop's *current* event, not the delivery's whole event list, which
    is where it parts company with :func:`note_suppressed_completion` at the
    same call site. That function is asking what the delivery as a whole
    suppressed, so it wants every kind; this one is asking whether *this* send
    was the report, and the call site runs once per event. Reading the list here
    would fire on a delivery's ``commented`` event too — writing the row before
    the ``completed`` iteration has sent the report it claims the reader has,
    and again on the iteration that did.

    ``posted`` is ``send_passive`` at the call site, and False means this
    subscription's ``delivery_mode`` is ``wake``: the agent is woken and the
    thread gets no message. There is nothing for the reader to reply to, so
    keying a report to that thread would prepend a report they never saw to
    their next message — and ``INSERT OR IGNORE`` would keep it there for the
    table's retention window.

    Returns whether the POST was made, which the notifier ignores; it exists so
    the behaviour is testable and so a silent no-op cannot pass for a write.

    Never raises. The delivery is the outcome that matters and this is a second
    write layered on top of it: an exception here would escape into the tick,
    rewind the cursor, and re-post the report the user has already read.
    """
    card = ""
    chat_id = thread_id = ""
    try:
        card = _task_id(task, sub) or "(unknown card)"
        if getattr(event, "kind", None) != "completed":
            # gave_up, crashed, timed_out: whatever is in `result` is not a
            # report with options in it, and the reader has nothing to apply.
            # commented, and every other mid-flight kind: the report has not
            # been sent yet, and the completed event later in this same
            # delivery is the iteration that stores it.
            return False
        if not posted:
            logger.debug(
                "kanban notifier: card %s completed on a wake-only "
                "subscription; nothing was posted in the thread, so there is "
                "no delivered report to store",
                card,
            )
            return False
        result = getattr(task, "result", None)
        if not actionable_report(result):
            # The common case — every status line, every "no drift found". Not
            # a warning: this is what most cards look like.
            logger.debug(
                "kanban notifier: card %s completed with nothing for a reply to "
                "act on; no incident row stored",
                card,
            )
            return False
        chat_id, thread_id = _incident_address(sub)
        if not (chat_id and thread_id):
            logger.warning(
                "kanban notifier: card %s delivered a report inviting a reply "
                "to chat %s thread %s, which is not a thread — that reply will "
                "reach an agent that cannot see the report",
                card,
                chat_id or "<no chat>",
                thread_id or "<no thread>",
            )
            return False
        # Clipped to what was delivered. `result_block` bounds the chat message
        # at the same limit, and storing more than the reader was shown would
        # let the agent answer about options that never reached them.
        _post_incident(chat_id, thread_id, clip_handoff(str(result).strip(), RESULT_LIMIT))
        # chat and thread are logged as separate fields rather than joined with
        # a slash. A Google Chat thread_id is already the fully qualified
        # `spaces/<space>/threads/<thread>`, so `%s/%s` printed the space twice
        # ("spaces/0sm.../spaces/0sm.../threads/IKT...") and read like a
        # malformed identifier the store had written. Slack's is a bare ts, and
        # would have read fine either way.
        logger.info(
            "kanban notifier: stored card %s's report against chat %s thread %s "
            "so a reply to it carries the options back",
            card,
            chat_id,
            thread_id,
        )
        return True
    except Exception:
        logger.warning(
            "kanban notifier: could not store card %s's report against chat %s "
            "thread %s; the report was delivered, but a reply naming an option "
            "will reach an agent that cannot see it",
            card or "(unknown card)",
            chat_id or "<no chat>",
            thread_id or "<no thread>",
            exc_info=True,
        )
        return False
