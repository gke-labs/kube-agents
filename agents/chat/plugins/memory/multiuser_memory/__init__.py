import hashlib
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from agent.memory_provider import MemoryProvider
from tools.registry import tool_error
from utils import atomic_replace

logger = logging.getLogger(__name__)

ENTRY_DELIMITER = "\n§\n"
MAX_ENTRY_LENGTH = 2000
MAX_ENTRIES_PER_TARGET = 500

MEMORY_TOOL_SCHEMA = {
    "name": "multiuser_memory",
    "description": "Read, add, replace, or remove shared environment instructions and SOPs, or personal user profile notes.",
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "add", "replace", "remove"],
                "description": (
                    "What to do: 'read' entries (returns on-disk index, raw content, and rendered view for each entry), "
                    "'add' a new entry, 'replace' an entry, or 'remove' an entry."
                ),
            },
            "target": {
                "type": "string",
                "enum": ["memory", "user"],
                "description": "'memory' for shared system-wide SOPs; 'user' for personal preferences specific to this user.",
            },
            "index": {
                "type": "integer",
                "description": (
                    "0-based on-disk index of the entry to replace or remove (as reported by 'read' in the 'index' field; "
                    "alternative to matching old_content)."
                ),
            },
            "content": {"type": "string", "description": "The text entry to add (for 'add') or remove (for 'remove')."},
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


def _is_safe_char(ch: str) -> bool:
    """Check whether a character is safe from control/zero-width/bidi smuggling."""
    code = ord(ch)
    # Preserve newline (\n, 10) and tab (\t, 9)
    if code in (9, 10):
        return True
    # Strip C0 control characters (< 32), DEL (127), and C1 control characters (128-159)
    if code < 32 or 127 <= code <= 159:
        return False
    # Strip zero-width, bidi, and format control characters
    # U+200B-U+200F (Zero-width space, non-joiner, joiner, LRM, RLM)
    # U+202A-U+202E (Bidi embedding/override controls: LRE, RLE, PDF, LRO, RLO)
    # U+2060-U+206F (Word joiner, invisible operators, bidi isolates)
    # U+FEFF (Zero-width no-break space / BOM)
    # U+00AD (Soft hyphen), U+034F (Combining grapheme joiner), U+061C (Arabic letter mark), U+180E (Mongolian vowel separator)
    # U+2028 (Line separator), U+2029 (Paragraph separator), U+2800 (Braille pattern blank)
    if (
        0x200B <= code <= 0x200F
        or 0x202A <= code <= 0x202E
        or 0x2060 <= code <= 0x206F
        or code in (0xFEFF, 0x00AD, 0x034F, 0x061C, 0x180E, 0x2028, 0x2029, 0x2800)
    ):
        return False
    # Strip Unicode tag block and non-printable supplementary blocks (U+E0000 and above)
    if code >= 0xE0000:
        return False
    return True


def _strip_unsafe_chars(text: str) -> str:
    """Strip ANSI escape sequences, carriage returns, control, zero-width, and bidi characters."""
    if not text:
        return ""
    cleaned = re.sub(r"\r", "", text)
    cleaned = re.sub(
        r"(?:\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\x9B[0-?]*[ -/]*[@-~])",
        "",
        cleaned,
    )
    return "".join(ch for ch in cleaned if _is_safe_char(ch))


def _neutralize_prompt_injection(text: str) -> str:
    """Neutralize LLM special tokens, prompt injection tags, and code fence framing."""
    if not text:
        return ""

    # Delimiter tags (<system...>, </system...>, < system>, <system/>, < /system>, etc.)
    # 1. Closing/spaced-closing (</ system>, < / system>), self-closing (<system/>, <system />),
    #    and standard tags (<system>, <system extra="1">). Uses (?=[^\S\n]|[>/]) lookahead to defuse
    #    standard and self-closing tags (and CLI placeholders like <system namespace>) without
    #    mangling SOP hyphenated names (<system-node-critical>).
    #    Scan class consumes '<' unless it starts a complete tag candidate, using non-overlapping
    #    whitespace quantifiers ([^\S\n]*(?:/[^\S\n]*)?) to maintain strictly linear evaluation.
    #    Iterates to a fixed point to defuse nested candidates (<system <system>foo>).
    for _ in range(10):
        prev = text
        text = re.sub(
            r"<(?:[^\S\n]*/[^\S\n]*|/?)(system|instruction|prompt|admin|untrusted_[a-z0-9_-]+)"
            r"(?=[^\S\n]|[>/])"
            r"(?:[^><\n]|<(?![^\S\n]*(?:/[^\S\n]*)?(?:system|instruction|prompt|admin|untrusted_[a-z0-9_-]+)(?=[^\S\n]|[>/])))*>",
            r"[\1_tag_neutralized]",
            text,
            flags=re.IGNORECASE,
        )
        # 2. Spaced opening tags without attributes (< system>, < system/>) or with attributes
        #    (< system role="admin">), requiring an immediate attribute ([a-z_][a-z0-9_-]*[^\S\n]*=)
        #    to prevent colliding with threshold inequalities like
        #    'CPU < system limit; set threshold=90 if usage > 80%'.
        #    Scan class consumes '<' unless it starts another tag candidate, keeping work linear.
        text = re.sub(
            r"<[^\S\n]+(system|instruction|prompt|admin|untrusted_[a-z0-9_-]+)"
            r"(?:[^\S\n]*(?:/[^\S\n]*)?>"
            r"|(?=[^\S\n]+[a-z_][a-z0-9_-]*[^\S\n]*=)"
            r"(?:[^><\n]|<(?![^\S\n]*(?:/[^\S\n]*)?(?:system|instruction|prompt|admin|untrusted_[a-z0-9_-]+)(?=[^\S\n]|[>/])))*>)",
            r"[\1_tag_neutralized]",
            text,
            flags=re.IGNORECASE,
        )
        if text == prev:
            break

    # Markdown code fence injection attempting to frame system/instruction blocks
    text = re.sub(
        r"(?<!`)`{3,}[^\S\n]*(system|instruction|prompt)\b",
        r"```text",
        text,
        flags=re.IGNORECASE,
    )

    # Specific LLM special tokens, instruction markers, and fake security notices
    replacements = {
        r"<\|im_start\|>": "[token_start]",
        r"<\|im_end\|>": "[token_end]",
        r"<\|start_header_id\|>": "[token_start_header_id]",
        r"<\|end_header_id\|>": "[token_end_header_id]",
        r"<\|eot_id\|>": "[token_eot_id]",
        r"<\|endoftext\|>": "[token_endoftext]",
        r"<\|system\|>": "[token_system]",
        r"<\|user\|>": "[token_user]",
        r"<\|assistant\|>": "[token_assistant]",
        r"<start_of_turn>": "[token_start_of_turn]",
        r"<end_of_turn>": "[token_end_of_turn]",
        r"###\s*System:": "[SYSTEM_TEXT]:",
        r"###\s*Instruction:": "[INSTRUCTION_TEXT]:",
        r"\[INST\]": "[INST_TEXT]",
        r"\[/INST\]": "[/INST_TEXT]",
        r"<<SYS>>": "[SYS_TEXT]",
        r"<</SYS>>": "[/SYS_TEXT]",
        r"<USER_REQUEST>": "[USER_REQUEST_TAG]",
        r"</USER_REQUEST>": "[/USER_REQUEST_TAG]",
        r"<TOOL_CALL>": "[TOOL_CALL_TAG]",
        r"</TOOL_CALL>": "[/TOOL_CALL_TAG]",
        r"===\s*\[SECURITY NOTICE:": "=== [SECURITY_NOTICE_TEXT:",
        r"\[SECURITY NOTICE:": "[SECURITY_NOTICE_TEXT:",
    }
    for pattern, replacement in replacements.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)

    return text


def sanitize_memory_entry(text: str) -> str:
    """Sanitize a new memory entry before storage.

    Strips unsafe control/bidi characters and neutralizes entry delimiter smuggling (\n§\n)
    while keeping the stored content faithful and non-destructive.
    """
    if not text or not isinstance(text, str):
        return ""

    cleaned = _strip_unsafe_chars(text)
    # Delimiter smuggling: neutralize delimiter lines and sequences so a new entry cannot split on storage
    cleaned = re.sub(r"(?m)^[^\S\n]*§[^\S\n]*$", ";", cleaned)
    while ENTRY_DELIMITER in cleaned:
        cleaned = cleaned.replace(ENTRY_DELIMITER, "\n;\n")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def sanitize_for_prompt(text: str, strip_headings: bool = True) -> str:
    """Sanitize a memory entry specifically for injection-safe prompt rendering.

    Runs in-memory during prompt construction or tool read actions without modifying stored data on disk.
    Best-effort defense-in-depth against accidental or naive prompt framing; not an
    impermeable security boundary (multiline tag syntax like <system\\n> and natural-language
    instructions intentionally pass through to prevent corrupting legitimate SOP text).
    """
    if not text or not isinstance(text, str):
        return ""

    cleaned = _strip_unsafe_chars(text)
    cleaned = _neutralize_prompt_injection(cleaned)
    if strip_headings:
        # Neutralize lines starting with '#' so entries cannot create new root-level markdown prompt sections.
        # Consumes all leading hash runs and spaces/tabs (e.g. '# ## Heading' or '  # # # Heading')
        # so subsequent hashes cannot become un-neutralized headings. Uses [^\S\n]* rather than \s*
        # to avoid quadratic backtracking across lines of newlines.
        cleaned = re.sub(r"(?m)^([^\S\n]*)#+[# \t]*", r"\1", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def validate_memory_entry(content: Any, max_length: int = MAX_ENTRY_LENGTH) -> Tuple[Optional[str], Optional[str]]:
    """Validate a memory entry. Returns (sanitized_content, error_message)."""
    if content is None or not isinstance(content, str):
        return None, "Content must be a non-empty string."
    raw = content.strip()
    if not raw:
        return None, "Content cannot be empty."
    if len(raw) > max_length:
        return None, f"Memory entry exceeds maximum length of {max_length} characters (got {len(raw)})."

    sanitized = sanitize_memory_entry(raw)
    if not sanitized or not sanitize_for_prompt(sanitized, strip_headings=True):
        return None, "Memory entry is empty or contains only invalid/control characters."

    return sanitized, None


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
        clean_entries = []
        for e in entries:
            if not e or not isinstance(e, str):
                continue
            # Guard against delimiter smuggling at the write boundary
            if ENTRY_DELIMITER in e:
                e = e.replace(ENTRY_DELIMITER, "\n;\n")
            s = e.strip()
            if s:
                clean_entries.append(s)
        content = ENTRY_DELIMITER.join(clean_entries) if clean_entries else ""
        tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp_path.write_text(content, encoding="utf-8")
        atomic_replace(tmp_path, path)

    def system_prompt_block(self) -> str:
        blocks = [
            "To save or read shared SOPs (target='memory') or personal user preferences (target='user'), use the `multiuser_memory` tool."
        ]
        mem_entries = self._read_entries("memory")
        if mem_entries:
            rendered = [sanitize_for_prompt(e, strip_headings=True) for e in mem_entries]
            content = "\n".join(f"- {e}" for e in rendered if e)
            if content:
                blocks.append(f"## System & Environment Memory (Shared SOPs)\n{content}")

        if self._session_is_shared:
            blocks.append(SHARED_SESSION_NOTICE)
            return "\n\n".join(blocks)

        user_entries = self._read_entries("user")
        if user_entries:
            rendered = [sanitize_for_prompt(e, strip_headings=True) for e in user_entries]
            content = "\n".join(f"- {e}" for e in rendered if e)
            if content:
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
            items = []
            non_empty_count = 0
            for idx, raw in enumerate(entries):
                rendered = sanitize_for_prompt(raw, strip_headings=False)
                if rendered:
                    non_empty_count += 1
                items.append(
                    {
                        "index": idx,
                        "content": raw,
                        "rendered": rendered if rendered else "[empty entry]",
                    }
                )
            return json.dumps(
                {
                    "success": True,
                    "target": target,
                    "entries": items,
                    "count": non_empty_count,
                    "total_entries": len(entries),
                },
                ensure_ascii=False,
            )

        elif action == "add":
            content_val = args.get("content")
            sanitized_content, err = validate_memory_entry(content_val)
            if err:
                return tool_error(err)
            store_desc = "shared memory" if target == "memory" else "user memory"
            if len(entries) >= MAX_ENTRIES_PER_TARGET and sanitized_content not in entries:
                return tool_error(f"Maximum memory entries ({MAX_ENTRIES_PER_TARGET}) reached for {store_desc}.")
            if sanitized_content not in entries:
                entries.append(sanitized_content)
                self._write_entries(target, entries)
            return json.dumps({"success": True, "message": f"Added to {target} memory."})

        elif action == "replace":
            index_val = args.get("index")
            old_val = args.get("old_content") or args.get("old_text")
            new_val = args.get("new_content") or args.get("content")
            if not new_val or not isinstance(new_val, str) or not new_val.strip():
                return tool_error("new_content required for 'replace'.")
            sanitized_new, err = validate_memory_entry(new_val)
            if err:
                return tool_error(err)

            target_idx = None
            if index_val is not None:
                if isinstance(index_val, bool):
                    return tool_error(f"Invalid index: {index_val}. Must be an integer.")
                try:
                    index_val = int(index_val)
                except (ValueError, TypeError):
                    return tool_error(f"Invalid index: {index_val}. Must be an integer.")
                if not entries:
                    return tool_error(f"Cannot replace in {target} memory: store is empty.")
                if not (0 <= index_val < len(entries)):
                    return tool_error(f"Index {index_val} out of range (0 to {len(entries) - 1}) for {target} memory.")
                target_idx = index_val
            else:
                if not old_val or not isinstance(old_val, str) or not old_val.strip():
                    return tool_error("old_content and new_content required for 'replace'.")

                old_c = old_val.strip()
                old_sanitized = sanitize_memory_entry(old_c)
                old_rendered_read = sanitize_for_prompt(old_c, strip_headings=False)
                old_rendered_prompt = sanitize_for_prompt(old_c, strip_headings=True)

                for idx, entry in enumerate(entries):
                    if entry == old_c or entry == old_sanitized:
                        target_idx = idx
                        break
                if target_idx is None:
                    for idx, entry in enumerate(entries):
                        if (
                            sanitize_for_prompt(entry, strip_headings=False) in (old_c, old_rendered_read)
                            or sanitize_for_prompt(entry, strip_headings=True) in (old_c, old_rendered_prompt)
                        ):
                            target_idx = idx
                            break

            if target_idx is not None:
                entries[target_idx] = sanitized_new
                self._write_entries(target, entries)
                return json.dumps({"success": True, "message": f"Replaced entry in {target} memory."})
            return tool_error(f"Old content exact match not found in {target} memory.")

        elif action == "remove":
            index_val = args.get("index")
            old_val = args.get("old_content") or args.get("content")
            target_idx = None
            if index_val is not None:
                if isinstance(index_val, bool):
                    return tool_error(f"Invalid index: {index_val}. Must be an integer.")
                try:
                    index_val = int(index_val)
                except (ValueError, TypeError):
                    return tool_error(f"Invalid index: {index_val}. Must be an integer.")
                if not entries:
                    return tool_error(f"Cannot remove from {target} memory: store is empty.")
                if not (0 <= index_val < len(entries)):
                    return tool_error(f"Index {index_val} out of range (0 to {len(entries) - 1}) for {target} memory.")
                target_idx = index_val
            else:
                if not old_val or not isinstance(old_val, str) or not old_val.strip():
                    return tool_error("Content to remove is required.")

                old_c = old_val.strip()
                old_sanitized = sanitize_memory_entry(old_c)
                old_rendered_read = sanitize_for_prompt(old_c, strip_headings=False)
                old_rendered_prompt = sanitize_for_prompt(old_c, strip_headings=True)

                for idx, entry in enumerate(entries):
                    if entry == old_c or entry == old_sanitized:
                        target_idx = idx
                        break
                if target_idx is None:
                    for idx, entry in enumerate(entries):
                        if (
                            sanitize_for_prompt(entry, strip_headings=False) in (old_c, old_rendered_read)
                            or sanitize_for_prompt(entry, strip_headings=True) in (old_c, old_rendered_prompt)
                        ):
                            target_idx = idx
                            break

            if target_idx is not None:
                entries.pop(target_idx)
                self._write_entries(target, entries)
                return json.dumps({"success": True, "message": f"Removed from {target} memory."})
            return tool_error(f"Exact match not found in {target} memory.")

        return tool_error(f"Invalid action: {action}")

def register(ctx: Any) -> None:
    ctx.register_memory_provider(MultiUserFileMemoryProvider())
