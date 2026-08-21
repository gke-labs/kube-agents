#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Deterministic report generator for the k8s-event-watcher daily activity summary."""

import argparse
import datetime
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional


DEFAULT_EXCLUDE_NAMESPACES = frozenset({"kube-system", "kube-public", "kube-node-lease"})


def excluded_namespaces() -> FrozenSet[str]:
    """Namespaces kept out of the breakdown and the headline counts.

    An environment knob rather than a YAML file, matching how every other
    threshold in this feature is tuned — `ALERT_DAILY_LIMIT_*` in
    `session_kv_server.py`, `WATCHER_*` on the sidecar. A file needs a search
    path, a parser and an answer for every way it can be half-written, and each
    of those three was load-bearing enough to carry its own tests; an
    environment variable is one `getenv`. Set it on the `PlatformAgent` CR under
    `spec.deployment.env` — the operator's `safeSandboxEnvOverrides` allowlist
    carries the name, and `kubectl set env` on the Deployment is reverted by the
    next reconcile.

    Resolved per call rather than at import so a test, or a cron tick with a
    patched environment, sees the current value. An empty value excludes
    nothing, which is the one setting the file could not express without
    tripping over its own `None` handling.

    This is a noise filter, and what it can and cannot reach is exact. An
    excluded row is still counted by the two alert tallies — `cap_dropped` and
    `delivery_failed` — so silencing a namespace cannot hide a ceiling drop or a
    failed delivery, and cannot hand the all-clear back to a day one of those
    spoiled. It does drop the row from `suppressed_info`: informational churn in
    an excluded namespace is precisely what the filter exists to stop reporting,
    and counting it in the veto would put every stock install with `kube-system`
    excluded permanently out of all-clear.

    `excluded_occurrences` is not a veto term either, for that same reason at one
    remove: `kube-system` ships excluded and the watcher forwards it anyway, so
    the count is non-zero on an ordinary day and vetoing on it would pin the
    header to 📊 and leave 🟢 unreachable. The residue is that a day whose only
    informational churn sat in an excluded namespace still grades green, which
    overstates coverage by exactly that much; the qualifier line under the counts
    carries the scope caveat in words, which an emoji cannot.
    `generate_markdown_report` owns that argument.
    """
    raw = os.getenv("EOD_EXCLUDE_NAMESPACES")
    if raw is None:
        return DEFAULT_EXCLUDE_NAMESPACES
    return frozenset(ns.strip() for ns in raw.split(",") if ns.strip())


def default_db_paths() -> List[str]:
    """Where to look for the session KV database, most authoritative first.

    SESSION_KV_DB_PATH is what `session_kv_server.py` itself reads and what the
    operator sets on both containers, so it is the only source that stays
    correct if the mount ever moves; the literal below is a fallback for a
    hand-run, not the contract. Resolved per call rather than at import so a
    test (or a cron tick with a patched environment) sees the current value.

    There is deliberately no `/tmp` candidate. Nothing in this repository
    writes a session database there, so it could only ever match a file some
    other process left behind — and `/tmp` in that container is writable by
    anything the agent runs, which would let whoever creates one filename
    author the recap, remediation text included. It would do so quietly, too:
    the search stops at the first candidate that reads, so an unmounted session
    volume would render a green recap over a stray file rather than the 🔴 card
    naming the paths it searched. These two cover the pod and a hand-run;
    `--db` covers everything else.
    """
    candidates = [
        os.getenv("SESSION_KV_DB_PATH", ""),
        "/var/lib/kube-agents/session/session_kv.db",
    ]
    # In the pod the operator sets the variable to the literal, so the two
    # collapse to one. Deduplicated because the read-failure card names every
    # path it searched, and the same path listed twice reads like two mounts
    # went missing.
    seen: Dict[str, None] = {}
    for candidate in candidates:
        if candidate:
            seen.setdefault(candidate, None)
    return list(seen)


# How far back a run looks. The job ticks on weekday evenings only, so Monday
# has to reach back over the weekend; see default_window_hours.
DEFAULT_WINDOW_HOURS = 24
WEEKEND_WINDOW_HOURS = 72


def default_window_hours(now: Optional[datetime.datetime] = None) -> int:
    """Hours a run must look back to leave no gap since the previous run.

    The cron expression is `0 21 * * 1-5`. A fixed 24-hour window means Monday
    reports back to Sunday 21:00 and nothing ever reports Friday 21:00 through
    Sunday 21:00 — the rows sit in the ledger for the full TTL and no run reads
    them. Monday therefore reaches back three days.

    `now` defaults to UTC because the scheduler ticks in UTC: Hermes evaluates
    the expression against `hermes_time.now()`, which falls back to the pod's
    zone when no `HERMES_TIMEZONE` or `config.yaml` `timezone` is set, and
    nothing here sets one. The weekday this tests therefore has to be the
    weekday the scheduler counted. Move the expression to another hour and the
    prose above changes but the arithmetic does not; move it to another *zone*
    without moving this clock and Monday's window detaches from the run that
    opens it.
    """
    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)
    # Monday is 0.
    return WEEKEND_WINDOW_HOURS if now.weekday() == 0 else DEFAULT_WINDOW_HOURS


def resolve_cluster_name(cli_cluster: Optional[str] = None) -> str:
    """Resolves the active cluster name from `--cluster-name` or the environment."""
    if cli_cluster:
        return cli_cluster

    return os.getenv("GKE_CLUSTER_NAME") or os.getenv("CLUSTER_NAME") or "kubernetes-cluster"


def load_intercepted_events(
    db_path: Optional[str] = None,
    window_hours: Optional[int] = None,
    problems: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Loads the events the watcher forwarded within the window, from the ledger.

    The ledger (`intercepted_events`, written by `session_kv_server.py`) is the
    source rather than the watcher's `dedup.json` snapshot, which cannot answer
    the question this report asks. That snapshot is a rolling cache of
    *currently active* incidents keyed by `(uid, reason)`: it carries no
    namespace and no workload name to group by, it is never pruned when a
    window expires so it spans the life of the volume rather than a day, and
    each entry's `count` resets to 1 every time its window rolls over. The
    ledger records one row per forwarded event, with the fields the report
    needs and a timestamp to bound them by.

    Nothing here scopes the query to one cluster, and that is deliberate.
    `start-services.sh` passes the watcher `--profiles-dir`, which makes every
    Cluster Agent profile in the pod a watched cluster fanning into this one
    table, so a `WHERE cluster = ?` would discard most of the fleet's events.
    The rows carry their cluster and `generate_markdown_report` says which ones
    the window covered.

    Returning `[]` is ambiguous — a quiet fleet and an unreadable ledger look
    identical to the caller — and this runs as a `no_agent` cron job whose
    stdout *is* the chat message, so a warning on stderr reaches the container
    log and nobody else. Pass a list as `problems` to collect the reasons no
    ledger could be read; `generate_markdown_report` renders them, so the
    difference reaches the reader rather than being reported as a clean day. An
    out-parameter rather than a changed return type, to keep the call sites.
    """
    if window_hours is None:
        window_hours = default_window_hours()
    candidates = [db_path] if db_path else default_db_paths()
    searched = [c for c in candidates if c]
    # Discarded wholesale if a later candidate reads cleanly: a failure on a
    # path that another path made up for is not something the recap reports.
    failures: List[str] = []
    for path_str in candidates:
        if not path_str:
            continue
        p = Path(path_str)
        if not p.exists():
            continue
        try:
            conn = sqlite3.connect(str(p), timeout=2.0)
            try:
                cursor = conn.cursor()
                columns = {row[1] for row in cursor.execute("PRAGMA table_info(intercepted_events)")}
                # A table without `cluster` or `object_uid` is not an older
                # shape to read around, it is a ledger nothing can write to.
                # `session_kv_server.record_intercepted_event` names both
                # unconditionally in its INSERT and wraps the whole write in a
                # blanket `except`, so on this shape every event raises
                # `no such column`, is logged, and is dropped. The
                # table stays empty. Substituting '' would read that empty
                # table cleanly and print a 🟢 all-clear every weekday over a
                # recording path that has never worked — the precise failure
                # the `problems` list exists to prevent. Reported as a read
                # failure instead; `session_management.md`, "A pre-release
                # table, and no migration", is the operator-facing fix.
                #
                # Guarded on `columns` being non-empty because PRAGMA returns
                # no rows for a table that does not exist, and that fault has
                # its own message: the SELECT below raises `no such table`.
                missing = [c for c in ("cluster", "object_uid") if c not in columns]
                if columns and missing:
                    raise sqlite3.OperationalError(
                        f"`intercepted_events` is missing {', '.join(missing)}, so "
                        "session_kv_server.py cannot write to it and the table "
                        "will stay empty — drop the table and let the server "
                        "recreate it"
                    )
                # `delivery_error` gets the tolerance `cluster` does not, and
                # the difference is which statement names the column. The
                # INSERT does not, so a table missing this one is still being
                # written to correctly; only `mark_delivery_failed`'s later
                # UPDATE fails. Absent therefore reads as '' — no failure
                # recorded — which is the truthful answer for rows nobody ever
                # checked, and the recap keeps working against a ledger written
                # by an older session server mid-rollout.
                delivery_col = "delivery_error" if "delivery_error" in columns else "''"
                cursor.execute(
                    "SELECT cluster, namespace, workload, object_uid, object_kind, reason, message, "
                    f"severity, occurrences, notified, created_at, {delivery_col} "
                    "FROM intercepted_events "
                    "WHERE created_at >= datetime('now', ?) "
                    "ORDER BY created_at DESC",
                    (f"-{int(window_hours)} hours",),
                )
                rows = cursor.fetchall()
            finally:
                conn.close()
        except sqlite3.OperationalError as e:
            # A database that predates the ledger has every other table but not
            # this one, and one that predates `cluster` has a table the writer
            # cannot use — the check above raises into here so both arrive as
            # one kind of answer. Reported rather than swallowed: the recap is
            # about to say nothing happened, and "the ledger is unusable" is a
            # different thing from "the fleet was quiet".
            sys.stderr.write(f"Warning: Cannot read intercepted_events from {path_str}: {e}\n")
            failures.append(f"`{path_str}` — cannot read `intercepted_events`: {e}")
            continue
        except Exception as e:
            sys.stderr.write(f"Warning: Failed to query {path_str}: {e}\n")
            failures.append(f"`{path_str}` — query failed: {e}")
            continue
        return [
            {
                "cluster": r[0] or "",
                "namespace": r[1] or "",
                "workload": r[2] or "",
                "object_uid": r[3] or "",
                "object_kind": r[4] or "",
                "reason": r[5] or "Unknown",
                "message": r[6] or "",
                "severity": r[7] or "",
                "occurrences": int(r[8] or 1),
                "notified": bool(r[9]),
                "created_at": r[10],
                "delivery_error": r[11] or "",
            }
            for r in rows
        ]
    # Distinguished from "the fleet was quiet". Two different faults end up
    # here and they point at different things, so they are not reported with
    # one message: nothing on any candidate path means the session volume is
    # not mounted where anything expects it, whereas a database that was found
    # and would not read has already appended its own reason above.
    if not failures:
        failures.append(f"no session KV database on any of: {', '.join(f'`{s}`' for s in searched)}")
        sys.stderr.write(
            f"Warning: no session KV database found on {', '.join(searched)}; "
            "reporting an empty day\n"
        )
    if problems is not None:
        problems.extend(failures)
    return []


def _plural(count: int, noun: str) -> str:
    """`1 event` / `2 events` — the recap is read by a human, not parsed."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def sanitize_chat_message(message: str) -> str:
    """Cleans up internal Kubernetes UID hashes and newlines for crisp chat rendering."""
    if not message:
        return ""
    msg = re.sub(r"_[a-zA-Z0-9-]+\([a-f0-9-]+\)", "", message)
    msg = re.sub(r"[\r\n\t]+", " ", msg).strip()
    return msg[:120]



# The severities this recap lists, and the whole of its subject. A Critical or
# Warning was posted to chat the moment it happened, so listing it here repeats
# what the on-call already read and acted on; informational events are the only
# class the severity gate holds back, which makes them the only class a daily
# digest tells anyone something new about. See the SOP, "What this recap does
# not report", for where the other grades are covered.
LISTED_SEVERITIES = frozenset({"Info"})

# One emoji rather than a severity map, because only one grade can reach the
# listing. Deliberately not 🔴: every listed group is an event the severity gate
# decided was not worth waking anyone for, and a red dot beside it recreates in
# the digest exactly the false alarm the gate exists to prevent.
_LISTED_EMOJI = "🔹"
_SECTION_HEADING = "🔕 Informational Events Held Back from Chat"
# The overflow past this is counted on a trailing line, never dropped silently.
_ENTRY_LIMIT = 10
# How many foreign cluster names the scope line prints before it counts the
# rest. A fan-in install can watch more clusters than a chat card can carry,
# and the point of the line is that the reader sees the scope is wider than the
# header's one name — not that they read every name.
_CLUSTER_LIMIT = 5


def filter_and_aggregate_events(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Deterministically groups and summarizes what k8s-event-watcher forwarded.

    The headline counts and the workload breakdown are derived from the same
    filtered rows, so they describe one scope: an excluded namespace is absent
    from both, not from one. `cap_dropped`, `cap_dropped_alerts` and
    `delivery_failed` are counted over every row instead, so a withheld or
    undelivered alert is reported wherever it happened. Anything the namespace filter removed is totalled in
    `excluded_occurrences`, which is what lets the report say it covered part
    of the fleet.
    """

    exclude_ns = excluded_namespaces()

    total_occurrences = 0
    excluded_occurrences = 0
    # Every cluster the window's rows came from, excluded namespaces included:
    # this is the scope the recap looked at, not the scope it found noise in.
    # Rows written with no cluster belong to whichever cluster the ledger is
    # mounted on, which `_workload_label` already treats as the report's own, so
    # they add nothing here.
    clusters_seen: set = set()
    alerts_posted = 0
    suppressed_info = 0
    cap_dropped = 0
    # Keyed on the UID rather than on `group_key`, and the two answer different
    # questions. On a quota refusal the watcher forgets the dedup entry, so one
    # failure re-offers on every sighting and writes a row each time — chat
    # would have received one alert. But `group_key` carries `workload`, which
    # `clean_workload_name` has already stripped the replica suffix from, so a
    # rollout that OOMKills forty pods of one Deployment collapses to a single
    # key: forty incidents reported as one. `(cluster, uid, reason)` is the
    # watcher's own dedup key and is the only thing in the row that separates
    # those two cases. `cap_dropped` rows still veto the all-clear, where any
    # row at all is the whole question.
    cap_dropped_incidents: set = set()
    delivery_failed = 0
    workload_map: Dict[str, Dict[str, Any]] = {}

    for event in events:
        ns = event.get("namespace", "")
        reason = event.get("reason", "Unknown")

        # A flag, not a `continue`. `EOD_EXCLUDE_NAMESPACES` is a noise filter on the
        # breakdown and the headline counts; the `cap_dropped` and `delivery_failed`
        # tallies are counted over every row regardless, because they are what
        # vetoes the ✅ all-clear. kube-system ships excluded and the watcher
        # forwards it anyway, so a `continue` here would drop a control-plane
        # delivery failure out of that veto and let the recap call the day clean.
        # SOP: "Where the namespace filter does and does not reach".
        excluded = bool(ns and ns in exclude_ns)

        count = int(event.get("occurrences", 1))
        severity = event.get("severity", "")
        if excluded:
            excluded_occurrences += count
        else:
            total_occurrences += count

        # `notified = 0` covers three unrelated outcomes and they must not be
        # reported as one: the severity gate held the event back as
        # informational, the daily ceiling dropped an alert that was going to
        # be posted, or chat itself refused the post. Conflating the second
        # with the first is how a day of cap-dropped OOMKills comes out as
        # "suppressed informational events"; conflating the third with the
        # second sends the reader to raise a limit that is not the problem.
        #
        # Only an explicit `Info` counts as informational, so a row with no
        # severity errs towards being reported rather than filed under the
        # quiet heading.
        #
        # The two `not excluded` tests are nested rather than folded into their
        # `elif`, and must stay that way: folded, an excluded row that alerted
        # falls past both branches into the `else` and is counted as withheld by
        # the ceiling. The branch still has to claim the row; only the increment
        # is skipped.
        #
        # `suppressed_info` is counted in occurrences, not rows: it is printed
        # by the closing 📉 line, which a reader checks against the `count` on
        # each listed group and against `total_occurrences` in the headline,
        # and those are occurrences. `alerts_posted` stays a row count on
        # purpose — it is printed as "*N alerts* went to chat", and chat
        # received one post per row however many sightings that row stands
        # for. Every row this version writes carries `occurrences = 1`, since
        # the watcher's payload count comes from `Observe`'s new-incident
        # branch and duplicates never reach the dispatch, so the two units
        # agree today; they are written apart so they keep agreeing if a
        # future writer batches sightings into one row.
        cap_row = False
        event_delivery_failed = bool(event.get("delivery_error"))
        if event_delivery_failed:
            delivery_failed += 1
        elif event.get("notified"):
            if not excluded:
                alerts_posted += 1
        elif severity == "Info":
            if not excluded:
                suppressed_info += count
        else:
            cap_dropped += 1
            cap_row = True

        # Used as stored. session_kv_server.clean_workload_name already stripped
        # the replica hash on the way in, and only for `kind == pod`. A
        # kind-agnostic pass over the result strips the last segment off any
        # name ending in five alphanumerics, merging `api-store` and `api-cache`
        # into one `api` line the SRE cannot resolve back to a service.
        workload = event.get("workload", "") or "unknown-workload"
        cluster = event.get("cluster", "") or ""
        if cluster:
            clusters_seen.add(cluster)
        # Cluster, severity and outcome are all in the key, and each for the
        # same reason: a group is listed or dropped as a unit, and its `count`
        # is printed as a fact about every row in it, so anything the listing
        # discriminates on has to be something the whole group shares.
        #
        # One session KV database serves every cluster profile in the pod, so
        # without the cluster `prod/api` on two clusters is one line with their
        # counts added. One reason arrives at both grades — `BackOff` is Info
        # when Kubernetes types it Normal and Warning when it does not — so
        # without the severity a group's grade is whichever row the ledger
        # returned first. And `notified` decides whether the group belongs
        # under a heading that says "Held Back from Chat" at all, so a group
        # holding one delivered row and one withheld row can be neither: keyed
        # without it, the two merge and the pair is listed as two withheld
        # events or as none, while the 📉 total counts one.
        group_key = f"{cluster}/{ns}/{workload}/{reason}/{severity}/{int(bool(event.get('notified')))}"
        if cap_row:
            cap_dropped_incidents.add((cluster, event.get("object_uid", ""), reason))
        msg = sanitize_chat_message(event.get("message", ""))

        if group_key in workload_map:
            group = workload_map[group_key]
            group["count"] += count
            if not group["message"]:
                group["message"] = msg
        else:
            workload_map[group_key] = {
                "key": group_key,
                "reason": reason,
                "cluster": cluster,
                "namespace": ns or "default",
                "workload": workload,
                "severity": severity,
                # Both set once rather than ORed on merge, because both are
                # decided by the key: the namespace settles `excluded`, and
                # `notified` is a key field in its own right.
                "excluded": excluded,
                "notified": bool(event.get("notified")),
                "count": count,
                "message": msg,
            }

    # Informational *and* held back. Both, because the heading over this list
    # says "Held Back from Chat" and the closing 📉 total counts only rows that
    # were: selecting on severity alone let a delivered Info row into the
    # listing while `suppressed_info` left it out, so one set of rows was
    # announced as "not repeated here", repeated here under a heading saying
    # they never arrived, and then totalled as zero.
    #
    # Only a session server predating the Info gate writes an Info row with
    # `notified = 1`, so nothing this version produces reaches the else-branch
    # — which is the point. The listing has one subject and the three lines
    # about it agree.
    filtered_entries = [
        e
        for e in workload_map.values()
        if not e["excluded"] and e["severity"] in LISTED_SEVERITIES and not e["notified"]
    ]

    # Descending, because the listing is cut at `_ENTRY_LIMIT` and the groups
    # that survive the cut should be the noisiest ones.
    filtered_entries.sort(key=lambda x: x["count"], reverse=True)

    # No dedup ratio here, deliberately: every ledger row is already one
    # deduplicated incident, so a derived "noise reduction" would measure key
    # collisions rather than the watcher's work. SOP: "Why no deduplication
    # ratio is reported".
    return {
        "total_occurrences": total_occurrences,
        # Excluded namespaces are out of scope here, matching
        # `total_occurrences` above: the headline counts and the breakdown
        # under them describe one scope, not two.
        "unique_incidents": sum(1 for e in workload_map.values() if not e["excluded"]),
        # How much the namespace filter removed. Not printed as a figure — it
        # is what lets the closing lines say they are reporting part of the
        # fleet instead of implying they covered all of it.
        "excluded_occurrences": excluded_occurrences,
        # Which clusters the counts above are summed over. Sorted so the header
        # renders the same way twice for the same day.
        "clusters": sorted(clusters_seen),
        "alerts_posted": alerts_posted,
        "suppressed_info": suppressed_info,
        "cap_dropped": cap_dropped,
        "cap_dropped_alerts": len(cap_dropped_incidents),
        "delivery_failed": delivery_failed,
        "entries": filtered_entries,
    }


def _workload_label(entry: Dict[str, Any], cluster_name: str) -> str:
    """`namespace/workload`, prefixed with the cluster when it is not this report's own.

    One session KV database serves every cluster profile in the pod, so the name
    in the header is the cluster this job runs on and not necessarily the one an
    event came from. Naming the cluster only when it differs leaves the ordinary
    single-cluster recap exactly as it was.
    """
    label = f"{entry['namespace']}/{entry['workload']}"
    cluster = entry.get("cluster") or ""
    if cluster and cluster != cluster_name:
        return f"{cluster}:{label}"
    return label


def generate_markdown_report(
    summary: Dict[str, Any],
    cluster_name: Optional[str] = None,
    report_date: Optional[str] = None,
    problems: Optional[List[str]] = None,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> str:
    """Renders a clean, chat-optimized markdown activity digest without awkward line breaks.

    `problems` carries the reasons `load_intercepted_events` could not read a
    ledger. They are rendered in place of the all-clear, because an unreadable
    ledger and a quiet fleet produce the same empty summary and the quiet-day
    wording is the one an operator will believe.

    `window_hours` is disclosed in the header on any run that is not the daily
    default. `report_date` is a single date, so Monday's 72-hour catch-up would
    otherwise print a weekend's churn under one day's heading with nothing on
    the card to say so.
    """
    if not cluster_name:
        cluster_name = resolve_cluster_name()

    if not report_date:
        report_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    entries = summary.get("entries", [])
    entry_count = len(entries)

    suppressed = summary.get("suppressed_info", 0)
    # What the counts cover, printed once and directly under them. Two
    # qualifications, each answering a way the numbers mislead on their own,
    # each gated on the condition that makes it true.
    #
    # Severity, because the headline and the three bullet counts are summed
    # over every row in scope while the listing below them holds `Info` alone.
    # A delivered alert is already accounted for in words — "*N alerts* went to
    # chat", "*Alerts Raised:* N" — so the residue that nothing explains is the
    # ceiling drop and the failed delivery, and those two are the gate. Without
    # it a day of 30 cap-dropped Criticals reports 30 events across 30 groups
    # over a listing naming none of them, and the reader who tries to reconcile
    # the two is reading a card whose numbers do not add up.
    #
    # Namespace, because a recap excluding `kube-system` otherwise says
    # "nothing was held back from chat" over a day of `kube-system` churn —
    # true of what it counted, false as the reader will read it.
    #
    # One line rather than a clause on each claim. Appended, the namespace note
    # landed in the headline, the ✅ and the 📉 of one small card — three times
    # on a day whose whole traffic was excluded — and a note printed three
    # times is read none.
    qualifiers: List[str] = []
    if summary.get("cap_dropped") or summary.get("delivery_failed"):
        qualifiers.append("Counts cover every severity; only informational events are listed.")
    if summary.get("excluded_occurrences"):
        qualifiers.append("Namespaces in `EOD_EXCLUDE_NAMESPACES` are outside this recap's scope.")
    scope_line = "_" + " ".join(qualifiers) + "_" if qualifiers else ""
    # This recap reports informational events and nothing else. A ceiling-withheld
    # or undelivered alert is graded Critical or Warning and was never this
    # report's subject, so it is never *named* below — no workload, no reason, no
    # message. It is counted: the two ⚠️ lines further down carry the totals, and
    # for the delivery-failure class that line is the only unprompted report of it
    # anywhere, since no metric counts that one. See the SOP, "What this recap does
    # not report", which owns the naming/counting line.
    #
    # Both counts are also read for a second purpose: they veto the ✅ all-clear and
    # the 🟢 header. Not reporting an alert is the choice; asserting it did not
    # happen is a different thing, and a recap that prints "nothing was held back
    # from chat" over a day the ceiling ate 30 Criticals is not silent about them,
    # it denies them.
    all_clear = not (
        summary.get("cap_dropped", 0) or suppressed or summary.get("delivery_failed", 0)
    )

    # Chat markup throughout, not Markdown: this job's stdout is delivered
    # verbatim to Google Chat or Slack, and both render bold as *single*
    # asterisks and have no headings at all. `**bold**` and `### Heading` reach
    # the reader as literal asterisks and hashes.
    lines: List[str] = []
    # The header grades what the day contained, not whether there was anything
    # to list, so 🟢 is gated on `all_clear` — the same condition as the ✅ below
    # — and not on `entry_count`. Green asserts the day was clean, so a day with
    # no informational events but 30 ceiling-withheld Criticals must not take
    # it; it falls to 📊, neutral, which is honest where green would not be.
    # SOP: "What the header emoji grades".
    #
    # `excluded_occurrences` is deliberately *not* a veto term. It reads like
    # one — the recap did not look in the excluded namespaces, so green
    # overclaims by a little — but `kube-system` ships in
    # `DEFAULT_EXCLUDE_NAMESPACES` and the watcher runs with no namespace filter
    # of its own, so on a real cluster that count is non-zero every single day.
    # Vetoing on it pins the header to 📊 forever: 🟢 becomes unreachable outside
    # an empty ledger, and three-state grading that only ever emits one state
    # grades nothing. Worse, it costs 📊 its meaning too — the neutral header
    # stops marking the days it was added to mark. The ✅ line below carries the
    # scope caveat in words, which is where a caveat belongs and was the
    # argument for the veto in the first place.
    if problems:
        header_emoji = "🔴"
    elif entry_count > 0 or not all_clear:
        header_emoji = "📊"
    else:
        header_emoji = "🟢"

    # `cluster_name` is the cluster this job runs on, and on a fan-in install it
    # is not the scope of a single number below it: the watcher is started with
    # `--profiles-dir`, so every Cluster Agent profile in the pod forwards into
    # one ledger and the reader counts all of it. The counts stay fleet-wide —
    # scoping them to the host would throw most of the report away — so what has
    # to change is the header, which otherwise prints a fleet's totals under one
    # cluster's name. Named, not filtered. SOP: "Which clusters a recap covers".
    other_clusters = [c for c in summary.get("clusters", []) if c and c != cluster_name]
    fleet_suffix = f" +{_plural(len(other_clusters), 'cluster')}" if other_clusters else ""
    window_suffix = "" if window_hours == DEFAULT_WINDOW_HOURS else f", last {window_hours}h"
    lines.append(
        f"{header_emoji} *k8s-event-watcher Daily Activity Recap* — "
        f"`{cluster_name}`{fleet_suffix} ({report_date}{window_suffix})"
    )
    if other_clusters:
        named = ", ".join(f"`{c}`" for c in other_clusters[:_CLUSTER_LIMIT])
        remaining_clusters = len(other_clusters) - _CLUSTER_LIMIT
        if remaining_clusters > 0:
            named += f" and {_plural(remaining_clusters, 'other')}"
        lines.append(
            "_One ledger serves every watched cluster, so every count below covers "
            f"{named} as well as `{cluster_name}`._"
        )

    # Above the body and outside both arms below, because an alert nobody
    # received is the one thing in this ledger that no other channel reports
    # unprompted. `k8s_event_watcher_events_quota_suppressed_total` and
    # `GET /v1/alert-quota` only help a reader who already suspects it, and a
    # failed delivery is not counted anywhere at all — the recap is what reaches
    # the on-call without being asked.
    #
    # Stated, not left to the absence of the all-clear: a day whose ceiling ate
    # thirty Criticals otherwise renders as an ordinary card under "Alerts
    # Raised: 0", and nobody notices a line that is not there. Counts rather
    # than listings, because this recap never names a withheld alert.
    if not problems:
        if summary.get("cap_dropped"):
            lines.append(
                f"⚠️ *{_plural(summary['cap_dropped_alerts'], 'alert')} withheld by the daily "
                "ceiling and never reached chat.*"
            )
        if summary.get("delivery_failed"):
            lines.append(
                f"⚠️ *{_plural(summary['delivery_failed'], 'alert')} failed to post to chat.*"
            )

    if entry_count > 0:
        # The alert count is a count, not a listing: it says the watcher paged
        # someone today, without repeating what chat already delivered.
        lines.append(
            f"_Forwarded *{_plural(summary['total_occurrences'], 'event')}* across "
            f"*{_plural(summary['unique_incidents'], 'workload/reason group')}*. "
            f"*{_plural(summary['alerts_posted'], 'alert')}* went to chat as it happened "
            f"and {'is' if summary['alerts_posted'] == 1 else 'are'} not repeated here._"
        )
        if scope_line:
            lines.append(scope_line)
        lines.append("")

        lines.append(f"*{_SECTION_HEADING}*")
        for idx, e in enumerate(entries[:_ENTRY_LIMIT], start=1):
            lines.append(
                f"{idx}. {_LISTED_EMOJI} "
                f"*`{_workload_label(e, cluster_name)}`* "
                f"(`{e['reason']}` • {_plural(e['count'], 'event')})"
            )
            if e.get("message"):
                lines.append(f"    • *Issue:* {e['message']}")
        remaining = len(entries) - _ENTRY_LIMIT
        if remaining > 0:
            lines.append(f"_…and {_plural(remaining, 'further group')} not listed._")
        lines.append("")

    else:
        # From the summary, not hardcoded to zero: "no incidents to list" is not
        # "no events seen", and a day whose entire traffic was suppressed lands
        # here. Withheld on a read failure, where the same three zeroes are the
        # absence of a measurement rather than a measurement of zero.
        if not problems:
            lines.append(f"• *Events Forwarded:* {summary['total_occurrences']}")
            lines.append(f"• *Workload/Reason Groups:* {summary['unique_incidents']}")
            lines.append(f"• *Alerts Raised:* {summary['alerts_posted']}")
            # Otherwise a recap excluding kube-system prints three zeroes that
            # read as a claim about the whole fleet on a day of kube-system churn.
            if scope_line:
                lines.append(scope_line)
            lines.append("")
        if problems:
            # The paths, in the body. This job runs `no_agent`, so its stdout is
            # the entire chat message and the stderr warning reaches only the
            # container log — where nobody looks, because the recap they did read
            # said everything was fine.
            lines.append("*⚠️ This recap could not read the event ledger — it does not describe the fleet.*")
            for problem in problems:
                lines.append(f"• {problem}")
            lines.append("")
            lines.append(
                "_Zero events below means the recap saw nothing, not that nothing happened. "
                "Check that the session volume is mounted and `session_kv_server.py` is writing "
                "to it before treating today as quiet._"
            )
        # Gated on the raw counts of everything the ledger recorded, including the
        # two classes this recap does not report. That is the whole point: the
        # claim is about the fleet, not about the recap's subject, so a day the
        # ceiling ate 30 Criticals stays silent rather than printing an all-clear
        # over them. `suppressed` is in the condition rather than `entry_count`
        # for the same reason: the listing is cut at `_ENTRY_LIMIT`, so a day with
        # informational events can still be a day this section does not name.
        #
        # The wording claims only what was measured, which is the ledger and
        # not the watcher. SOP: "What the ✅ all-clear does and does not claim".
        elif all_clear:
            lines.append(
                "✅ _Nothing was held back from chat in this window. This "
                "reports the ledger, not the watcher: confirm the daemon is running before "
                "reading a quiet day as a healthy one._"
            )

    # Always printed and last, including on a quiet day and on a day the
    # listing above already covers: that listing is cut at `_ENTRY_LIMIT` and
    # this is the total across all of them, and on a quiet day it is the only
    # line separating a fleet with nothing to say from a watcher that has
    # stopped forwarding. Counts events graded Info only: adding cap-dropped
    # Criticals here would report a day of alerts nobody received as routine
    # churn. Dropped on a read failure with the other counts.
    if not problems:
        while lines and not lines[-1]:
            lines.pop()
        lines.append("")
        lines.append(
            f"📉 _*{_plural(suppressed, 'informational event')}* held back from chat in this "
            "window, across every workload counted here._"
        )

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic k8s-event-watcher Daily Activity Recap")
    parser.add_argument("--db", help="Path to session_kv.db")
    parser.add_argument("--cluster-name", help="Cluster name override")
    parser.add_argument(
        "--window-hours",
        type=int,
        default=None,
        help=(
            f"How far back to report (default: {DEFAULT_WINDOW_HOURS}, "
            f"{WEEKEND_WINDOW_HOURS} on Monday to cover the weekend)"
        ),
    )
    args = parser.parse_args()

    window_hours = args.window_hours if args.window_hours is not None else default_window_hours()

    problems: List[str] = []
    events = load_intercepted_events(args.db, window_hours=window_hours, problems=problems)
    cluster = resolve_cluster_name(args.cluster_name)

    summary = filter_and_aggregate_events(events)
    report = generate_markdown_report(
        summary, cluster_name=cluster, problems=problems, window_hours=window_hours
    )
    print(report)


if __name__ == "__main__":
    main()
