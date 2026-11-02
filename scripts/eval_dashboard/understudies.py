#!/usr/bin/env python3
"""Write the per-domain scorecard: each domain's presubmit seat, how long it
has been uncovered, its open `nightly-red` issues and its ranked understudies.

#3968 sections 2 and 6: a demotion that takes a domain's only blocking seat
needs a stand-in ready to cite, and the owner of a domain is expected to keep
one. ``coverage_gaps.py`` already ranks a domain's nightly cases on the owner
issue it files while the domain is uncovered; this is the same ranking for
every domain, covered or not, so a stand-in is visible before the demotion
rather than after it. One row per domain in ``docs/designs/domains.yaml``
order::

    seats            the roster cases that cover it (empty: uncovered)
    uncovered_since  the allowlist entry's start, read as coverage_gaps reads
                     it, with days_uncovered, the entry's issue, its held-out
                     case and how many days past `expires` it is
    nightly_red      open `nightly-red` issues labelled `domain:<slug>` and
                     the age of the oldest, in days; null when GitHub was not
                     read or the listing could not be trusted
    understudies     the best UNDERSTUDIES_PER_DOMAIN candidates
                     (``coverage_gaps.domain_candidates``: on the nightly
                     matrix, with a verification_spec, not expected_fail, off
                     the roster), each with its nightly record and the
                     evidence store's verdict
    understudy_ready whether any candidate reads `would-admit` in the store,
                     the roster's admission bar (docs/eval-gate-roster.md);
                     null when the store was not read this tick

The record bar is the only part of the seat bar read here: a recorded red,
classified misses, a guarded fixture and deterministic checks are held at
admission by ``scripts/test_eval_rosters.py``.

Nobody edits the ranking; it is a view of the record. The CI-health tick
writes it before the render, and ``render.py --scorecard`` puts it in
``brief.json`` for the Cases page's domain view (``cases.html#view=domains``).
It writes nothing to GitHub: the `nightly-red` listing is a read, through the
same trusted read the nightly bug filer uses (``nightly_bugs.read_listings``).
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

try:
    import domain_coverage
except ImportError:  # scripts/ is not on sys.path yet
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    import domain_coverage
from eval_dashboard import coverage_gaps, ghcli, nightly, nightly_bugs

UNDERSTUDIES_PER_DOMAIN = 2
DOMAIN_LABEL_PREFIX = nightly_bugs.DOMAIN_LABEL.format(slug="")
STATE_WOULD_ADMIT = "would-admit"


def log(message: str) -> None:
    print(message, file=sys.stderr)


def red_counts(issues: list | None, now: datetime.datetime) -> dict[str, dict] | None:
    """``{slug: {open, oldest_days}}`` from the open `nightly-red` issues by
    their `domain:<slug>` label; None when they were not read. An issue
    with no domain label (the infra ones) counts nowhere."""
    if issues is None:
        return None
    out: dict[str, dict] = {}
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("pull_request"):
            continue
        created = nightly.parse_iso(issue.get("created_at"))
        age = (now - created).days if created else None
        for label in issue.get("labels") or []:
            name = label.get("name") if isinstance(label, dict) else label
            if not isinstance(name, str) or not name.startswith(DOMAIN_LABEL_PREFIX):
                continue
            row = out.setdefault(name[len(DOMAIN_LABEL_PREFIX):], {"open": 0, "oldest_days": None})
            row["open"] += 1
            if age is not None and (row["oldest_days"] is None or age > row["oldest_days"]):
                row["oldest_days"] = age
    return out


def read_red_issues(gh: ghcli.Gh) -> list | None:
    """The open `nightly-red` issues, or None when the listing failed or
    GraphQL never agreed with it."""
    open_issues, _, doubt = nightly_bugs.read_listings(gh, None, label=nightly_bugs.LABEL)
    if open_issues is None or doubt:
        log(f"warning: the {nightly_bugs.LABEL} issues could not be read{f' ({doubt})' if doubt else ''}; the scorecard says so")
        return None
    return open_issues


def understudy(c: dict) -> dict:
    store = c["store"]
    return {"case": c["case"], "passed": c["passed"], "graded": c["graded"], "nights": c["nights"], "rate": c["rate"],
            "meets_bar": c["meets_bar"], "held_out": c["held_out"],
            "store": None if store is None else {"state": store.get("state"), "runs": store.get("runs"), "passes": store.get("passes")}}


def scorecard(repo_root: pathlib.Path, data: dict, store: dict | None, now: datetime.datetime, red_issues: list | None) -> dict:
    """The scorecard document. Raises as ``coverage_gaps.find_gaps`` does on
    a read that cannot be trusted (no domains, a gap off the allowlist)."""
    rows, _ = coverage_gaps.load_domains(repo_root)
    gaps = {g["slug"]: g for g in coverage_gaps.find_gaps(repo_root, data, store, now)}
    claims = domain_coverage.domain_claims(repo_root)
    candidates = coverage_gaps.domain_candidates(repo_root, data, store)
    reds = red_counts(red_issues, now)
    store_read = coverage_gaps.store_records(store, data) is not None
    domains = []
    for row in rows:
        slug = row["slug"]
        gap = gaps.get(slug) or {}
        ranked = None if candidates is None else candidates.get(slug, [])
        ready = None
        if store_read and ranked is not None:
            ready = any((c["store"] or {}).get("state") == STATE_WOULD_ADMIT for c in ranked)
        domains.append({
            "slug": slug, "name": row.get("name") or slug,
            "owner": gap.get("owner") or row.get("owner"), "backup": row.get("backup"),
            "seats": [coverage_gaps.case_of(path) for path, counts in claims.get(slug, []) if counts],
            "uncovered_since": gap["since"].isoformat() if gap.get("since") else None,
            "days_uncovered": gap.get("days"),
            "allowlist_issue": gap.get("issue"), "held_out": gap.get("held_out_case"), "expired_days": gap.get("expired_days"),
            "nightly_red": None if reds is None else reds.get(slug, {"open": 0, "oldest_days": None}),
            "understudies": None if ranked is None else [understudy(c) for c in ranked[:UNDERSTUDIES_PER_DOMAIN]],
            "candidates": None if ranked is None else len(ranked),
            "understudy_ready": ready,
        })
    return {"schema_version": 1, "generated_at": now.astimezone(datetime.timezone.utc).strftime(nightly_bugs.GITHUB_TIME_FORMAT),
            "bar": {"rate": coverage_gaps.BAR_RATE, "min_runs": coverage_gaps.BAR_RUNS, "nights": nightly_bugs.LOOKBACK_NIGHTS},
            "store_read": store_read, "nightly_red_read": reds is not None, "domains": domains}


def summary(doc: dict) -> str:
    lines = [f"scorecard: {len(doc['domains'])} domains, {sum(1 for d in doc['domains'] if d['seats'])} with a presubmit seat"]
    for d in doc["domains"]:
        best = ", ".join(u["case"] for u in d["understudies"] or []) or "none"
        ready = {True: "ready", False: "not ready", None: "store not read"}[d["understudy_ready"]]
        red = "not read" if d["nightly_red"] is None else d["nightly_red"]["open"]
        lines.append(f"  {d['slug']}: seat {', '.join(d['seats']) or 'none'}; understudies {best} ({ready}); nightly-red {red}")
    return "\n".join(lines)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True, type=pathlib.Path, help="data.json the collector wrote")
    parser.add_argument("--store", type=pathlib.Path, default=None, help="store.json the tick read (optional: without it understudy_ready is null)")
    parser.add_argument("--out", required=True, type=pathlib.Path, help="where to write scorecard.json")
    parser.add_argument("--repo", default=ghcli.DEFAULT_REPO)
    parser.add_argument("--repo-root", type=pathlib.Path, default=coverage_gaps.REPO_ROOT, help="checkout holding bench/tasks, hack/eval and domains.yaml")
    parser.add_argument("--now", help="evaluate as of this ISO 8601 time (default: now)")
    parser.add_argument("--no-github", action="store_true", help="read no issues; nightly_red is null")
    return parser.parse_args(argv)


def main(argv=None, gh_runner=None) -> int:
    args = parse_args(argv)
    data = coverage_gaps.read_json(args.data, "data.json")
    if data is None:
        log(f"ERROR: {args.data} is not a readable data.json")
        return 1
    now = nightly.parse_iso(args.now) or datetime.datetime.now(datetime.timezone.utc)
    red_issues = None
    if not args.no_github:
        # Read-only: a dry-run client refuses every write.
        gh = ghcli.Gh(args.repo, gh_runner, dry_run=True) if gh_runner else ghcli.Gh(args.repo, dry_run=True)
        red_issues = read_red_issues(gh)
    try:
        doc = scorecard(args.repo_root, data, coverage_gaps.read_json(args.store, "store.json"), now, red_issues)
    except (ValueError, AssertionError) as exc:
        log(f"ERROR: {exc}; no scorecard written")
        return 1
    args.out.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    print(summary(doc))
    return 0


if __name__ == "__main__":
    sys.exit(main())
