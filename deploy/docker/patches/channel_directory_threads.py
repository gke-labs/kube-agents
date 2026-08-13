"""Stop the Slack channel-directory refresh probing thread ids as channels.

Installed into the image at ``/opt/hermes/gateway/channel_directory_threads.py``
and wired into ``gateway/channel_directory.py`` by
``apply_channel_directory_threads.py``.

The storm
---------
Measured on the cluster on 2026-08-09: the credential proxy was returning a 502
every time, in bursts of 33, every five minutes -- roughly 9,500 failed Slack
API calls a day, all of them ``channel_not_found``. The burst's last request and
``/opt/data/channel_directory.json``'s ``updated_at`` share a millisecond, which
is what identified the caller.

Six things line up to produce it:

1. Gateway housekeeping refreshes the channel directory every five minutes.
2. ``_build_slack`` merges in entries derived from session history, and
   ``_session_entry_id`` composes those ids as ``f"{chat_id}:{thread_id}"``. So
   every Slack *thread* in a single DM becomes its own directory entry -- 33 of
   them, every one with base id ``D0BKGRBM6RH``.
3. ``_session_entry_name`` names them ``D0BKGRBM6RH / topic <ts>``.
4. The unresolved filter tests ``name.startswith(("C0", "D0", "G0"))``, and every
   one of those names does, so all 33 are queued for resolution.
5. ``conversations_info`` is then called with the *composite* id. Slack has never
   heard of ``D0BKGRBM6RH:1786240438.796879``, so it answers ``channel_not_found``
   and the proxy turns the ``SlackApiError`` into a 502.
6. The handler logs at DEBUG and continues, so no name is ever fixed and the same
   33 entries are re-probed on the next refresh, forever.

Note that step 6 is why this was invisible: the failure is caught, the directory
still builds, and the only symptom is someone else's error-rate graph.

The fix
-------
Three changes, in the order they matter:

``base_channel_id``
    Resolve against the channel, not the thread. This alone turns every one of
    the 33 calls from a guaranteed miss into a hit.

``unresolved_by_channel``
    One probe per distinct channel, not one per entry. 33 calls become 1, and the
    resolved name is applied to every entry sharing that channel.

``note_unresolvable`` / the miss cache
    A channel that genuinely cannot be resolved -- a private channel the bot was
    removed from -- is not retried for :data:`MISS_TTL_SECONDS`. The first two
    changes fix the storm we have; this one bounds the cost of the next one,
    because the failure mode above was unbounded repetition of a call that could
    never succeed.

:data:`MAX_PROBES_PER_REFRESH` caps a single refresh. Reaching it is logged at
WARNING rather than passed over: a silent cap would read like a fully resolved
directory and hide exactly the kind of growth that caused this.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, List, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "RAW_ID_PREFIXES",
    "MISS_TTL_SECONDS",
    "MAX_PROBES_PER_REFRESH",
    "base_channel_id",
    "looks_like_raw_id",
    "rename_entry",
    "unresolved_by_channel",
    "note_unresolvable",
    "note_resolved",
]

#: Slack id prefixes that mean a directory entry is still showing a raw id where
#: a human-readable name belongs. Kept identical to the tuple upstream tests, so
#: the two cannot disagree about which entries need resolving.
RAW_ID_PREFIXES = ("C0", "D0", "G0")

#: Separator ``_session_entry_id`` puts between a channel id and a thread id.
THREAD_SEPARATOR = ":"

#: Separator ``_session_entry_name`` puts between the channel label and the
#: thread label. Only the part before it is a channel name to be replaced --
#: rewriting the whole string would throw away which thread the entry is.
NAME_SEPARATOR = " / "

#: How long a channel that failed to resolve is left alone. Six hours: long
#: enough that a permanent failure costs four calls a day instead of 288, short
#: enough that re-inviting the bot to a channel fixes its name the same morning.
MISS_TTL_SECONDS = 6 * 60 * 60

#: Most channels probed in one refresh. The directory had 33 entries across 1
#: channel; a refresh wanting more than this many *distinct* channels is a new
#: bug, and should be reported rather than silently served.
MAX_PROBES_PER_REFRESH = 25

#: channel id -> monotonic time of the last failed probe.
_misses: Dict[str, float] = {}


def base_channel_id(entry_id: object) -> str:
    """The channel id inside a possibly thread-qualified directory entry id.

    ``"D0BKGRBM6RH:1786240438.796879"`` -> ``"D0BKGRBM6RH"``. A plain id is
    returned unchanged, so this is safe to call on every entry.
    """
    text = str(entry_id or "")
    return text.split(THREAD_SEPARATOR, 1)[0]


def looks_like_raw_id(name: object) -> bool:
    """Whether ``name`` is still a Slack id rather than something readable."""
    return str(name or "").startswith(RAW_ID_PREFIXES)


def rename_entry(entry: Dict[str, Any], resolved: str, entry_type: str = "") -> None:
    """Replace the raw-id part of ``entry``'s name with ``resolved``.

    ``"D0BKGRBM6RH / topic 1786240438.796879"`` becomes
    ``"<peer> / topic 1786240438.796879"``: the thread label is what tells two
    entries on the same channel apart, so it survives.

    A no-op when the entry already has a readable name, which makes this safe to
    call on every entry sharing a resolved channel without first checking which
    of them still needed it.
    """
    if not resolved:
        return
    name = str(entry.get("name") or "")
    head, sep, tail = name.partition(NAME_SEPARATOR)
    if not looks_like_raw_id(head):
        return
    entry["name"] = f"{resolved}{sep}{tail}" if sep else resolved
    if entry_type:
        entry["type"] = entry_type


def note_unresolvable(channel_id: object, detail: object = "") -> None:
    """Record that ``channel_id`` could not be resolved, so it is not retried."""
    cid = base_channel_id(channel_id)
    if not cid:
        return
    first = cid not in _misses
    _misses[cid] = time.monotonic()
    if first:
        # Once per channel per TTL, at DEBUG: an unresolvable channel is normal
        # (the bot is not in it) and the point of the cache is that nobody has
        # to hear about it again.
        logger.debug(
            "Channel directory: %s will not be probed again for %ds: %s",
            cid,
            MISS_TTL_SECONDS,
            detail,
        )


def note_resolved(channel_id: object) -> None:
    """Forget any recorded failure for ``channel_id``."""
    _misses.pop(base_channel_id(channel_id), None)


def _suppressed(channel_id: str, now: float) -> bool:
    last = _misses.get(channel_id)
    if last is None:
        return False
    if now - last >= MISS_TTL_SECONDS:
        del _misses[channel_id]
        return False
    return True


def unresolved_by_channel(
    channels: Iterable[Dict[str, Any]],
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Group entries still showing a raw id by the channel that would fix them.

    Returns ``[(channel_id, [entry, ...]), ...]`` in first-seen order, with
    recently-failed channels dropped and the list capped at
    :data:`MAX_PROBES_PER_REFRESH`. One tuple is one ``conversations.info`` call;
    every entry in it gets the answer.
    """
    now = time.monotonic()
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    # Channels, not entries: one DM contributed 33 entries to the directory that
    # caused this, and "skipped 33 channels" would misreport that as 33 problems.
    suppressed: set = set()
    for entry in channels:
        if not looks_like_raw_id(entry.get("name")):
            continue
        cid = base_channel_id(entry.get("id"))
        if not cid or cid in suppressed:
            continue
        if cid in grouped:
            grouped[cid].append(entry)
            continue
        if _suppressed(cid, now):
            suppressed.add(cid)
            continue
        grouped[cid] = [entry]

    items = list(grouped.items())
    if suppressed:
        logger.debug(
            "Channel directory: skipped %d channel(s) that failed to resolve "
            "within the last %ds",
            len(suppressed),
            MISS_TTL_SECONDS,
        )
    if len(items) > MAX_PROBES_PER_REFRESH:
        logger.warning(
            "Channel directory: %d channels need resolving, probing %d this "
            "refresh; the rest keep their raw ids until the next one",
            len(items),
            MAX_PROBES_PER_REFRESH,
        )
        items = items[:MAX_PROBES_PER_REFRESH]
    return items
