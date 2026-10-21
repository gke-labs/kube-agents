#!/usr/bin/env python3
"""upgrade_readiness_brief_queue.py - the findings queue as the weekly brief
reads it, read before the brief's model turn starts.

``upgrade-readiness-weekly-brief`` is a model turn over names a tenant can
write: budget, webhook and workload names reach it in the review's summary and
in the queue's rows. Its roster entry therefore gives it the terminal tool and
no MCP server (``enabled_toolsets`` carries ``no_mcp``), so the turn holds no
``platform_control`` tool, and nothing it reads can make it register, verify
or decide a finding. The queue reads step 2 of its SOP needs are made here
instead: the scheduler runs this script before the turn (the entry's
``script``) and puts its stdout above the prompt.

- ``open``: ``GET /v1/findings/ranked``, the open backlog, worst first.
- ``snoozed`` and ``dismissed``: ``GET /v1/findings`` by state, at the queue's
  page limit, for each project in ``projects`` (the ones the review's summary
  names, and any other the open backlog holds); without the summary, which is
  the one list that names a project whose rows are all decided, each state is
  read once across every project instead.
- ``cut``: each of those pages that came back full, so its count is a floor;
  ``unread``: each one the script could not read.

Each list keeps only the rows whose check starts with ``upgrade-``. An
undecided critical row carries what a Decide-now line names, since the brief
may compose that line from the queue alone; every other row carries its state alone, which
keeps the prompt small on a large fleet. A backlog that cannot be read prints
``error`` alone and exits 0: the turn still runs, and its SOP has it say the
queue was not read. A decided page that cannot be read costs only its count.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import findings_queue  # noqa: E402
import upgrade_readiness_review as review  # noqa: E402

RANKED_PATH = review.KV_FINDINGS_PATH + "/ranked"
DECIDED_STATES = ("snoozed", "dismissed")
PAGE_LIMIT = findings_queue.MAX_LIST_LIMIT
DETAILED_SEVERITY = "critical"
UNDECIDED_STATES = ("queued", "surfaced")
DETAIL_FIELDS = ("id", "state", "severity", "check", "project", "cluster", "namespace", "object", "title")
STATE_FIELDS = ("id", "state", "severity", "check", "project", "cluster", "snoozed_until")
RECOMMENDATION_KEY = "recommendation"
ACTION_KEY = "action"
ERROR_PREFIX = "the findings queue could not be read: "


def page_rows(answer: dict) -> list:
    rows = answer.get("findings")
    if not isinstance(rows, list):
        raise ValueError("the queue answered without a findings list")
    return rows


def brief_row(row: dict, detailed: bool) -> dict:
    fields = DETAIL_FIELDS if detailed else STATE_FIELDS
    kept = {key: row[key] for key in fields if row.get(key) not in (None, "")}
    recommendation = row.get(RECOMMENDATION_KEY)
    if detailed and isinstance(recommendation, dict) and recommendation.get(ACTION_KEY):
        kept[ACTION_KEY] = recommendation[ACTION_KEY]
    return kept


def upgrade_rows(rows: list, open_backlog: bool) -> list[dict]:
    return [
        brief_row(row, open_backlog and row.get("severity") == DETAILED_SEVERITY and row.get("state") in UNDECIDED_STATES)
        for row in rows
        if isinstance(row, dict) and str(row.get("check") or "").startswith(review.CHECK_PREFIX)
    ]


def summary_projects() -> list[str]:
    """The projects the review's own summary names; none when it is missing or unreadable."""
    try:
        doc = json.loads((review.weekly_home() / review.LATEST_JSON_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    projects = doc.get("projects") if isinstance(doc, dict) else None
    return [p for p in projects if isinstance(p, str) and p] if isinstance(projects, list) else []


def queue_view() -> dict:
    open_rows = upgrade_rows(page_rows(review.kv_request(RANKED_PATH)), open_backlog=True)
    named = summary_projects()
    projects = set(named) | {r["project"] for r in open_rows if r.get("project")}
    # Per project when the summary names them; without it, nothing names a project whose
    # rows are all decided, so each state is read once across every project instead.
    pages = [{"state": s, "project": p} for s in DECIDED_STATES for p in sorted(projects)] if named else [{"state": s} for s in DECIDED_STATES]
    decided: dict[str, list[dict]] = {state: [] for state in DECIDED_STATES}
    cut, unread = [], []
    for page in pages:
        query = urllib.parse.urlencode({**page, "limit": PAGE_LIMIT})
        try:
            rows = page_rows(review.kv_request(f"{review.KV_FINDINGS_PATH}?{query}"))
        except (OSError, ValueError) as exc:
            # The backlog already read still says which rows nobody has decided;
            # only this page's count is lost.
            unread.append({**page, "error": str(exc)})
            continue
        if len(rows) >= PAGE_LIMIT:
            cut.append(page)
        decided[page["state"]] += upgrade_rows(rows, open_backlog=False)
    projects |= {r["project"] for state in DECIDED_STATES for r in decided[state] if r.get("project")}
    return {"projects": sorted(projects), "open": open_rows, **decided, "cut": cut, "unread": unread}


def main() -> int:
    try:
        view = queue_view()
    except (OSError, ValueError) as exc:
        view = {"error": f"{ERROR_PREFIX}{exc}"}
    print(json.dumps(view))
    return 0


if __name__ == "__main__":
    sys.exit(main())
