"""Which coverage allowlist records in docs/designs/domains.yaml are past their expiry.

Each allowlist record excuses a domain with no blocking-roster case until its
`expires` date (the comment above `allowlist:` in that file describes the
fields). An expired record does not fail pull requests: the CI-health tick
calls `expired_records` and escalates to the record's owner on its issue.
scripts/test_domain_coverage.py checks the record shape on every pull request
and imports `is_date` and `record_owner` from here.
"""

from __future__ import annotations

import datetime
import pathlib
from typing import NamedTuple

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOMAINS_FILE = REPO_ROOT / "docs" / "designs" / "domains.yaml"


class ExpiredRecord(NamedTuple):
    slug: str
    owner: str | None
    issue: int | None
    expires: datetime.date
    days_past: int


def is_date(value: object) -> bool:
    """True for a plain date, as yaml.safe_load reads an unquoted YYYY-MM-DD.

    A timestamp (a datetime, which is a date subclass) or a quoted string is
    not one.
    """
    return isinstance(value, datetime.date) and not isinstance(value, datetime.datetime)


def record_owner(entry: dict, domain_owners: dict) -> str | None:
    """The record's `owner:`, else its domain row's."""
    owner = entry.get("owner")
    return owner if isinstance(owner, str) and owner.strip() else domain_owners.get(entry.get("slug"))


def expired_records(entries: list, domain_owners: dict, today: datetime.date) -> list[ExpiredRecord]:
    """The records whose `expires` date is before `today`, in file order.

    A record still holds on its `expires` day. Entries that are not records
    with a date are skipped; the shape test reports them.
    """
    expired = []
    for entry in entries:
        if not isinstance(entry, dict) or not is_date(entry.get("expires")):
            continue
        if entry["expires"] < today:
            issue = entry.get("issue")
            expired.append(
                ExpiredRecord(
                    slug=entry.get("slug"),
                    owner=record_owner(entry, domain_owners),
                    issue=issue if isinstance(issue, int) and not isinstance(issue, bool) else None,
                    expires=entry["expires"],
                    days_past=(today - entry["expires"]).days,
                )
            )
    return expired


def load(path: pathlib.Path = DOMAINS_FILE) -> tuple[dict, list]:
    """``({domain slug: owner}, allowlist entries)`` from domains.yaml."""
    import yaml  # the CI-health tick has PyYAML; keep the import local for callers that only pass entries

    data = yaml.safe_load(path.read_text()) or {}
    owners = {
        row["slug"]: row.get("owner")
        for row in data.get("domains") or []
        if isinstance(row, dict) and isinstance(row.get("slug"), str)
    }
    return owners, data.get("allowlist") or []


def utc_today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()
