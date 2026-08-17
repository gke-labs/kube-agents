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
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import yaml
except ImportError:
    yaml = None


# The governance directory reaches the pod one way only: docker-entrypoint.sh
# step 2.6 scaffolds /opt/platform-template/governance/ into
# $TARGET_DIR/profiles/platform. There is no /opt/data/governance and no
# /opt/defaults/governance.
#
# HERMES_HOME first, because `profile_cron_tick.py` sets it to the profile home
# it is ticking, making `$HERMES_HOME/governance` the scaffolded directory
# itself. The absolute path is the fallback a hand-run from outside the profile
# resolves.
AGENT_HOME = os.getenv("PLATFORM_AGENT_HOME", "/opt/data")
HERMES_HOME = os.getenv("HERMES_HOME", "")

DEFAULT_CONFIG_PATHS = [
    path
    for path in (
        f"{HERMES_HOME}/governance/eod_report_config.yaml" if HERMES_HOME else "",
        f"{AGENT_HOME}/profiles/platform/governance/eod_report_config.yaml",
        # For a hand-run from a checkout, where none of the above exists.
        "agents/platform/governance/eod_report_config.yaml",
    )
    if path
]

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


def resolve_cluster_name(cli_cluster: Optional[str] = None, config: Optional[Dict[str, Any]] = None) -> str:
    """Resolves the active cluster name using GKE_CLUSTER_NAME environment variable or config."""
    if cli_cluster:
        return cli_cluster

    if config:
        cfg_name = config.get("cluster_name") or config.get("filters", {}).get("cluster_name")
        if cfg_name:
            return cfg_name

    return os.getenv("GKE_CLUSTER_NAME") or os.getenv("CLUSTER_NAME") or "kubernetes-cluster"


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Loads the YAML configuration with fallback defaults."""
    default_config: Dict[str, Any] = {
        "version": "v1",
        "filters": {
            "min_event_count": 1,
            "exclude_namespaces": ["kube-system", "kube-public", "kube-node-lease"],
        },
        "sections": {
            "telemetry_summary": True,
            "workload_breakdown": True,
        },
    }

    candidates = [config_path] if config_path else DEFAULT_CONFIG_PATHS
    for path_str in candidates:
        if not path_str:
            continue
        p = Path(path_str)
        if p.exists():
            try:
                content = p.read_text(encoding="utf-8")
                if yaml:
                    loaded = yaml.safe_load(content)
                elif path_str.endswith((".yaml", ".yml")):
                    raise ImportError("PyYAML is required to parse YAML configuration files. Please install 'pyyaml'.")
                else:
                    loaded = json.loads(content)

                if isinstance(loaded, dict):
                    # A key written with no value parses as `None`, and that is
                    # what the obvious edit produces: commenting out the three
                    # items under `exclude_namespaces:` — the natural way to
                    # stop excluding namespaces — leaves the key behind with
                    # nothing under it. Assigned over the default, that `None`
                    # reaches `set(...)` in filter_and_aggregate_events as a
                    # TypeError, and emptying `filters:` or `sections:` the same
                    # way gives an AttributeError earlier still. Nothing catches
                    # either: this job runs `no_agent`, so its stdout *is* the
                    # chat message, and the recap simply stops arriving every
                    # weekday with the traceback going only to the container
                    # log — the silent failure the 🔴 read-failure card exists to
                    # prevent, reached through a well-formed file.
                    #
                    # A key with no value says nothing about the setting, so it
                    # keeps the default. Both levels, because `filters:` merges
                    # as a dict and would otherwise pass its `None` members
                    # through.
                    for k, v in loaded.items():
                        if v is None:
                            continue
                        if isinstance(v, dict) and isinstance(default_config.get(k), dict):
                            default_config[k].update({ik: iv for ik, iv in v.items() if iv is not None})
                        else:
                            default_config[k] = v
                    return default_config

                # An empty file parses to None and a stray list to a list.
                # Without this the loop falls through to the "no config found"
                # warning below, which names every path it searched — including
                # this one, which exists and was read successfully.
                sys.stderr.write(
                    f"Warning: {path_str} is empty or is not a YAML mapping "
                    f"({type(loaded).__name__}); using built-in defaults.\n"
                )
            except Exception as e:
                sys.stderr.write(f"Warning: Failed to load config from {path_str}: {e}\n")

    # Falling through is not benign: the operator's exclude_namespaces and
    # section toggles are silently not in force, and because the shipped YAML
    # currently repeats these defaults value for value, an edit to it would
    # produce a byte-identical report with nothing saying why. Say so — on
    # stderr, because this job's stdout is delivered verbatim as the chat
    # message.
    sys.stderr.write(
        "Warning: no eod_report_config.yaml found on "
        f"{', '.join(str(c) for c in candidates if c)}; using built-in defaults\n"
    )
    return default_config


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
                # `cluster` postdates the rest of the table. Naming it
                # unconditionally would turn a database written by an older
                # session server into an OperationalError and lose the whole
                # day's ledger, so it is selected only when it is there and
                # substituted with '' when it is not.
                columns = {row[1] for row in cursor.execute("PRAGMA table_info(intercepted_events)")}
                cluster_col = "cluster" if "cluster" in columns else "''"
                # Same treatment, and for the same reason: a database written
                # by a session server that predates the delivery write-back has
                # no such column, and naming it would cost the whole day's
                # ledger. Absent reads as '' — no failure recorded — which is
                # the truthful answer for rows nobody ever checked.
                delivery_col = "delivery_error" if "delivery_error" in columns else "''"
                cursor.execute(
                    f"SELECT {cluster_col}, namespace, workload, object_kind, reason, message, "
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
            # this one. Reported rather than swallowed: the recap is about to
            # say nothing happened, and "the table is missing" is a different
            # thing from "the fleet was quiet".
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
                "object_kind": r[3] or "",
                "reason": r[4] or "Unknown",
                "message": r[5] or "",
                "severity": r[6] or "",
                "occurrences": int(r[7] or 1),
                "notified": bool(r[8]),
                "created_at": r[9],
                "delivery_error": r[10] or "",
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
# digest tells anyone something new about.
#
# Fixed rather than a defaulted `include_severities` key, which is what this
# was. Widening it bought a digest of already-delivered alerts that reads as
# new, and it never reached the two classes chat genuinely missed — alerts the
# daily ceiling withheld and alerts whose delivery failed — because those are
# not a severity selection but a separate outcome the recap declines to report
# at all. A knob whose only setting was the wrong one is not a knob. SOP:
# "What this recap does not report".
LISTED_SEVERITIES = frozenset({"Info"})

# One emoji rather than a severity map, because only one grade can reach the
# listing. Deliberately not 🔴: every listed group is an event the severity gate
# decided was not worth waking anyone for, and a red dot beside it recreates in
# the digest exactly the false alarm the gate exists to prevent.
_LISTED_EMOJI = "🔹"
_SECTION_HEADING = "🔕 Informational Events Held Back from Chat"
# Ten, not the five an alert list used. That five was sized against the risk of
# a routine BackOff pushing the day's real OOMKilled off the list; an Info-only
# list has no warning to protect, and routine churn is precisely what the
# reader came for. The overflow is counted on a trailing line either way.
_ENTRY_LIMIT = 10


def filter_and_aggregate_events(
    events: List[Dict[str, Any]],
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Deterministically groups and summarizes what k8s-event-watcher forwarded.

    The headline counts and the workload breakdown are derived from the same
    filtered rows, so they describe one scope: an excluded namespace is absent
    from both, not from one. `cap_dropped` and `delivery_failed` — the two
    outcomes no toggle switches off — are counted over every row instead, so a
    withheld or undelivered alert is reported wherever it happened. Anything
    the namespace filter removed is totalled in `excluded_occurrences`, which is
    what lets the report say it covered part of the fleet.
    """

    filters = config.get("filters", {})
    min_count = int(filters.get("min_event_count", 1))
    exclude_ns = set(filters.get("exclude_namespaces", []))
    # `include_severities` was a configuration key and is not one any more. An
    # install that still carries it gets told so on stderr rather than having it
    # silently ignored, because the two readings of a stale key are opposite:
    # somebody who wrote `["Info"]` loses nothing, and somebody who widened it is
    # reading a narrower report than the one they configured.
    if "include_severities" in filters:
        configured = {str(s) for s in filters.get("include_severities") or [] if str(s).strip()}
        if configured - LISTED_SEVERITIES:
            sys.stderr.write(
                "Warning: filters.include_severities in eod_report_config.yaml is no longer "
                f"honoured — {sorted(configured)} requested, but this recap lists "
                "informational events only. Remove the key; the SOP section 'What this "
                "recap does not report' says where the other severities are covered.\n"
            )
        else:
            sys.stderr.write(
                "Warning: filters.include_severities in eod_report_config.yaml is obsolete and "
                "ignored; the listing is fixed to informational events. Remove the key.\n"
            )

    total_occurrences = 0
    forwarded = 0
    excluded_occurrences = 0
    alerts_posted = 0
    suppressed_info = 0
    cap_dropped = 0
    delivery_failed = 0
    workload_map: Dict[str, Dict[str, Any]] = {}

    for event in events:
        ns = event.get("namespace", "")
        reason = event.get("reason", "Unknown")

        # A flag, not a `continue`. `exclude_namespaces` is a noise filter on the
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
            forwarded += 1

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
        event_delivery_failed = bool(event.get("delivery_error"))
        event_cap_dropped = False
        if event_delivery_failed:
            delivery_failed += 1
        elif event.get("notified"):
            if not excluded:
                alerts_posted += 1
        elif severity == "Info":
            if not excluded:
                suppressed_info += 1
        else:
            cap_dropped += 1
            event_cap_dropped = True

        # Used as stored. session_kv_server.clean_workload_name already stripped
        # the replica hash on the way in, and only for `kind == pod`. A
        # kind-agnostic pass over the result strips the last segment off any
        # name ending in five alphanumerics, merging `api-store` and `api-cache`
        # into one `api` line the SRE cannot resolve back to a service.
        workload = event.get("workload", "") or "unknown-workload"
        cluster = event.get("cluster", "") or ""
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
        msg = sanitize_chat_message(event.get("message", ""))

        if group_key in workload_map:
            group = workload_map[group_key]
            group["count"] += count
            # `notified` is not merged: it is part of the key, so every row
            # here already agrees on it.
            group["cap_dropped"] = group["cap_dropped"] or event_cap_dropped
            group["delivery_failed"] = group["delivery_failed"] or event_delivery_failed
            # Counted alongside the flags, because the flags are ORs while
            # `count` sums the whole group. Ten delivered rows and one that
            # failed is `delivery_failed = True` with `count = 11`, and a
            # listing that prints `count` claims eleven alerts never reached
            # chat when one did not.
            group["cap_dropped_count"] += count if event_cap_dropped else 0
            group["delivery_failed_count"] += count if event_delivery_failed else 0
            if event_delivery_failed and not group["delivery_error"]:
                group["delivery_error"] = str(event.get("delivery_error", ""))
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
                "cap_dropped": event_cap_dropped,
                "delivery_failed": event_delivery_failed,
                "delivery_error": str(event.get("delivery_error", "")) if event_delivery_failed else "",
                "count": count,
                "cap_dropped_count": count if event_cap_dropped else 0,
                "delivery_failed_count": count if event_delivery_failed else 0,
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
    #
    # min_event_count is applied after grouping, not per row: it is a threshold
    # on how much noise a workload made today, and a workload that tripped ten
    # times is over a threshold of three even though no single row is.
    filtered_entries = [
        e
        for e in workload_map.values()
        if not e["excluded"]
        and e["severity"] in LISTED_SEVERITIES
        and not e["notified"]
        and e["count"] >= min_count
    ]

    # Aggregated, never rendered. `generate_markdown_report` reports informational
    # events only, so neither list reaches the chat message; they are part of the
    # summary because callers other than the report read it, and because the two
    # counts beside them veto the ✅ all-clear and the 🟢 header — a recap may
    # decline to report a withheld alert, but it may not assert the day was clean
    # over one. Kept as lists rather than bare counters so a consumer can say which
    # workloads, and so restoring a listing is a rendering change and not a
    # re-derivation.
    #
    # `exclude_namespaces` deliberately does not apply to either: that filter drops
    # routine churn from the breakdown, and an alert nobody received is not churn.
    #
    # Thresholded on `cap_dropped_count`, not on `count`: against the group total a
    # workload with one withheld alert and nine delivered ones would clear a
    # threshold of five on the strength of nine events that are not withheld.
    cap_dropped_entries = [
        e
        for e in workload_map.values()
        if e["cap_dropped"] and e["cap_dropped_count"] >= min_count
    ]

    # No `min_event_count` either, and for a stronger reason than the ceiling has:
    # a ceiling drop is still counted by `k8s_event_watcher_events_quota_suppressed_total`
    # and `GET /v1/alert-quota`, whereas nothing anywhere counts a failed delivery —
    # the alert is not in chat and the metric recorded it as sent. The ledger's
    # `delivery_error` column is the only trace, and this list is the only thing
    # that reads it.
    delivery_failed_entries = [e for e in workload_map.values() if e["delivery_failed"]]

    filtered_entries.sort(key=lambda x: x["count"], reverse=True)
    # Sorted on the same number each section prints, so that the five that
    # survive the `[:5]` cut are the five worst by that section's measure
    # rather than the five noisiest workloads that happen to appear in it.
    cap_dropped_entries.sort(key=lambda x: x["cap_dropped_count"], reverse=True)
    delivery_failed_entries.sort(key=lambda x: x["delivery_failed_count"], reverse=True)

    # No dedup ratio here, deliberately: every ledger row is already one
    # deduplicated incident, so a derived "noise reduction" would measure key
    # collisions rather than the watcher's work. SOP: "Why no deduplication
    # ratio is reported".
    return {
        "total_occurrences": total_occurrences,
        "forwarded": forwarded,
        # Excluded namespaces are out of scope here, matching
        # `total_occurrences` above: the headline counts and the breakdown
        # under them describe one scope, not two.
        "unique_incidents": sum(1 for e in workload_map.values() if not e["excluded"]),
        # How much the namespace filter removed. Not printed as a figure — it
        # is what lets the closing lines say they are reporting part of the
        # fleet instead of implying they covered all of it.
        "excluded_occurrences": excluded_occurrences,
        "alerts_posted": alerts_posted,
        "suppressed_info": suppressed_info,
        "cap_dropped": cap_dropped,
        "delivery_failed": delivery_failed,
        "entries": filtered_entries,
        "cap_dropped_entries": cap_dropped_entries,
        "delivery_failed_entries": delivery_failed_entries,
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
    config: Dict[str, Any],
    cluster_name: Optional[str] = None,
    report_date: Optional[str] = None,
    problems: Optional[List[str]] = None,
) -> str:
    """Renders a clean, chat-optimized markdown activity digest without awkward line breaks.

    `problems` carries the reasons `load_intercepted_events` could not read a
    ledger. They are rendered in place of the all-clear, because an unreadable
    ledger and a quiet fleet produce the same empty summary and the quiet-day
    wording is the one an operator will believe.
    """
    if not cluster_name:
        cluster_name = resolve_cluster_name(config=config)

    if not report_date:
        report_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    sections = config.get("sections", {})
    entries = summary.get("entries", [])
    entry_count = len(entries)

    suppressed = summary.get("suppressed_info", 0)
    # Appended to the two lines that make a claim about the whole fleet, and
    # only when the filter actually removed something. Without it a recap
    # excluding kube-system reports "nothing was held back from chat" over a day
    # of kube-system churn — true of what it counted, false as the reader will
    # read it.
    scope_note = (
        " Namespaces in `filters.exclude_namespaces` are outside this recap's scope."
        if summary.get("excluded_occurrences")
        else ""
    )
    # This recap reports informational events and nothing else. A ceiling-withheld
    # or undelivered alert is graded Critical or Warning, was never this report's
    # subject, and is not named, counted or hinted at anywhere below — see the SOP,
    # "What this recap does not report", for the gap that leaves and where the
    # remaining signal lives.
    #
    # Both counts are still read, for one purpose: they veto the ✅ all-clear and
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
    # The header grades what the day contained, not whether there was anything to
    # list — computed inside the quiet arm, where it started, the busy arm opened
    # 📊 unconditionally, and one informational event is enough to take that arm.
    #
    # 🟢 is gated on `all_clear`, the same condition as the ✅ below, and not on
    # `entry_count`: green is an assertion that the day was clean, so a day with no
    # informational events but 30 ceiling-withheld Criticals must not take it. It
    # falls to 📊 — neutral, which is honest, where green would not be. SOP: "What
    # the header emoji grades".
    if problems:
        header_emoji = "🔴"
    elif entry_count > 0 or not all_clear:
        header_emoji = "📊"
    else:
        header_emoji = "🟢"
    lines.append(f"{header_emoji} *k8s-event-watcher Daily Activity Recap* — `{cluster_name}` ({report_date})")

    if entry_count > 0:
        if sections.get("telemetry_summary", True):
            # The alert count is a count, not a listing: it says the watcher
            # paged someone today, without repeating what chat already
            # delivered.
            lines.append(
                f"_Forwarded *{_plural(summary['total_occurrences'], 'event')}* across "
                f"*{_plural(summary['unique_incidents'], 'workload/reason group')}*. "
                f"*{_plural(summary['alerts_posted'], 'alert')}* went to chat as it happened "
                f"and {'is' if summary['alerts_posted'] == 1 else 'are'} not repeated here."
                + scope_note
                + "_"
            )
        lines.append("")

        if sections.get("workload_breakdown", True):
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
        if sections.get("telemetry_summary", True) and not problems:
            lines.append(f"• *Events Forwarded:* {summary['total_occurrences']}")
            lines.append(f"• *Workload/Reason Groups:* {summary['unique_incidents']}")
            lines.append(f"• *Alerts Raised:* {summary['alerts_posted']}")
            # Otherwise a recap excluding kube-system prints three zeroes that
            # read as a claim about the whole fleet on a day of kube-system churn.
            if scope_note:
                lines.append(f"_{scope_note.strip()}_")
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
        # over them. `suppressed` is in the condition for the same reason, since
        # the closing 📉 total reports events `min_event_count` kept out of the
        # listing.
        #
        # The wording claims only what was measured, which is the ledger and
        # not the watcher. SOP: "What the ✅ all-clear does and does not claim".
        elif all_clear:
            lines.append(
                "✅ _Nothing was held back from chat in this window." + scope_note + " This "
                "reports the ledger, not the watcher: confirm the daemon is running before "
                "reading a quiet day as a healthy one._"
            )

    # Always printed and last, including on a quiet day and on a day the
    # listing above already covers: that listing is cut at `_ENTRY_LIMIT` and
    # this is the total across all of them, and on a quiet day it is the only
    # line separating a fleet with nothing to say from a watcher that has
    # stopped forwarding. Counts events graded Info only — cap-dropped
    # Criticals were once added here, which read as routine churn on a day of
    # alerts nobody received. Dropped on a read failure with the other counts.
    if sections.get("suppressed_summary", True) and not problems:
        while lines and not lines[-1]:
            lines.pop()
        lines.append("")
        lines.append(
            f"📉 _*{_plural(suppressed, 'informational event')}* held back from chat today, "
            "across every workload counted here." + scope_note + "_"
        )

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic k8s-event-watcher Daily Activity Recap")
    parser.add_argument("--config", help="Path to eod_report_config.yaml")
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

    cfg = load_config(args.config)
    problems: List[str] = []
    events = load_intercepted_events(args.db, window_hours=window_hours, problems=problems)
    cluster = resolve_cluster_name(args.cluster_name, cfg)

    summary = filter_and_aggregate_events(events, cfg)
    report = generate_markdown_report(
        summary, cfg, cluster_name=cluster, problems=problems
    )
    print(report)


if __name__ == "__main__":
    main()
