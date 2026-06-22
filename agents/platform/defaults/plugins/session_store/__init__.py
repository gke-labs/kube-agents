import logging
import json
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes.plugin.session_store")

def log_event_to_db(
    event: Any, 
    gateway: Any, 
    session_store: Any, 
    **kwargs: Any
) -> Optional[Dict[str, str]]:
    """Gateway pre-dispatch hook handler."""
    try:
        source = event.source
        
        # 1. Resolve Session ID
        session_entry = session_store.get_or_create_session(source)
        session_id = session_entry.session_id
        
        # 2. Extract User Identity (Google Chat maps user email to user_id)
        user_email = source.user_id or "unknown_email"
        chat_id = source.chat_id or ""
        thread_id = source.thread_id or ""
        
        logger.info(
            "Logging incoming GChat event: User=%s, Session=%s, ChatID=%s, ThreadID=%s, TextLength=%d",
            user_email, session_id, chat_id, thread_id, len(event.text or "")
        )
        
        # 3. Write metadata to local KV store sidecar (port 8699)
        write_session_metadata(session_id, user_email, chat_id, thread_id)
        
    except Exception as exc:
        logger.error("Error in session_store pre_gateway_dispatch hook: %s", exc, exc_info=True)

    # Continue normal message dispatch
    return None


import sqlite3

def write_session_metadata(session_id: str, email: str, chat_id: str, thread_id: str):
    """Write session user_email metadata directly to the SQLite database."""
    db_path = "/opt/data/session_kv.db"
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=5.0)
        c = conn.cursor()
        
        # Ensure table is initialized in case plugin starts before the sidecar server
        c.execute("""
            CREATE TABLE IF NOT EXISTS session_metadata (
                session_id TEXT PRIMARY KEY,
                metadata TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        k8s_host = os.getenv("KUBERNETES_SERVICE_HOST", "")
        metadata_json = json.dumps({
            "user_email": email,
            "KUBERNETES_SERVICE_HOST": k8s_host,
            "google_chat_id": chat_id,
            "google_thread_id": thread_id
        })
        c.execute(
            "INSERT OR REPLACE INTO session_metadata (session_id, metadata, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)",
            (session_id, metadata_json)
        )
        # Prune records older than 7 days to keep database size bounded
        c.execute(
            "DELETE FROM session_metadata WHERE updated_at < datetime('now', '-7 days')"
        )
        conn.commit()
    except Exception as e:
        logger.error("Failed to write metadata directly to SQLite for session %s: %s", session_id, e)
    finally:
        if conn:
            conn.close()
def register(ctx: Any) -> None:
    """Register the pre_gateway_dispatch hook."""
    ctx.register_hook("pre_gateway_dispatch", log_event_to_db)


