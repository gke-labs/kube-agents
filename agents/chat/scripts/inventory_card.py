"""The first inventory report as a card built from the stored findings, not the report's prose.

``bootstrap_delivery.py`` has the sweep's extracted findings
(``INVENTORY.items.json``), the ranking worker's scores (``INVENTORY.scores.json``,
absent on a clean fleet), the pacing limits (``INVENTORY.limits.json``) and the
items the worker's ``select`` chose (``INVENTORY.shown.json``). :func:`build`
recomputes that choice with ``inventory_findings.select_items``, the function
``select`` ran, checks it against the shown file, and lays the result out as the
card the issue mock draws: a headline counting the things to fix first, a
counts line, one row per listed item with the scored recommendation under it,
the queue line, the scan's checks folded, and the buttons. Nothing here reads
``INVENTORY.md``; a problem with any input raises :class:`CardUnavailable`
with its reason, and the caller posts the text instead.

:func:`chat` is the card as a ``kube-agents.chat/v1`` message for
``spec.mode: next``, :func:`blocks` the same card as Block Kit for the
today door, each with the fallback text the door posts when it cannot lay the
card out.

``FIX_FIRST``, ``FIX_ONLY`` and ``COUNT_WORDS`` are ``inventory_presenter``'s
until its card goes; the two cards must not differ in a word.
"""

from __future__ import annotations

from typing import Any, NamedTuple

import chat_message
import findings_queue as fq
import inventory_findings
import slack_presenter
from inventory_presenter import ACTION_ID_PREFIX, COUNT_WORDS, FIX_FIRST, FIX_ONLY

#: The files' top-level keys, as bootstrap_delivery.py names them (its test holds the copies equal).
ITEMS_KEY = "items"
SCORES_KEY = "scores"
#: The scores file's cluster names, the key ``inventory_findings.complete_clusters`` reads.
COMPLETE_KEY = "complete_clusters"
#: The files, as the reasons name them.
ITEMS_FILE = "the items file"
SCORES_FILE = "the scores file"
SHOWN_FILE = "the shown file"
SHOWN_ENTRY = "an item in the shown file"

HEADLINE_FIX = "First look at your fleet: {count}"
HEADLINE_DONE = "First look at your fleet: done"
#: Nothing listed, yet critical items wait: the list is turned off (a limit of
#: 0), or every listed row was dismissed. Not "done".
HEADLINE_QUEUED = "First look at your fleet: {count} queued"
THING_NOUN = ("thing to fix first", "things to fix first")
CRITICAL_NOUN = ("critical item", "critical items")
ONE_WORD = "one"
DETAIL_SEPARATOR = " · "
CLUSTERS_IN_FULL = "{count} scanned in full"
CLUSTER_NOUN = ("cluster", "clusters")
CHECKS_COUNT = "{count} configuration checks"
DONE_CHECKS = "no critical item in this scan's {count} checks"
MORE_OBJECTS = "{title}, and {count} more {noun}"
OBJECT_NOUN = ("object", "objects")
#: The scored recommendation under a row: what breaks, then what to do. Each
#: part is clipped on its own budget before the join, so a long risk cannot
#: push the action past the row's 300 characters.
RISK_MAX = 120
ACTION_MAX = 170
SENTENCE_JOIN = " "
#: The queue line, the one line after the rows. It paces the queued criticals
#: only: ``fq.pace`` adds no other item while a critical waits, so the others'
#: pace depends on the user's decisions, and the report text states it.
MORE_ITEMS_NOUN = ("more item is", "more items are")
SMALLER_ITEMS_NOUN = ("smaller item is", "smaller items are")
QUEUED_CRITICAL = "{more} queued, {deferred} of them critical."
QUEUED = "{more} queued."
PACE_CRITICALS = "I'll bring up to {count} critical a day."
PACE_CRITICALS_WAIT = "The critical ones wait until you ask."
FOLD_TITLE = "What the scan checked"
SEE_ALL = "See all {count}"
ITEM_NOUN = ("item", "items")
#: A fix button's reply names the finding, so the turn it posts carries it;
#: the title is clipped so the reply stays one the spec accepts.
FIX_REPLY = "{label}: {title}"
FIX_REPLY_JOIN = ": "
CRITICAL = fq.SEVERITIES[0]
NO_SCORES = "no scores file beside the items"
NOT_AN_OBJECT = "{name} is not a JSON object"
NO_LIST = "{name} has no `{key}` list"
NO_SCORES_MAP = "the scores file has no `scores` map"
NO_NAME_LIST = "the scores file's `{key}` is not a list of cluster names"
SHOWN_DIFFERS = "the listed items differ from the shown file: select chose {shown}, the recompute {recomputed}"
SELECT_FAILED = "the selection could not be recomputed: {errors}"
ERROR_JOIN = "; "
MARKDOWN_BOLD = "**"
LINE = "\n"


class CardUnavailable(Exception):
    """The card cannot be built from these files; the message says why."""


class Card(NamedTuple):
    headline: str
    detail: str
    rows: list[dict]
    after: str
    fold_title: str
    fold_rows: list[dict]
    choices: list[str]
    replies: dict[str, str]


def _plural(count: int, nouns: tuple[str, str]) -> str:
    return f"{count} {nouns[count != 1]}"


def _mapping(value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise CardUnavailable(NOT_AN_OBJECT.format(name=name))
    return value


def _inputs(items: Any, scores: Any, shown: Any) -> tuple[list, dict, set[str], list[list[str]]]:
    item_list = _mapping(items, ITEMS_FILE).get(ITEMS_KEY)
    if not isinstance(item_list, list):
        raise CardUnavailable(NO_LIST.format(name=ITEMS_FILE, key=ITEMS_KEY))
    if item_list:
        if scores is None:
            raise CardUnavailable(NO_SCORES)
        score_file = _mapping(scores, SCORES_FILE)
        score_map = score_file.get(SCORES_KEY)
        if not isinstance(score_map, dict):
            raise CardUnavailable(NO_SCORES_MAP)
        names = score_file.get(COMPLETE_KEY)
        if names is not None and not (isinstance(names, list) and all(isinstance(name, str) for name in names)):
            raise CardUnavailable(NO_NAME_LIST.format(key=COMPLETE_KEY))
        complete = inventory_findings.complete_clusters(score_file)
    else:
        # A clean fleet has nothing to score, so the SOP writes no scores file for it.
        score_map, complete = {}, set()
    shown_items = _mapping(shown, SHOWN_FILE).get(inventory_findings.SHOWN_ITEMS)
    if not isinstance(shown_items, list):
        raise CardUnavailable(NO_LIST.format(name=SHOWN_FILE, key=inventory_findings.SHOWN_ITEMS))
    shown_ids = []
    for entry in shown_items:
        ids = entry.get(inventory_findings.SHOWN_IDS) if isinstance(entry, dict) else None
        if not isinstance(ids, list):
            raise CardUnavailable(NO_LIST.format(name=SHOWN_ENTRY, key=inventory_findings.SHOWN_IDS))
        shown_ids.append([str(fid) for fid in ids])
    return item_list, score_map, complete, shown_ids


def _select(item_list: list, score_map: dict, limit: int, exclude: frozenset[str]) -> tuple[list[list[dict]], int, int]:
    try:
        return inventory_findings.select_items(item_list, score_map, limit, exclude)
    except inventory_findings.Failure as failure:
        raise CardUnavailable(SELECT_FAILED.format(errors=ERROR_JOIN.join(failure.errors))) from failure
    except (fq.FindingError, KeyError, TypeError, ValueError) as exc:
        raise CardUnavailable(SELECT_FAILED.format(errors=str(exc))) from exc


def _ids(chosen: list[list[dict]]) -> list[list[str]]:
    return [[row["id"] for row in members] for members in chosen]


def _best(members: list[dict]) -> dict:
    """The line's most severe row, the first in ranked order among equals.

    The queue ranks an actionable or higher-scoring major ahead of a floored
    critical, so a line's severity, and the row under its tag, come from this
    row and not from its first.
    """
    return min(members, key=lambda row: fq.SEVERITIES.index(row["severity"]))


def _listed(
    item_list: list, score_map: dict, limit: int, shown_ids: list[list[str]], suppressed: frozenset[str]
) -> tuple[list[list[dict]], int, int]:
    """The lines the card lists, and the deferred and other counts.

    ``select`` ran either with ``--exclude`` for every id ``register`` named
    suppressed (the SOP's Step 5) or with no exclusions (the shell sandbox
    kept it from the queue), so the shown file must equal one recompute or
    the other. The counts come from the excluded recompute, the fleet less
    the dismissed rows. The lines are the shown ones less those rows, and a
    line is listed only while a critical row is left in it: a line that lost
    its last critical is among the recompute's others, and nothing is
    promoted into a dismissed row's place, since only the shown items are
    marked shown.
    """
    excluded, deferred, others = _select(item_list, score_map, limit, suppressed)
    shown = excluded
    if suppressed and _ids(shown) != shown_ids:
        shown = _select(item_list, score_map, limit, frozenset())[0]
    if _ids(shown) != shown_ids:
        raise CardUnavailable(SHOWN_DIFFERS.format(shown=shown_ids, recomputed=_ids(excluded)))
    remaining = ([row for row in members if row["id"] not in suppressed] for members in shown)
    listed = [members for members in remaining if members and _best(members)["severity"] == CRITICAL]
    return listed, deferred + len(excluded) - len(listed), others


def _row(members: list[dict]) -> dict:
    best = _best(members)
    title = best["title"]
    if len(members) > 1:
        more = len(members) - 1
        title = MORE_OBJECTS.format(title=title, count=more, noun=OBJECT_NOUN[more != 1])
    recommendation = best["recommendation"]
    risk = slack_presenter._clip(chat_message._line(recommendation["risk"]), RISK_MAX)
    action = slack_presenter._clip(chat_message._line(recommendation["action"]), ACTION_MAX)
    return {"severity": best["severity"], "text": title, "detail": SENTENCE_JOIN.join(part for part in (risk, action) if part)}


def _headline(rows: int, deferred: int) -> str:
    if rows:
        word = ONE_WORD if rows == 1 else COUNT_WORDS.get(rows, str(rows))
        return HEADLINE_FIX.format(count=f"{word} {THING_NOUN[rows != 1]}")
    if deferred:
        return HEADLINE_QUEUED.format(count=_plural(deferred, CRITICAL_NOUN))
    return HEADLINE_DONE


def _detail(rows: int, deferred: int, complete: set[str]) -> str:
    checks = len(inventory_findings.SCAN_CHECKS)
    parts = []
    if complete:
        parts.append(CLUSTERS_IN_FULL.format(count=_plural(len(complete), CLUSTER_NOUN)))
    parts.append(CHECKS_COUNT.format(count=checks) if rows or deferred else DONE_CHECKS.format(count=checks))
    return DETAIL_SEPARATOR.join(parts)


def _queue_line(deferred: int, others: int, limits: fq.PacingLimits) -> str:
    remaining = deferred + others
    if not remaining:
        return ""
    if not deferred:
        return QUEUED.format(more=_plural(remaining, SMALLER_ITEMS_NOUN))
    queued = QUEUED_CRITICAL.format(more=_plural(remaining, MORE_ITEMS_NOUN), deferred=deferred)
    pace = PACE_CRITICALS.format(count=limits.daily_criticals) if limits.daily_criticals else PACE_CRITICALS_WAIT
    return f"{queued} {pace}"


def _fix_reply(label: str, title: str) -> str:
    room = chat_message.REPLY_MAX - len(label) - len(FIX_REPLY_JOIN)
    return FIX_REPLY.format(label=label, title=slack_presenter._clip(title, room))


def build(
    items: Any, scores: Any, limits: fq.PacingLimits, shown: Any, suppressed: frozenset[str] = frozenset()
) -> Card:
    """The card from the stored files, or :class:`CardUnavailable` with the reason.

    ``items``, ``scores`` and ``shown`` are the parsed files (``scores`` None when
    there is none, which a clean fleet allows). ``suppressed`` is the ids the
    queue reported the user had dismissed when this run registered the
    findings; see :func:`_listed` for how the shown file is checked against
    the recompute and what the rows and counts leave out.
    """
    item_list, score_map, complete, shown_ids = _inputs(items, scores, shown)
    listed, deferred, others = _listed(item_list, score_map, limits.first_report_criticals, shown_ids, suppressed)
    rows = [_row(members) for members in listed]
    total = len(rows) + deferred + others
    choices = []
    replies = {}
    if rows:
        label = FIX_FIRST if len(rows) > 1 else FIX_ONLY
        choices.append(label)
        replies[label] = _fix_reply(label, rows[0]["text"])
    if total > len(rows):
        choices.append(SEE_ALL.format(count=_plural(total, ITEM_NOUN)))
    return Card(
        headline=_headline(len(rows), deferred),
        detail=_detail(len(rows), deferred, complete),
        rows=rows,
        after=_queue_line(deferred, others, limits),
        fold_title=FOLD_TITLE,
        fold_rows=[{"text": words} for _, words in inventory_findings.SCAN_CHECKS],
        choices=choices,
        replies=replies,
    )


def text(card: Card) -> str:
    """The card as markdown: what a door posts when it cannot lay the card out."""
    lines = [f"{MARKDOWN_BOLD}{card.headline}{MARKDOWN_BOLD}", card.detail, ""]
    for row in card.rows:
        lines.append(slack_presenter.severity_row(row["severity"], row["text"]))
        lines.append(row["detail"])
    if card.after:
        lines += ["", card.after]
    return LINE.join(lines).rstrip() + LINE


def chat(card: Card) -> tuple[dict, str]:
    """``(message, text)``: the card as a ``kube-agents.chat/v1`` report message and its text."""
    message = chat_message.report_message(
        card.headline,
        detail=card.detail,
        rows=card.rows,
        choices=card.choices,
        after_rows=card.after,
        fold_title=card.fold_title,
        fold_rows=card.fold_rows,
        replies=card.replies,
    )
    return message, text(card)


def blocks(card: Card) -> tuple[list[dict], str]:
    """``(blocks, text)``: the card as Block Kit for the today door and the message's mrkdwn text."""
    built = slack_presenter.blocks_report(
        card.headline,
        detail=card.detail,
        rows=card.rows,
        choices=card.choices,
        action_id_prefix=ACTION_ID_PREFIX,
        after_rows=card.after,
        fold_title=card.fold_title,
        fold_rows=card.fold_rows,
    )
    lead = SENTENCE_JOIN.join(part for part in (card.headline, card.detail) if part)
    return built, slack_presenter.fallback_text(lead, rows=card.rows, after_rows=card.after)
