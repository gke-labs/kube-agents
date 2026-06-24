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

@app.get("/v1/sessions/{session_id}/metadata")
def get_metadata(session_id: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT metadata FROM session_metadata WHERE session_id = ?", (session_id,))
    row = c.fetchone()
    conn.close()
    
    if row:
        try:
            return json.loads(row[0])
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Data decoding failure: {e}")

    # Metadata not found for current session. Check if it's a subagent session with a parent in state.db
    state_db_path = "/opt/data/state.db"
    parent_id = None
    if os.path.exists(state_db_path):
        try:
            conn_state = sqlite3.connect(state_db_path)
            c_state = conn_state.cursor()
            c_state.execute("SELECT parent_session_id FROM sessions WHERE id = ?", (session_id,))
            parent_row = c_state.fetchone()
            conn_state.close()
            if parent_row and parent_row[0]:
                parent_id = parent_row[0]
        except Exception as e:
            print(f"Error querying state.db for parent session of {session_id}: {e}")

    if parent_id:
        print(f"Resolving metadata recursively for parent session {parent_id} of session {session_id}")
        return get_metadata(parent_id)

    raise HTTPException(status_code=404, detail="Session metadata not found")


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

