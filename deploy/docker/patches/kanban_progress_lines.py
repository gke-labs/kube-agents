"""Mid-run progress lines for the kanban notifier.

Installed into the image at ``/opt/hermes/gateway/kanban_progress_lines.py``
and wired into ``gateway/kanban_watchers.py`` by
``deploy/docker/patches/apply_kanban_progress_lines.py``.

**The problem.** A delegated card is silent from the moment it is claimed until
the moment it completes. Measured on the ``kage-management`` board over 298
runs, that silence is a p50 of 264 seconds and a p90 of 1102 seconds. The
plumbing around it costs about a minute; the silence is what makes delegation
feel slower than doing the work in the chat thread. The workaround the personas
used to prescribe — split the work into child cards so each completion posts a
line — buys visibility by paying a fresh dispatch tick, a fresh 17–19 second
worker cold start, and a fresh worker context per stage. It makes the real
number worse to improve the perceived one.

**The unlock.** ``kanban_heartbeat(note=...)`` already writes a ``heartbeat``
event carrying that note (``hermes_cli/kanban_db.py``); the notifier simply
does not deliver that kind. Two properties make delivering it nearly free:

1. The per-tool-call auto-heartbeats fired by ``tools/kanban_tools.py`` write
   ``payload=None``. All 2,107 heartbeat rows on the live board are noteless,
   so "has a note" separates a deliberate progress update from a liveness ping
   exactly — no new event kind and no schema change.
2. ``heartbeat`` is absent from ``_WAKE_KINDS`` in the notifier, so a progress
   line posts straight into the chat thread without waking the creator's agent.
   It costs zero LLM turns, which is why a worker can afford to send several.

``progress_note`` is the whole filter: it returns the note a human should see,
or ``""`` for everything else. The empty return is what keeps the auto-
heartbeats silent, so it is the single most important behaviour here.

Length is capped through ``clip_handoff`` rather than a hard slice, for the
same reason the completion handoff is: a note that ends in a link must not have
that link severed into a dead one. See ``kanban_handoff_clip.py``.
"""

from __future__ import annotations

try:  # In the image the patches live under the ``gateway`` package.
    from gateway.kanban_handoff_clip import clip_handoff
except ImportError:  # Unit tests import the patch modules flat.
    from kanban_handoff_clip import clip_handoff

# A progress line is a status ping, not the report. The completion handoff
# still carries the full result at DEFAULT_LIMIT (1200), and a worker that
# needs more room than this should be completing, not heartbeating.
DEFAULT_NOTE_LIMIT = 300


def progress_note(payload: object, limit: int = DEFAULT_NOTE_LIMIT) -> str:
    """Return the deliverable progress note on a heartbeat event's payload.

    Returns ``""`` — meaning "deliver nothing" — for a ``None`` payload, a
    payload that is not a mapping, a missing ``note`` key, or a blank note.
    Only a note a worker deliberately wrote produces a chat line.
    """
    if not isinstance(payload, dict):
        return ""
    return clip_handoff(payload.get("note"), limit)
