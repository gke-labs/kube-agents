#!/usr/bin/env python3
"""Small HTTP resolver for platform session metadata."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Dict
from contextlib import closing

import logging

from fastapi import BackgroundTasks, FastAPI, HTTPException
from agent_common_server import _run_env, CONFIG_PATH, DOTENV_PATH, STATE_DB_PATH

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger("session_kv_server")

try:
    import dotenv
    dotenv.load_dotenv(DOTENV_PATH)
except Exception:
    pass

app = FastAPI()

SESSION_KV_DB_PATH = os.getenv("SESSION_KV_DB_PATH", "/var/lib/kube-agents/session/session_kv.db")


def init_db() -> None:
    db_dir = os.path.dirname(SESSION_KV_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        with conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_metadata (
                    session_id TEXT PRIMARY KEY,
                    metadata TEXT NOT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )


# Columns in hermes's gateway_routing table that this service owns and is
# allowed to write. Every other column belongs to hermes and is inherited
# verbatim from a real hermes-written row (see template logic below) so we
# never persist a row shape hermes cannot route.
_GATEWAY_OWNED_COLUMNS = ("scope", "session_key", "entry_json", "updated_at")

# Prefix of session IDs minted by this service (see create_session). We only
# ever create or update gateway_routing rows for our own sessions.
_OUR_SESSION_PREFIX = "k8s-evt-"


def register_gateway_routing(session_id: str, platform: str, chat_id: str, thread_id: str) -> None:
    """Register a thread->session mapping in hermes's gateway_routing table.

    This writes into a SQLite database owned by the external hermes service, so
    it is deliberately defensive:
      * It never clobbers a row it does not own (scoped to ``k8s-evt-`` sessions).
      * It inherits any hermes-owned columns from a real existing row rather than
        guessing their shape.
      * It uses a generous busy_timeout and leaves hermes's journal mode intact
        so it queues behind hermes's writers instead of failing on a locked DB.
    """
    gateway_db = STATE_DB_PATH
    if not os.path.exists(gateway_db):
        logger.warning(f"Gateway DB not found at {gateway_db}; skipping routing registration.")
        return

    import time
    now_iso = datetime.now(timezone.utc).isoformat()
    scope = os.environ.get("PLATFORM_AGENT_SESSIONS_DIR", "/opt/data/sessions")
    session_key = f"agent:main:{platform}:group:{chat_id}:{thread_id}"

    entry = {
        "session_key": session_key,
        "session_id": session_id,
        "created_at": now_iso,
        "updated_at": now_iso,
        "display_name": chat_id,
        "platform": platform,
        "chat_type": "group",
        "origin": {
            "platform": platform,
            "chat_id": chat_id,
            "chat_name": chat_id,
            "chat_type": "group",
            "thread_id": thread_id
        }
    }

    # Values for the columns we own. Everything else is inherited from a real row.
    owned = {
        "scope": scope,
        "session_key": session_key,
        "entry_json": json.dumps(entry),
        "updated_at": time.time(),
    }

    try:
        # Fix 3: shared live DB. A generous busy_timeout makes us queue behind
        # hermes's writers instead of erroring with "database is locked". We do
        # NOT set journal_mode here — leave whatever hermes configured on the
        # file intact (match hermes, don't fight it).
        with closing(sqlite3.connect(gateway_db, timeout=5.0)) as conn:
            conn.execute("PRAGMA busy_timeout=5000")
            with conn:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(gateway_routing)").fetchall()]
                if not cols:
                    logger.error("gateway_routing table missing; refusing to write routing entry.")
                    return
                missing = [c for c in owned if c not in cols]
                if missing:
                    logger.error(
                        f"gateway_routing schema drift: expected columns {missing} not found in {cols}. "
                        f"Refusing to write to avoid corrupting hermes state."
                    )
                    return

                # Fix 1 (scoping): never overwrite a row we do not own. hermes
                # creates routing rows for human-initiated threads; clobbering one
                # would hijack a real user's conversation.
                existing = conn.execute(
                    "SELECT entry_json FROM gateway_routing WHERE session_key = ?",
                    (session_key,),
                ).fetchone()
                if existing is not None:
                    try:
                        owner = (json.loads(existing[0]) or {}).get("session_id", "")
                    except Exception:
                        owner = ""
                    if not owner.startswith(_OUR_SESSION_PREFIX):
                        logger.error(
                            f"Refusing to overwrite gateway_routing[{session_key!r}] "
                            f"owned by {owner or 'unknown/hermes'}; not a {_OUR_SESSION_PREFIX} session."
                        )
                        return

                # Fix 2 (template-from-a-real-row): inherit hermes's exact values
                # for any column we don't own, preferring a row for the same
                # platform. This keeps our synthetic row conformant even if hermes
                # adds columns we don't know about.
                insert_cols = list(owned.keys())
                insert_vals = [owned[c] for c in insert_cols]
                non_owned = [c for c in cols if c not in owned]
                if non_owned:
                    template = conn.execute(
                        f"SELECT {', '.join(non_owned)} FROM gateway_routing "
                        f"WHERE session_key LIKE ? ORDER BY rowid DESC LIMIT 1",
                        (f"agent:main:{platform}:%",),
                    ).fetchone()
                    if template is None:
                        template = conn.execute(
                            f"SELECT {', '.join(non_owned)} FROM gateway_routing "
                            f"ORDER BY rowid DESC LIMIT 1"
                        ).fetchone()
                    if template is None:
                        logger.warning(
                            "No existing gateway_routing row to use as a template; writing "
                            "owned columns only. Verify the row shape against a real hermes "
                            "row before relying on reply routing."
                        )
                    else:
                        for c, v in zip(non_owned, template):
                            insert_cols.append(c)
                            insert_vals.append(v)

                # Fix 1: ON CONFLICT DO UPDATE (not INSERT OR REPLACE, which is a
                # delete+reinsert that would wipe any hermes-managed columns). Only
                # our owned columns are refreshed on conflict.
                update_set = ", ".join(f"{c}=excluded.{c}" for c in owned if c != "session_key")
                conn.execute(
                    f"INSERT INTO gateway_routing ({', '.join(insert_cols)}) "
                    f"VALUES ({', '.join('?' for _ in insert_cols)}) "
                    f"ON CONFLICT(session_key) DO UPDATE SET {update_set}",
                    tuple(insert_vals),
                )
                logger.info(f"Registered gateway routing for session {session_id} on {platform} thread {thread_id}")
    except Exception as exc:
        logger.error(f"Failed to insert gateway routing entry: {exc}")



@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/sessions", status_code=201)
def create_session() -> Dict[str, str]:
    """Create a new session ID for the incoming incident."""
    session_id = f"k8s-evt-{uuid.uuid4().hex[:8]}"
    
    # Save the session to the local metadata DB
    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
                (session_id, json.dumps({"platform": "k8s-watcher", "created_at": datetime.now(timezone.utc).isoformat()}))
            )
    return {"sessionID": session_id}

def clean_workload_name(kind: str, name: str) -> str:
    if kind.lower() == "pod":
        # Match pattern of deployment replica (e.g. -6cfdb6b98b-zwv24)
        m = re.match(r"^(.*?)-[a-f0-9]{8,10}-[a-z0-9]{5}$", name)
        if m:
            return m.group(1)
        # Match pattern of statefulset/job/pod replica (e.g. -0 or -abcde)
        m = re.match(r"^(.*?)-[a-z0-9]{5}$", name)
        if m:
            return m.group(1)
    return name


def clean_reason_label(reason: str) -> str:
    # E.g. FailedToDrainNode -> Failed to drain node
    s = re.sub(r'(?<!^)(?=[A-Z])', ' ', reason).lower()
    return s.capitalize()


def clean_event_message(message: str) -> str:
    msg = message.replace("PodDisruptionBudget", "PDB")
    # Simplify PDB eviction violation message:
    m = re.search(r"cannot be evicted:\s*(would violate PDB\s+(?:[^/]+/)?([a-zA-Z0-9_-]+))", msg)
    if m:
        clean_pdb = m.group(2)
        return f"Eviction would violate PDB {clean_pdb}"
    return msg


def get_severity_details(event_type: str, reason: str) -> tuple[str, str]:
    event_lower = event_type.lower()
    reason_lower = reason.lower()
    
    # Blocker if it blocks drain, eviction, or scheduling
    is_blocker = (
        event_lower == "warning" and 
        any(x in reason_lower for x in ("drain", "evict", "schedul", "capacity", "oomkilled", "crashloopbackoff", "failedmount"))
    )
    
    if is_blocker:
        return "🔴", "Critical"
    elif event_lower == "warning":
        return "🟡", "Warning"
    else:
        return "🔵", "Info"



def get_active_platform() -> str:
    try:
        import yaml
        with open(CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
        platforms = cfg.get("platforms", {})
        if platforms.get("slack", {}).get("enabled"):
            return "slack"
        if platforms.get("google_chat", {}).get("enabled"):
            return "google_chat"
    except Exception as exc:
        logger.error(f"Failed to parse config.yaml for active platform: {exc}")
    if os.environ.get("SLACK_BOT_TOKEN"):
        return "slack"
    return "google_chat"


def _post_initial_alert(active_platform: str, alert_msg: str) -> str | None:
    """Send initial warning alert via hermes CLI and return the thread/message ID."""
    try:
        res = subprocess.run(
            ["hermes", "send", "--json", "--to", active_platform, alert_msg],
            check=True,
            capture_output=True,
            text=True,
            env=_run_env()
        )
        resp = json.loads(res.stdout)
        msg_id = resp.get("message_id", "")
        if msg_id:
            # Google Chat message IDs contain space and message parts; we extract the thread key.
            if active_platform == "google_chat" and "/messages/" in msg_id:
                space_part, msg_part = msg_id.split("/messages/", 1)
                thread_key = msg_part.split(".")[0]
                return f"{space_part}/threads/{thread_key}"
            return msg_id
    except subprocess.CalledProcessError as exc:
        logger.error(f"Failed to post warning alert. Stdout: {exc.stdout}. Stderr: {exc.stderr}. Exc: {exc}")
    except Exception as exc:
        logger.error(f"Failed to post warning alert or parse message_id response: {exc}")
    return None


def _register_session_routing(session_id: str, platform: str, thread_id: str) -> None:
    """Save thread configurations in session_metadata SQLite table and register routing in Gateway state.db."""
    try:
        with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
            with conn:
                row = conn.execute(
                    "SELECT metadata FROM session_metadata WHERE session_id = ?",
                    (session_id,)
                ).fetchone()
                if row:
                    meta = json.loads(row[0])
                    meta["thread_id"] = thread_id
                    if platform == "slack":
                        meta["chat_id"] = os.environ.get("SLACK_HOME_CHANNEL", "")
                    else:
                        meta["chat_id"] = thread_id.split("/threads/")[0]
                    
                    # Update SQLite metadata table
                    conn.execute(
                        "UPDATE session_metadata SET metadata = ? WHERE session_id = ?",
                        (json.dumps(meta), session_id)
                    )
                    # Register mapping in gateway's state.db to enable two-way routing of chat replies
                    register_gateway_routing(session_id, platform, meta["chat_id"], thread_id)
    except Exception as exc:
        logger.error(f"Failed to update session metadata with thread_id: {exc}")


def _create_gateway_session(api_url: str, session_id: str, headers: Dict[str, str]) -> bool:
    """POST request to local gateway API to initialize the troubleshooting session ID."""
    try:
        req = urllib.request.Request(
            f"{api_url}/api/sessions",
            data=json.dumps({"session_id": session_id, "title": f"Triage {session_id}"}).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return True
    except urllib.error.HTTPError as exc:
        if exc.code == 409:  # 409 Conflict means it already exists, which is acceptable
            return True
        logger.error(f"Failed to create gateway API session (code {exc.code}): {exc.read().decode()}")
    except Exception as exc:
        logger.error(f"Failed to connect to gateway API server: {exc}")
    return False


def _build_agent_query(session_id: str, payload: Dict[str, Any]) -> str:
    """Format a detailed Markdown diagnostic query for the Platform Agent."""
    event_reason = payload.get("reason") or "Unknown"
    namespace = payload.get("namespace") or "default"
    object_kind = payload.get("kind_of_object") or payload.get("kindOfObject") or "Pod"
    object_name = payload.get("name") or ""
    message = payload.get("message") or ""
    cluster_name = os.environ.get("GKE_CLUSTER_NAME", "platform-agent-host")

    return (
        f"Analyze the following Kubernetes event warning on GKE cluster '{cluster_name}' "
        f"for the active session '{session_id}'.\n\n"
        f"**Event Details:**\n"
        f"• *Resource:* {namespace}/{object_kind}/{object_name}\n"
        f"• *Event Reason:* {event_reason}\n"
        f"• *Warning Message:* {message}\n\n"
        f"When calling your send_notification tool to report findings, you MUST pass this exact session ID: '{session_id}' as the session_id argument so it routes as a threaded reply to the warning alert.\n\n"
        f"When done, post your final diagnostic report to the chat platform (using your notification tool) formatted exactly like this:\n\n"
        f"📋 *Incident Triage*\n\n"
        f"• *Issue:* <Short 1-sentence description of the problem>\n"
        f"• *Root Cause:* <Key constraint mismatch or log finding in 1-2 sentences>\n\n"
        f"🛠️ *Proposed Fixes (GitOps):*\n"
        f"*Option A (<Action Title>):* <1-sentence description of Option A GitOps fix>.\n"
        f"*Option B (<Action Title>):* <1-sentence description of Option B GitOps fix>.\n\n"
        f"🔗 [GKE Workloads](https://console.cloud.google.com/kubernetes/workload/overview?project={os.environ.get('GCP_PROJECT', 'jayantid-gkedemos')}) | "
        f"[Cloud Logs](https://console.cloud.google.com/logs/query;query=resource.type%3D%22k8s_container%22?project={os.environ.get('GCP_PROJECT', 'jayantid-gkedemos')})\n\n"
        f"👉 *Reply to this thread with 'apply Option A' or 'apply Option B' to automatically open a GitOps Pull Request with the fix.*\n\n"
        f"---"
        f"\n\n**GitOps PR Instructions (For subsequent turns if the user replies):**\n"
        f"If the user replies to the thread with 'apply Option A' or 'apply Option B':\n"
        f"1. You are explicitly authorized to create a new branch, modify the resource manifests in the local checkout, commit, push, and open a GitHub Pull Request matching the selected option.\n"
        f"2. Post a threaded response confirming the PR was created and include the clickable PR link.\n"
        f"3. Do not execute any write mutations (kubectl scale, patch, or apply) directly on the live cluster."
    )


def _start_agent_turn(api_url: str, session_id: str, query: str, headers: Dict[str, str]) -> None:
    """Post the agent query request to execute the diagnostic reasoning loop."""
    try:
        req = urllib.request.Request(
            f"{api_url}/api/sessions/{session_id}/chat",
            data=json.dumps({"message": query}).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=300.0) as resp:
            if resp.status != 200:
                logger.error(f"Gateway API chat execution failed (status {resp.status})")
    except Exception as exc:
        logger.error(f"Failed to call gateway API chat execution: {exc}")


def trigger_agent_troubleshooter(session_id: str, alert_msg: str, payload: Dict[str, Any]) -> None:
    """Post warning alert to Chat, configure thread mapping, and trigger the agent loop in background."""
    active_platform = get_active_platform()
    
    # 1. Post initial warning notification to Google Chat or Slack
    thread_id = _post_initial_alert(active_platform, alert_msg)
    
    # 2. Register thread-to-session mappings for two-way chat routing
    if thread_id:
        _register_session_routing(session_id, active_platform, thread_id)

    # 3. Configure HTTP authentication headers for Hermes REST gateway
    api_url = os.environ.get("PLATFORM_API_URL", "http://localhost:8642")
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("API_SERVER_KEY", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # 4. Instantiate the session in Platform Gateway
    session_created = _create_gateway_session(api_url, session_id, headers)
    if not session_created:
        logger.error(f"Aborting troubleshooting trigger: session creation failed for {session_id}")
        return

    # 5. Formulate instructions query and execute the agent turn
    agent_query = _build_agent_query(session_id, payload)
    _start_agent_turn(api_url, session_id, agent_query, headers)


@app.post("/sessions/{session_id}/inject")
def inject_message(session_id: str, request_data: Dict[str, Any], background_tasks: BackgroundTasks) -> Dict[str, str]:
    """Receive the event payload and notify the Platform Agent via Google Chat."""
    raw_message = request_data.get("message", "")
    if not raw_message:
        raise HTTPException(status_code=400, detail="message field is required")
        
    try:
        payload = json.loads(raw_message)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to parse inner payload JSON: {exc}")
        
    event_reason = payload.get("reason") or "Unknown"
    namespace = payload.get("namespace") or "default"
    object_kind = payload.get("kind_of_object") or payload.get("kindOfObject") or "Pod"
    object_name = payload.get("name") or ""
    message = payload.get("message") or ""
    count = payload.get("count") if payload.get("count") is not None else 1
    event_type = payload.get("type") or "Warning"

    severity_emoji, severity_label = get_severity_details(event_type, event_reason)
    clean_name = clean_workload_name(object_kind, object_name)
    clean_reason = clean_reason_label(event_reason)
    clean_msg = clean_event_message(message)

    # Construct a pretty notification alert
    alert_msg = (
        f"{severity_emoji} *{severity_label}:* {clean_reason} `{namespace}/{clean_name}` — {clean_msg}\n"
        f"🌱 _Digging down to the root cause..._"
    )
    
    # Delegate the heavy REST API call to FastAPI BackgroundTasks to keep response times sub-millisecond
    background_tasks.add_task(trigger_agent_troubleshooter, session_id, alert_msg, payload)
    
    return {"status": "injected"}


@app.get("/v1/sessions/{session_id}/metadata")
def get_metadata(session_id: str) -> Dict[str, Any]:
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        row = conn.execute(
            "SELECT metadata FROM session_metadata WHERE session_id = ?",
            (session_id,),
        ).fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Session metadata not found")

    try:
        return json.loads(row[0])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Data decoding failure: {exc}")


@app.get("/v1/sessions")
def list_sessions(limit: int = 100) -> Dict[str, Any]:
    limit = max(1, min(limit, 1000))
    with closing(sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0)) as conn:
        rows = conn.execute(
            """
            SELECT session_id, metadata, updated_at
            FROM session_metadata
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    sessions = []
    for session_id, metadata, updated_at in rows:
        try:
            parsed = json.loads(metadata)
        except Exception:
            parsed = {}
        sessions.append(
            {
                "session_id": session_id,
                "metadata": parsed,
                "updated_at": updated_at,
            }
        )
    return {"sessions": sessions}


init_db()
