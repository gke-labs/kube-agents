#!/usr/bin/env python3
"""Small HTTP resolver for platform session metadata."""

from __future__ import annotations

import json
import os
import sqlite3
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
