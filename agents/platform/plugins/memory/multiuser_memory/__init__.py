import hashlib
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from agent.memory_provider import MemoryProvider
from tools.registry import tool_error
from utils import atomic_replace

logger = logging.getLogger(__name__)

ENTRY_DELIMITER = "\n§\n"

MEMORY_TOOL_SCHEMA = {
    "name": "multiuser_memory",
    "description": "Read, add, replace, or remove shared environment instructions and SOPs, or personal user profile notes.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "add", "replace", "remove"],
                "description": "What to do: 'read' entries, 'add' a new entry, 'replace' an entry, or 'remove' an entry."
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "'memory' for shared system-wide SOPs; 'user' for personal preferences specific to this user."
            },
            "content": {"type": "string", "description": "The text entry to add (for 'add')."},
            "old_content": {"type": "string", "description": "The exact old text entry to replace (for 'replace')."},
            "new_content": {"type": "string", "description": "The new text entry (for 'replace')."},
        },
        "required": ["action", "target"],
    },
}

SHARED_SESSION_NOTICE = (
    "Personal memory is unavailable in this conversation. It is a shared thread that "
    "more than one person can post in, and the harness cannot attribute a message to "
    "its sender here, so nothing may be written to or read from a personal store. "
    "Use target='memory' only for facts that are genuinely shared; personal memory "
    "works in a direct message."
)


def _thread_sessions_are_per_user() -> bool:
    """Best-effort read of the gateway's ``thread_sessions_per_user`` setting.

    The gateway accepts the key at the top level of config.yaml or under
    ``gateway:`` (gateway/config.py). Upstream default is False.
    """
    try:
        from hermes_cli.config import load_config
        config = load_config() or {}
        for section in (config, config.get("gateway")):
            if isinstance(section, dict) and "thread_sessions_per_user" in section:
                return bool(section["thread_sessions_per_user"])
    except Exception as e:
        logger.debug("Could not read thread_sessions_per_user, assuming shared: %s", e)
    return False


class MultiUserFileMemoryProvider(MemoryProvider):
    """Memory provider that isolates USER.md per user_id while keeping MEMORY.md global."""

    def __init__(self):
        self._hermes_home: Optional[Path] = None
        self._user_id: str = "default"
        self._session_is_shared: bool = False

    @property
    def name(self) -> str:
        return "multiuser_memory"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        hermes_home_str = kwargs.get("hermes_home")
        self._hermes_home = Path(hermes_home_str) if hermes_home_str else Path("/opt/data")
        raw_user = kwargs.get("user_id") or "default"
        # Sanitize user_id for safe filesystem path and append a hash to prevent collisions
        sanitized = "".join(c if c.isalnum() or c in "-_." else "_" for c in raw_user).strip("_")
        user_hash = hashlib.sha256(raw_user.encode("utf-8")).hexdigest()[:12]
        self._user_id = f"{sanitized}_{user_hash}" if sanitized else f"default_{user_hash}"

        # Refuse the personal store when the session can carry more than one human.
        #
        # A fresh provider instance is built per Agent (plugins/memory/__init__.py
        # re-runs register() on every load_memory_provider call) and the gateway
        # caches one Agent per session key, so `self._user_id` is not shared across
        # concurrent users. What IS shared is the session itself: agent._user_id is
        # frozen once at construction (agent/agent_init.py), and build_session_key()
        # (gateway/session.py) deliberately omits the participant id inside a thread
        # unless `thread_sessions_per_user` is on — "threads are shared across all
        # participants". So in a shared thread the second speaker reuses the first
        # speaker's cached Agent, and this provider would hydrate person A's private
        # entries into person B's prompt and file B's writes under A's name.
        #
        # The provider cannot tell who is speaking (system_prompt_block() takes no
        # arguments, and handle_tool_call() is passed no identity), so it fails
        # closed: no personal reads, no personal writes. The shared store, which is
        # visible to everyone by design, is unaffected.
        chat_type = str(kwargs.get("chat_type") or "").strip().lower()
        self._session_is_shared = bool(
            chat_type
            and chat_type != "dm"
            and kwargs.get("thread_id")
            and not _thread_sessions_are_per_user()
        )
        if self._session_is_shared:
            logger.info(
                "multiuser_memory: personal store disabled for session %s "
                "(shared %s thread — sender cannot be attributed)",
                session_id, chat_type,
            )

    def _path_for(self, target: str) -> Path:
        mem_dir = self._hermes_home / "memories"
        if target == "user":
            user_dir = mem_dir / "users"
            user_dir.mkdir(parents=True, exist_ok=True)
            return user_dir / f"{self._user_id}.md"
        return mem_dir / "MEMORY.md"

    def _read_entries(self, target: str) -> List[str]:
        path = self._path_for(target)
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8").strip()
            if not text:
                return []
            return [e.strip() for e in text.split(ENTRY_DELIMITER) if e.strip()]
        except Exception as e:
            logger.error("Failed reading memory file %s: %s", path, e)
            return []

    def _write_entries(self, target: str, entries: List[str]) -> None:
        path = self._path_for(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = ENTRY_DELIMITER.join(entries) if entries else ""
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp_path.write_text(content, encoding="utf-8")
        atomic_replace(tmp_path, path)

    def system_prompt_block(self) -> str:
        blocks = [
            "To save or read shared SOPs (target='memory') or personal user preferences (target='user'), use the `multiuser_memory` tool."
        ]
        mem_entries = self._read_entries("memory")
        if mem_entries:
            content = "\n".join(f"- {e}" for e in mem_entries)
            blocks.append(f"## System & Environment Memory (Shared SOPs)\n{content}")

        if self._session_is_shared:
            blocks.append(SHARED_SESSION_NOTICE)
            return "\n\n".join(blocks)

        user_entries = self._read_entries("user")
        if user_entries:
            content = "\n".join(f"- {e}" for e in user_entries)
            blocks.append(f"## User Profile Memory (Private to {self._user_id})\n{content}")

        return "\n\n".join(blocks)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [MEMORY_TOOL_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        if tool_name != "multiuser_memory":
            return tool_error(f"Unknown tool: {tool_name}")

        action = args.get("action")
        target = args.get("target", "memory")
        if target not in {"memory", "user"}:
            return tool_error("Target must be 'memory' or 'user'.")
        # Fails closed on reads too: the entries in scope belong to whoever opened
        # this shared thread, not necessarily to whoever is speaking now.
        if target == "user" and self._session_is_shared:
            return tool_error(SHARED_SESSION_NOTICE)

        entries = self._read_entries(target)

        if action == "read":
            return json.dumps({"success": True, "target": target, "entries": entries}, ensure_ascii=False)

        elif action == "add":
            content_val = args.get("content")
            content = content_val.strip() if isinstance(content_val, str) else ""
            if not content:
                return tool_error("Content required for 'add'.")
            if content not in entries:
                entries.append(content)
                self._write_entries(target, entries)
            return json.dumps({"success": True, "message": f"Added to {target} memory."})

        elif action == "replace":
            old_val = args.get("old_content") or args.get("old_text")
            new_val = args.get("new_content") or args.get("content")
            old_c = old_val.strip() if isinstance(old_val, str) else ""
            new_c = new_val.strip() if isinstance(new_val, str) else ""
            if not old_c or not new_c:
                return tool_error("old_content and new_content required for 'replace'.")
            if old_c in entries:
                idx = entries.index(old_c)
                entries[idx] = new_c
                self._write_entries(target, entries)
                return json.dumps({"success": True, "message": f"Replaced entry in {target} memory."}) 
            return tool_error(f"Old content exact match not found in {target} memory.")

        elif action == "remove":
            old_val = args.get("old_content") or args.get("content")
            old_c = old_val.strip() if isinstance(old_val, str) else ""
            if not old_c:
                return tool_error("Content to remove is required.")
            if old_c in entries:
                entries.remove(old_c)
                self._write_entries(target, entries)
                return json.dumps({"success": True, "message": f"Removed from {target} memory."}) 
            return tool_error(f"Exact match not found in {target} memory.")

        return tool_error(f"Invalid action: {action}")

def register(ctx: Any) -> None:
    ctx.register_memory_provider(MultiUserFileMemoryProvider())
