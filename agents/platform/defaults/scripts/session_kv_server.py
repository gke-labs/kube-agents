from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import sqlite3
import os
import json

app = FastAPI()
DB_PATH = "/opt/data/session_kv.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS session_metadata (
            session_id TEXT PRIMARY KEY,
            metadata TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

init_db()

class MetadataPayload(BaseModel):
    metadata: dict

@app.post("/v1/sessions/{session_id}/metadata")
def set_metadata(session_id: str, payload: MetadataPayload):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    try:
        c.execute(
            "INSERT OR REPLACE INTO session_metadata (session_id, metadata, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (session_id, json.dumps(payload.metadata))
        )
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=500, detail=f"Database write failure: {e}")
    conn.close()
    return {"status": "success"}

@app.get("/v1/sessions/{session_id}/metadata")
def get_metadata(session_id: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT metadata FROM session_metadata WHERE session_id = ?", (session_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Session metadata not found")
    try:
        return json.loads(row[0])
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Data decoding failure: {e}")


@app.get("/v1/sessions")
def list_sessions():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT session_id, metadata, updated_at FROM session_metadata ORDER BY updated_at DESC")
    rows = c.fetchall()
    conn.close()
    
    sessions = []
    for row in rows:
        try:
            meta = json.loads(row[1])
        except Exception:
            meta = {}
        sessions.append({
            "session_id": row[0],
            "metadata": meta,
            "updated_at": row[2]
        })
    return sessions

