#!/usr/bin/env python3
"""Small HTTP resolver for platform session metadata."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import uuid
from datetime import datetime
from typing import Any, Dict

from fastapi import FastAPI, HTTPException

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


@app.post("/sessions/{session_id}/inject")
def inject_message(session_id: str, request_data: Dict[str, Any]) -> Dict[str, str]:
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
    
    # Construct a notification alert
    alert_msg = (
        f"🚨 *[K8s Event Warning]*\n"
        f"*Reason:* {event_reason}\n"
        f"*Resource:* {namespace}/{object_kind}/{object_name}\n"
        f"*Message:* {message}\n"
        f"*Count:* {count}\n"
        f"Starting autonomous troubleshooting run..."
    )
    
    # Trigger the agent using the native 'hermes' command-line interface
    try:
        subprocess.run(
            ["hermes", "send", "--to", "google_chat", alert_msg],
            check=True,
            capture_output=True,
            text=True
        )
    except subprocess.CalledProcessError as exc:
        print(f"Failed to dispatch to agent: {exc.stderr}")
        raise HTTPException(status_code=500, detail=f"Failed to dispatch to agent: {exc.stderr}")
        
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
