"""URL-safe clipping for the kanban notifier's completion handoff.

Installed into the image at ``/opt/hermes/gateway/kanban_handoff_clip.py`` and
wired into ``gateway/kanban_watchers.py`` by
``deploy/docker/patches/apply_kanban_notifier.py``.

A module of its own rather than part of ``gateway/kanban_notifier.py``, which
is what wires it in. It is a dependency-free text utility with no anchor into
upstream source, ``tools/cron_run_scope.py`` uses it for a different clip
entirely, and that patch is applied earlier in the build than the notifier one
— so it is copied into the image on its own, ahead of both.

Upstream builds the chat line for a completed card as::

    lines = payload_summary.strip().splitlines()
    h = lines[0][:200] if lines else payload_summary[:200]

Two things go wrong with that, and both were observed in production on
2026-08-03:

1. **The hard 200-character slice cuts mid-token.** A workload-reliability
   audit reported "… The audit ledger has been updated at
   https://github.com/gke-agentic/adamparco-infra/is" — the delivered message
   was exactly 200 characters, and `/issues/30` had been sliced down to `/is`.
   The link the whole report pointed at arrived broken.
2. **Everything after the first line is discarded**, so a worker that writes a
   perfectly good multi-line summary sees all but its opening line vanish.

``clip_handoff`` fixes both: it keeps the whole summary up to a much larger
budget and, when it must clip, clips on a whitespace boundary so the text
always ends at a whole token. A URL therefore either survives intact or is
dropped entirely — it is never truncated into a dead link.

Delivery-side length is not a concern here: the notifier calls
``adapter.send()`` directly rather than going through ``gateway/delivery.py``,
and the Slack adapter declares ``splits_long_messages`` and chunks internally.
"""

from __future__ import annotations

# Generous enough that a real audit summary is delivered whole; small enough
# that a runaway summary cannot flood the channel.
DEFAULT_LIMIT = 1200

ELLIPSIS = " […]"


def clip_handoff(text: object, limit: int = DEFAULT_LIMIT) -> str:
    """Return ``text`` clipped to ``limit`` characters without severing a token.

    Blank input yields an empty string. Text within budget is returned with only
    surrounding whitespace stripped. Over budget, the text is cut back to the
    last whitespace that fits and ``[…]`` is appended, so the final token is
    always whole — which is what keeps a trailing URL clickable.
    """
    if text is None:
        return ""
    body = str(text).strip()
    if limit <= 0 or len(body) <= limit:
        return body

    # Too small to hold a character and the marker: the marker is what gives
    # way, not the budget. `max(1, limit - 4)` kept one character and then
    # appended four more, so `limit=1` returned five — a clip that overshot the
    # only number it was given.
    if limit <= len(ELLIPSIS):
        return body[:limit].rstrip()

    head = body[: limit - len(ELLIPSIS)]
    cut = max(head.rfind(" "), head.rfind("\n"), head.rfind("\t"))
    if cut > 0:
        head = head[:cut]
    # No whitespace to cut on (one enormous token): a hard cut is the only
    # option left, and there is no partial-URL risk worth preserving in a token
    # that long.
    return head.rstrip() + ELLIPSIS
