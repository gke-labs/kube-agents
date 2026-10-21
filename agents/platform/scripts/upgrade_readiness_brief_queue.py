#!/usr/bin/env python3
"""upgrade_readiness_brief_queue.py - the findings queue as the weekly brief
reads it, read before the brief's model turn starts.

``upgrade-readiness-weekly-brief`` is a model turn over names a tenant can
write: budget, webhook and workload names reach it in the review's summary and
in the queue's rows. Its roster entry therefore gives it no MCP server
(``enabled_toolsets`` carries ``no_mcp``), so the turn holds no
``platform_control`` tool, and nothing it reads can make it register, verify
or decide a finding. The queue reads step 2 of its SOP needs are made here
instead: the scheduler runs this script before the turn (the entry's
``script``) and puts its stdout above the prompt.

- ``open``: ``GET /v1/findings/ranked``, the open backlog, worst first.
- ``snoozed`` and ``dismissed``: ``GET /v1/findings`` by state, at the queue's
  page limit, for each project in ``projects`` (the ones the review's summary
  names, and any other the open backlog holds).
- ``cut``: each of those pages that came back full, so its count is a floor.

Each list keeps only the rows whose check starts with ``upgrade-``, with the
fields the brief composes from. A read that fails prints ``error`` alone and
exits 0: the turn still runs, and its SOP has it say the queue was not read.
"""

from __future__ import annotations

import json
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import upgrade_readiness_review as review  # noqa: E402

RANKED_PATH = review.KV_FINDINGS_PATH + "/ranked"
DECIDED_STATES = ("snoozed", "dismissed")
PAGE_LIMIT = 1000  # the most rows one GET /v1/findings returns (findings_queue.list_findings)
OPEN_FIELDS = ("id", "state", "severity", "check", "project", "cluster", "namespace", "object", "title", "recommendation")
DECIDED_FIELDS = ("id", "severity", "project", "cluster", "snoozed_until")
ERROR_PREFIX = "the findings queue could not be read: "


def page_rows(answer: dict) -> list:
    rows = answer.get("findings")
    if not isinstance(rows, list):
        raise ValueError("the queue answered without a findings list")
    return rows


def upgrade_rows(rows: list, fields: tuple[str, ...]) -> list[dict]:
    return [
        {key: row[key] for key in fields if row.get(key) not in (None, "")}
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
    open_rows = upgrade_rows(page_rows(review.kv_request(RANKED_PATH)), OPEN_FIELDS)
    projects = sorted(set(summary_projects()) | {r["project"] for r in open_rows if r.get("project")})
    decided: dict[str, list[dict]] = {state: [] for state in DECIDED_STATES}
    cut = []
    for state in DECIDED_STATES:
        for project in projects:
            query = urllib.parse.urlencode({"state": state, "project": project, "limit": PAGE_LIMIT})
            rows = page_rows(review.kv_request(f"{review.KV_FINDINGS_PATH}?{query}"))
            if len(rows) >= PAGE_LIMIT:
                cut.append({"state": state, "project": project})
            decided[state] += upgrade_rows(rows, DECIDED_FIELDS)
    return {"projects": projects, "open": open_rows, **decided, "cut": cut}


def main() -> int:
    try:
        view = queue_view()
    except (OSError, ValueError) as exc:
        view = {"error": f"{ERROR_PREFIX}{exc}"}
    print(json.dumps(view))
    return 0


if __name__ == "__main__":
    sys.exit(main())
