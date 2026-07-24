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
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    yaml = None


DEFAULT_CONFIG_PATHS = [
    "/opt/data/governance/eod_report_config.yaml",
    "/opt/defaults/governance/eod_report_config.yaml",
    "agents/platform/governance/eod_report_config.yaml",
]

DEFAULT_DEDUP_PATHS = [
    "/var/lib/kube-agents/watcher/dedup.json",
    "/tmp/k8s-event-watcher-dedup.json",
    "/tmp/dedup.json",
]

DEFAULT_DB_PATHS = [
    "/var/lib/kube-agents/session/session_kv.db",
    "/tmp/session_kv.db",
]


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
            "include_namespaces": [],
            "exclude_namespaces": ["kube-system", "kube-public", "kube-node-lease"],
            "allowed_reasons": [],
        },
        "sections": {
            "telemetry_summary": True,
            "workload_breakdown": True,
            "action_items": True,
        },
        "formatting": {
            "verbosity": "detailed",
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
                    for k, v in loaded.items():
                        if isinstance(v, dict) and isinstance(default_config.get(k), dict):
                            default_config[k].update(v)
                        else:
                            default_config[k] = v
                    return default_config
            except Exception as e:
                sys.stderr.write(f"Warning: Failed to load config from {path_str}: {e}\n")
    return default_config


def load_dedup_data(dedup_path: Optional[str] = None) -> Dict[str, Any]:
    """Loads the k8s-event-watcher processed dedup snapshot file."""
    candidates = [dedup_path] if dedup_path else DEFAULT_DEDUP_PATHS
    for path_str in candidates:
        if not path_str:
            continue
        p = Path(path_str)
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data:
                    return data
            except Exception as e:
                sys.stderr.write(f"Warning: Failed to read dedup snapshot from {path_str}: {e}\n")
    return {}


def load_incident_records(db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Loads completed diagnostic triage reports from SQLite incidents table."""
    candidates = [db_path] if db_path else DEFAULT_DB_PATHS
    for path_str in candidates:
        if not path_str:
            continue
        p = Path(path_str)
        if p.exists():
            try:
                conn = sqlite3.connect(str(p), timeout=2.0)
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT chat_id, thread_id, report, created_at FROM incidents ORDER BY created_at DESC")
                    rows = cursor.fetchall()
                finally:
                    conn.close()
                return [
                    {"chat_id": r[0], "thread_id": r[1], "report": r[2], "created_at": r[3]}
                    for r in rows
                    if r[2]
                ]
            except Exception:
                pass
    return []


def clean_workload_name(name: str) -> str:
    """Strips ephemeral replica hashes to group by parent deployment/workload."""
    m = re.match(r"^(.*?)-[a-f0-9]{8,10}-[a-z0-9]{5}$", name)
    if m:
        return m.group(1)
    m = re.match(r"^(.*?)-[a-z0-9]{5}$", name)
    if m:
        return m.group(1)
    return name


def sanitize_chat_message(message: str) -> str:
    """Cleans up internal Kubernetes UID hashes and newlines for crisp chat rendering."""
    if not message:
        return ""
    msg = re.sub(r"_[a-zA-Z0-9-]+\([a-f0-9-]+\)", "", message)
    msg = re.sub(r"[\r\n\t]+", " ", msg).strip()
    return msg[:120]


def extract_actionable_fix_from_triage(
    workload: str,
    reason: str,
    message: str,
    incidents: List[Dict[str, Any]],
) -> str:
    """Extracts the actionable remediation fix directly from the SQLite incident triage reports."""
    for inc in incidents:
        report_text = inc.get("report", "")
        if not report_text:
            continue
        thread_id = inc.get("thread_id", "")
        thread_workload = thread_id.split("/")[-1] if "/" in thread_id else thread_id
        if thread_workload.lower() == workload.lower() or re.search(rf"\b{re.escape(workload)}\b", report_text, re.I):
            lines = report_text.splitlines()
            capture = False
            for line in lines:
                l_clean = line.strip()
                if re.search(r"###?\s*(Remediation|Action Items|Recommendation|Fix|Next Steps)", l_clean, re.I):
                    capture = True
                    continue
                if capture:
                    if l_clean.startswith("#"):
                        break
                    clean_text = re.sub(r"^[-*0-9.#]+\s*", "", l_clean).strip()
                    if len(clean_text) > 10:
                        return clean_text[:140]

            for line in lines:
                l_clean = line.strip()
                if re.search(r"\b(recommend|increase|bump|verify|check|provision|update|fix)\b", l_clean, re.I):
                    clean_text = re.sub(r"^[-*0-9.#]+\s*", "", l_clean).strip()
                    if len(clean_text) > 15:
                        return clean_text[:140]

    return ""


def filter_and_aggregate_events(
    dedup_entries: Dict[str, Any],
    config: Dict[str, Any],
    incidents: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Deterministically groups and summarizes what k8s-event-watcher processed today."""
    if incidents is None:
        incidents = []

    filters = config.get("filters", {})
    min_count = int(filters.get("min_event_count", 1))
    include_ns = set(filters.get("include_namespaces", []))
    exclude_ns = set(filters.get("exclude_namespaces", []))
    allowed_reasons = set(filters.get("allowed_reasons", []))

    total_seen = 0
    workload_map: Dict[str, Dict[str, Any]] = {}
    dispatched_sessions = set()

    for key_str, entry in dedup_entries.items():
        uid = key_str.split("|", 1)[0] if "|" in key_str else key_str
        reason = key_str.split("|", 1)[1] if "|" in key_str else "Unknown"
        count = int(entry.get("count", 1))

        ns = entry.get("namespace", "")
        pod_name = entry.get("name", uid[:12] if uid else "unknown-pod")

        if ns and ns in exclude_ns:
            continue
        if include_ns and ns not in include_ns:
            continue
        if allowed_reasons and reason not in allowed_reasons:
            continue

        total_seen += count

        if count < min_count:
            continue

        session_id = entry.get("session_id", "")
        if session_id:
            dispatched_sessions.add(session_id)

        workload = clean_workload_name(pod_name)
        group_key = f"{ns}/{workload}/{reason}"
        msg = sanitize_chat_message(entry.get("message", ""))
        actionable_fix = extract_actionable_fix_from_triage(workload, reason, msg, incidents)

        if group_key in workload_map:
            workload_map[group_key]["count"] += count
        else:
            workload_map[group_key] = {
                "key": key_str,
                "reason": reason,
                "namespace": ns or "default",
                "pod_name": pod_name,
                "workload": workload,
                "count": count,
                "message": msg,
                "actionable_fix": actionable_fix,
            }

    filtered_entries = list(workload_map.values())
    filtered_entries.sort(key=lambda x: x["count"], reverse=True)

    unique_incidents = len(filtered_entries)
    deduped_count = max(0, total_seen - unique_incidents)
    dedup_ratio = (deduped_count / total_seen * 100.0) if total_seen > 0 else 0.0

    return {
        "total_seen": total_seen,
        "unique_incidents": unique_incidents,
        "deduped_count": deduped_count,
        "dedup_ratio": dedup_ratio,
        "dispatched_sessions": len(dispatched_sessions),
        "completed_triages": len(incidents),
        "entries": filtered_entries,
    }


def generate_markdown_report(
    summary: Dict[str, Any],
    incidents: List[Dict[str, Any]],
    config: Dict[str, Any],
    cluster_name: Optional[str] = None,
    report_date: Optional[str] = None,
) -> str:
    """Renders a clean, chat-optimized markdown activity digest without awkward line breaks."""
    if not cluster_name:
        cluster_name = resolve_cluster_name(config=config)

    if not report_date:
        report_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    sections = config.get("sections", {})
    entries = summary.get("entries", [])
    entry_count = len(entries)

    lines: List[str] = []
    if entry_count > 0:
        lines.append(f"📊 **k8s-event-watcher Daily Activity Recap** — `{cluster_name}` ({report_date})")
        lines.append(
            f"*Today the watcher intercepted **{summary['total_seen']}** warning events, "
            f"suppressed **{summary['deduped_count']}** duplicates ({summary['dedup_ratio']:.1f}% noise reduction), "
            f"and dispatched **{summary['dispatched_sessions']}** incident alert sessions to the Platform Agent:*"
        )
        lines.append("")
        lines.append("---")

        if sections.get("workload_breakdown", True):
            lines.append("### 🚨 Incidents Intercepted by Watcher")
            for idx, e in enumerate(entries[:5], start=1):
                lines.append(f"{idx}. 🔴 **`{e['namespace']}/{e['workload']}`** (`{e['reason']}` • {e['count']}x)")
                if e.get("message"):
                    lines.append(f"   * **Issue:** {e['message']}")
                fix_text = e.get("actionable_fix") or "Autonomous triage report pending."
                lines.append(f"   * **Fix:** {fix_text}")
            lines.append("")
            lines.append("---")

        if sections.get("action_items", True):
            action_entries = [e for e in entries if e.get("actionable_fix")][:5]
            if action_entries:
                lines.append("### 🛠️ Action Items for SRE")
                for idx, e in enumerate(action_entries, start=1):
                    lines.append(f"{idx}. **`{e['namespace']}/{e['workload']}`:** {e['actionable_fix']}")
                lines.append("")

    else:
        lines.append(f"🟢 **k8s-event-watcher Daily Activity Recap** — `{cluster_name}` ({report_date})")
        lines.append(f"* **Total Warning Events Intercepted:** 0")
        lines.append(f"* **Deduplicated & Suppressed:** 0 (0.0% noise reduction)")
        lines.append(f"* **Alert Sessions Dispatched:** 0")
        lines.append("")
        lines.append("✅ *Watcher daemon active and streaming GKE events. Zero actionable warning events intercepted today.*")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic k8s-event-watcher Daily Activity Recap")
    parser.add_argument("--config", help="Path to eod_report_config.yaml")
    parser.add_argument("--dedup", help="Path to dedup.json")
    parser.add_argument("--db", help="Path to session_kv.db")
    parser.add_argument("--cluster-name", help="Cluster name override")
    args = parser.parse_args()

    cfg = load_config(args.config)
    dedup = load_dedup_data(args.dedup)
    incidents = load_incident_records(args.db)
    cluster = resolve_cluster_name(args.cluster_name, cfg)

    summary = filter_and_aggregate_events(dedup, cfg, incidents=incidents)
    report = generate_markdown_report(summary, incidents, cfg, cluster_name=cluster)
    print(report)


if __name__ == "__main__":
    main()
