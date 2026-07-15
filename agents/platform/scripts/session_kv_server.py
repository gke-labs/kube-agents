#!/usr/bin/env python3
"""Small HTTP resolver for platform session metadata."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from typing import Any, Dict

from fastapi import BackgroundTasks, FastAPI, HTTPException

app = FastAPI()

SESSION_KV_DB_PATH = os.getenv("SESSION_KV_DB_PATH", "/var/lib/kube-agents/session/session_kv.db")


def init_db() -> None:
    db_dir = os.path.dirname(SESSION_KV_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    with sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0) as conn:
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


@app.get("/healthz")
def healthz() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/sessions", status_code=201)
def create_session() -> Dict[str, str]:
    """Create a new session ID for the incoming incident."""
    session_id = f"k8s-evt-{uuid.uuid4().hex[:8]}"
    
    # Save the session to the local metadata DB
    with sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0) as conn:
        conn.execute(
            "INSERT INTO session_metadata (session_id, metadata) VALUES (?, ?)",
            (session_id, json.dumps({"platform": "k8s-watcher", "created_at": datetime.utcnow().isoformat()}))
        )
    return {"sessionID": session_id}


def trigger_agent_troubleshooter(session_id: str, alert_msg: str) -> None:
    """Post the warning alert to GChat, then call local gateway API to execute agent loop."""
    # 1. Trigger the red alert warning to Google Chat with --json to parse message_id
    thread_id = None
    try:
        res = subprocess.run(
            ["hermes", "send", "--json", "--to", "google_chat", alert_msg],
            check=True,
            capture_output=True,
            text=True
        )
        payload = json.loads(res.stdout)
        msg_id = payload.get("message_id", "")
        if msg_id:
            thread_id = msg_id.replace("/messages/", "/threads/")
    except Exception as exc:
        print(f"Failed to post warning alert or parse response: {exc}")

    # Update metadata DB with the parsed thread ID
    if thread_id:
        try:
            with sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0) as conn:
                row = conn.execute(
                    "SELECT metadata FROM session_metadata WHERE session_id = ?",
                    (session_id,)
                ).fetchone()
                if row:
                    meta = json.loads(row[0])
                    meta["thread_id"] = thread_id
                    meta["chat_id"] = thread_id.split("/threads/")[0]
                    conn.execute(
                        "UPDATE session_metadata SET metadata = ? WHERE session_id = ?",
                        (json.dumps(meta), session_id)
                    )
        except Exception as exc:
            print(f"Failed to update session metadata with thread_id: {exc}")

    # 2. Call local gateway API to run troubleshooter
    api_url = "http://localhost:8642"
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("API_SERVER_KEY", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    # Create session inside gateway if it doesn't exist
    try:
        req = urllib.request.Request(
            f"{api_url}/api/sessions",
            data=json.dumps({"session_id": session_id, "title": f"Triage {session_id}"}).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            pass
    except urllib.error.HTTPError as exc:
        if exc.code != 409:  # 409 Conflict means it already exists, which is fine
            print(f"Failed to create gateway API session (code {exc.code}): {exc.read().decode()}")
            return
    except Exception as exc:
        print(f"Failed to connect to gateway API server: {exc}")
        return

    # Trigger agent execution turn in the session
    agent_query = (
        f"Analyze the following Kubernetes event warning on GKE cluster '{os.environ.get('GKE_CLUSTER_NAME', 'platform-agent-host')}' "
        f"and perform root-cause analysis (inspect logs, describe the resource, check configuration issues, etc.).\n\n"
        f"When done, post your final diagnostic report to Google Chat (using your notification tool) formatted exactly like this:\n\n"
        f"🛠️ *Incident Triage Report* 🛠️\n\n"
        f"*1. Summary of Issue:*\n"
        f"<Brief description of what went wrong>\n\n"
        f"*2. Root-Cause Analysis:*\n"
        f"<Detailed findings, highlighting specific configuration values, log lines, or error codes>\n\n"
        f"*3. Actionable Remediation:*\n"
        f"<Clear, step-by-step commands or actions to fix the issue>\n\n"
        f"🔗 *Observability & Debugging Links:*\n"
        f"• [GKE Workload Console](https://console.cloud.google.com/kubernetes/workload/overview?project={os.environ.get('GCP_PROJECT', 'jayantid-gkedemos')})\n"
        f"• [Cloud Logging Console](https://console.cloud.google.com/logs/query;query=resource.type%3D%22k8s_container%22?project={os.environ.get('GCP_PROJECT', 'jayantid-gkedemos')})\n\n"
        f"---"
    )
    try:
        req = urllib.request.Request(
            f"{api_url}/api/sessions/{session_id}/chat",
            data=json.dumps({"message": agent_query}).encode("utf-8"),
            headers=headers,
            method="POST"
        )
        with urllib.request.urlopen(req) as resp:
            if resp.status != 200:
                print(f"Gateway API chat execution failed (status {resp.status})")
    except Exception as exc:
        print(f"Failed to call gateway API chat execution: {exc}")


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
        
    event_reason = payload.get("reason", "Unknown")
    namespace = payload.get("namespace", "default")
    object_kind = payload.get("kindOfObject", "Pod")
    object_name = payload.get("name", "")
    message = payload.get("message", "")
    count = payload.get("count", 1)
    
    # Construct a pretty notification alert
    alert_msg = (
        f"🚨 *Kubernetes Event Alert* 🚨\n\n"
        f"• *Resource:* `{namespace}/{object_kind}/{object_name}`\n"
        f"• *Event Reason:* `{event_reason}`\n"
        f"• *Warning Message:* {message}\n"
        f"• *Occurrence Count:* {count}\n\n"
        f"🔍 _Starting autonomous troubleshooting run inside GKE cluster..._"
    )
    
    # Delegate the heavy REST API call to FastAPI BackgroundTasks to keep response times sub-millisecond
    background_tasks.add_task(trigger_agent_troubleshooter, session_id, alert_msg)
    
    return {"status": "injected"}


@app.get("/v1/sessions/{session_id}/metadata")
def get_metadata(session_id: str) -> Dict[str, Any]:
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    with sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0) as conn:
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
    with sqlite3.connect(SESSION_KV_DB_PATH, timeout=5.0) as conn:
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
