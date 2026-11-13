"""Estimated end-to-end Slack times for a night's inject cases.

The full chat suite runs through the inject door, which skips Slack. The Slack
lane (hack/slack-lane/run.sh) runs a few canaries through real Slack and
records that night's Slack overhead from the A2A gateway's metrics
(slack-overhead.json; collect.py reads it into data.json's
``slack_overhead``). A case's estimate is its agent time plus one delivery lag
plus a post's time for each message its reply posted (the shape's
``bot_messages``), the formula hack/slack-lane/overhead.py's ``estimate``
holds. It assumes the canaries' quiet CI channel: no rate limiting under load
and no other posters, so it is a floor, not a forecast.
"""

from __future__ import annotations

import statistics

# How many nights the p95 is averaged over, the newest included.
P95_NIGHTS = 7
P50, P95 = 50, 95


def _percentile(values: list[float], pct: int) -> float | None:
    """Nearest rank, nightly.percentile's rule; nightly imports this module."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((pct / 100) * (len(ordered) - 1))]


def overhead_for(day: str | None, overheads) -> dict | None:
    """The Slack lane's overhead recorded on ``day`` (YYYY-MM-DD), the newest build's when a date has two."""
    for entry in overheads if isinstance(overheads, list) else []:
        if isinstance(entry, dict) and entry.get("date") == day:
            return entry
    return None


def estimate(agent_s, bot_messages, overhead: dict) -> float | None:
    lag, post = overhead.get("delivery_lag_s"), overhead.get("post_s")
    terms = (agent_s, bot_messages, lag, post)
    if any(not isinstance(t, (int, float)) or isinstance(t, bool) for t in terms):
        return None
    return round(agent_s + lag + bot_messages * post, 1)


def annotate(doc: dict, day: str | None, overheads) -> None:
    """Give each case of a night document its ``slack_e2e_s`` and the night its ``slack``
    summary, when the Slack lane recorded an overhead that night; nothing otherwise."""
    overhead = overhead_for(day, overheads)
    if overhead is None:
        return
    estimates = []
    for case in doc.get("cases") or []:
        shape = case.get("shape") or {}
        value = estimate(case.get("agent_p50_s"), shape.get("bot_messages_p50"), overhead)
        if value is not None:
            case["slack_e2e_s"] = value
            estimates.append(value)
    doc["slack"] = {
        "e2e_p50_s": _percentile(estimates, P50),
        "e2e_p95_s": _percentile(estimates, P95),
        "first_ack_p50_s": overhead.get("first_ack_p50_s"),
        "first_ack_p95_s": overhead.get("first_ack_p95_s"),
        "delivery_lag_s": overhead.get("delivery_lag_s"),
        "post_s": overhead.get("post_s"),
        "build_id": overhead.get("build_id"),
    }


def average_p95(nights: list[dict]) -> None:
    """Each night's ``slack.e2e_p95_7n_s``: the mean e2e p95 of up to P95_NIGHTS dates that
    carry one, its own and the dates before it (``nights`` newest first; a date's newest night
    counts once, so a same-date re-run does not count twice)."""
    for index, night in enumerate(nights):
        slack = night.get("slack")
        if not isinstance(slack, dict):
            continue
        by_date: dict = {}
        for n in nights[index:]:
            value = (n.get("slack") or {}).get("e2e_p95_s") if isinstance(n.get("slack"), dict) else None
            day = (n.get("started") or "")[:10]
            if isinstance(value, (int, float)) and day not in by_date:
                by_date[day] = value
            if len(by_date) == P95_NIGHTS:
                break
        slack["e2e_p95_7n_s"] = round(statistics.fmean(by_date.values()), 1) if by_date else None
