import os
import json
import sqlite3
import logging
from datetime import datetime, timezone, timedelta
from typing import List, Optional

try:
    import google.auth
    from googleapiclient.discovery import build
    HAS_GOOGLE_AUTH = True
except ImportError:
    HAS_GOOGLE_AUTH = False

logger = logging.getLogger("kube_agents.identity_resolver")

DEFAULT_DB_PATH = os.getenv("SESSION_KV_DB_PATH", "/var/lib/kube-agents/session/session_kv.db")
CACHE_TTL_MINUTES = 10


class CloudIdentityResolver:
    """Resolves and caches Google Workspace / Cloud Identity group memberships."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or os.getenv("SESSION_KV_DB_PATH", DEFAULT_DB_PATH)
        self._init_db()

    def _init_db(self) -> None:
        db_dir = os.path.dirname(self.db_path)
        try:
            if db_dir:
                os.makedirs(db_dir, exist_ok=True)
            with sqlite3.connect(self.db_path, timeout=5.0) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_group_cache (
                        user_email TEXT PRIMARY KEY,
                        groups_json TEXT NOT NULL,
                        expires_at TIMESTAMP NOT NULL
                    )
                """)
        except Exception as exc:
            logger.warning("Failed to initialize group cache database at %s: %s", self.db_path, exc)

    def get_user_groups(self, user_email: str, force_refresh: bool = False) -> List[str]:
        groups, _ = self.get_user_groups_with_expiration(user_email, force_refresh=force_refresh)
        return groups

    def get_user_groups_with_expiration(self, user_email: str, force_refresh: bool = False) -> tuple[List[str], str]:
        if not user_email:
            return [], ""

        if not force_refresh:
            cached = self._get_cached_groups_with_expiration(user_email)
            if cached is not None:
                return cached

        groups = self._query_cloud_identity(user_email)
        expires_at = self._set_cached_groups(user_email, groups)
        return groups, expires_at

    def _get_cached_groups_with_expiration(self, user_email: str) -> Optional[tuple[List[str], str]]:
        try:
            with sqlite3.connect(self.db_path, timeout=2.0) as conn:
                row = conn.execute(
                    "SELECT groups_json, expires_at FROM user_group_cache WHERE user_email = ?",
                    (user_email,)
                ).fetchone()
                if row:
                    groups_json, expires_at_str = row
                    expires_at = datetime.fromisoformat(expires_at_str)
                    if datetime.now(timezone.utc) < expires_at:
                        return json.loads(groups_json), expires_at_str
        except Exception as exc:
            logger.warning("Group cache read failure for %s: %s", user_email, exc)
        return None

    def _set_cached_groups(self, user_email: str, groups: List[str]) -> str:
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=CACHE_TTL_MINUTES)).isoformat()
        try:
            with sqlite3.connect(self.db_path, timeout=2.0) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO user_group_cache (user_email, groups_json, expires_at) VALUES (?, ?, ?)",
                    (user_email, json.dumps(groups), expires_at)
                )
        except Exception as exc:
            logger.warning("Group cache write failure for %s: %s", user_email, exc)
        return expires_at

    def _query_cloud_identity(self, user_email: str) -> List[str]:
        if not HAS_GOOGLE_AUTH:
            logger.warning("google.auth or googleapiclient not installed; skipping Cloud Identity query for %s", user_email)
            return []

        try:
            credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-identity.groups.readonly"]
            )
            service = build("cloudidentity", "v1", credentials=credentials)
            response = service.groups().memberships().searchTransitiveGroups(
                parent="groups/-",
                query=f"member_key_id == '{user_email}'"
            ).execute()

            groups = []
            for item in response.get("memberships", []):
                group_id = item.get("groupKey", {}).get("id")
                if group_id:
                    groups.append(group_id)
            return groups
        except Exception as exc:
            logger.error("Failed to query Cloud Identity API for %s: %s", user_email, exc)
            return []
