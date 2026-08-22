"""The provider itself: one session's identity, and what it may read and write.

A session resolves to exactly one of four states, decided in `initialize` and
fixed for its lifetime:

* **attributed** — a DM from a known user. Personal and shared memory, read and
  write, automatic capture on.
* **unattributable** — a multi-party thread. There are people here, and their
  personal memory exists; this session just cannot tell which of them is
  speaking, so it may not touch any of it.
* **unattended** — cron, the k8s event watcher. No person is in the
  conversation at all, so there is no personal memory in play to get wrong.
* **read-only** — a specialist profile. Shared memory, reads only, no write tool
  advertised.

`_user_tag` decides what may be *read and written*: empty means no personal
memory, and every branch that could cross users is written to fail closed on it
rather than to succeed conditionally. It is empty in the middle two states
alike, and they must not be collapsed anywhere a *default* is chosen — an
unqualified write is a guess about intent, and the safe guess in a room full of
people is the opposite of the safe guess in an empty one. `_unattended` is the
flag that keeps them apart; see `handle_tool_call`.

Identity handling and tool dispatch live here. Anything that reaches into the
stock Hindsight provider is in `client.py`; anything the model reads is in
`prompts.py`.
"""

import hashlib
import inspect
import json
import logging
import re
from typing import Any, Dict, FrozenSet, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

from . import client
from .config_schema import (
    PERSONAL_STRATEGY,
    PROVIDER_NAME,
    SCOPES,
    SHARED_STRATEGY,
    SHARED_TAG,
    USER_TAG_PREFIX,
    memory_is_read_only,
    thread_sessions_are_per_user,
)
from .prompts import (
    NO_IDENTITY_NOTICE,
    NO_MATCH_GUIDANCE,
    SHARED_SESSION_NOTICE,
    UNREACHABLE_GUIDANCE,
    system_prompt_block,
    tool_schemas,
)

logger = logging.getLogger(__name__)


def sanitize_user_id(user_id: str) -> str:
    """Reduce a gateway identity to something safe to use as a tag value.

    The readable half mirrors Hindsight's own ``_sanitize_bank_segment``:
    alphanumerics, dash and underscore survive, everything else collapses to a
    single dash. Applied for the same reason it is applied to bank names — the
    value is attacker-adjacent (it comes from the chat platform) and ends up in a
    query filter.

    That half is **lossy**, and here the tag is the entire isolation boundary, so
    a collision is not a cosmetic problem: two identities that sanitize alike
    would read each other's private memories and retain into each other's scope.
    Identities are email-shaped in every deployed configuration
    (``session_store/store.py`` treats the Google Chat ``user_id`` as an address),
    and punctuation is exactly what varies between them — ``a.b@corp.com`` and
    ``a-b@corp.com`` both reduce to ``a-b-corp-com``, as do ``alice@eng.corp.com``
    and ``alice.eng@corp.com``.

    A short digest of the *raw* identity is therefore appended, which is what
    ``multiuser_memory`` has always done for its filenames. The readable half
    stays first so a tag is still recognisable in the bank; the digest is what
    makes it unique. Whitespace is stripped before hashing so a padded copy of an
    id resolves to the same person.

    Returns ``""`` for an empty identity — the caller reads that as "no identity"
    and fails closed on personal memory, so it must not become a hash of nothing.
    """
    raw = str(user_id or "").strip()
    if not raw:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", raw)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{cleaned}_{digest}" if cleaned else digest


class KubeAgentsMemoryProvider(MemoryProvider):
    """One Hindsight bank, split per user by scope tags."""

    def __init__(self) -> None:
        self._hindsight: Optional[MemoryProvider] = None
        self._user_tag: str = ""
        self._personal_disabled_reason: str = ""
        # True only when there is no person in the conversation at all. Not the
        # same as `not self._user_tag`, which is also true of a group thread.
        self._unattended: bool = False
        self._session_id: str = ""
        self._read_only: bool = False
        # Per-method keyword allowlists for _call, resolved once from the stock
        # provider actually loaded. None means "takes **kwargs, forward it all".
        self._accepts: Dict[str, Optional[FrozenSet[str]]] = {}
        self._reported_drops: set = set()

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        if self._hindsight is not None:
            return self._hindsight.is_available()
        return client.hindsight_is_available()

    # -- lifecycle -----------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        self._session_id = str(session_id or "").strip()
        self._hindsight = None
        self._user_tag = ""
        self._personal_disabled_reason = ""
        self._unattended = False
        self._read_only = memory_is_read_only()

        user_id = sanitize_user_id(kwargs.get("user_id") or "")

        # Refuse personal memory when the session can carry more than one human.
        #
        # agent._user_id is frozen once at Agent construction, and
        # build_session_key() (gateway/session.py) deliberately omits the
        # participant id inside a thread unless `thread_sessions_per_user` is on.
        # So in a shared thread the second speaker reuses the first speaker's
        # cached Agent, and a per-user tag would recall person A's memories into
        # person B's prompt and retain B's turns under A's name. Nothing in the
        # provider protocol identifies the speaker — system_prompt_block() takes
        # no arguments and handle_tool_call() is passed no identity — so it fails
        # closed. Shared memory, visible to everyone by design, is unaffected.
        chat_type = str(kwargs.get("chat_type") or "").strip().lower()
        session_is_shared = bool(
            chat_type
            and chat_type != "dm"
            and kwargs.get("thread_id")
            and not thread_sessions_are_per_user()
        )

        if session_is_shared:
            self._personal_disabled_reason = SHARED_SESSION_NOTICE
            logger.info(
                "%s: personal memory disabled for session %s (shared %s thread — "
                "sender cannot be attributed)", PROVIDER_NAME, session_id, chat_type,
            )
        elif not user_id:
            self._personal_disabled_reason = NO_IDENTITY_NOTICE
            self._unattended = True
            logger.info("%s: personal memory disabled for session %s (no user identity)",
                        PROVIDER_NAME, session_id)
        else:
            self._user_tag = f"{USER_TAG_PREFIX}{user_id}"

        self._hindsight = client.load_hindsight(session_id, kwargs)
        if self._hindsight is None:
            logger.warning("%s: no memory available for session %s", PROVIDER_NAME, session_id)
            return

        client.apply_scoping(self._hindsight, user_tag=self._user_tag,
                             read_only=self._read_only)
        client.ensure_bank(self._hindsight)
        logger.info("%s: bank=%s scope=%s budget=%s writes=%s", PROVIDER_NAME,
                    getattr(self._hindsight, "_bank_id", "?"),
                    self._user_tag or "shared-only",
                    getattr(self._hindsight, "_budget", "?"),
                    "denied (read_only)" if self._read_only else "allowed")

    def shutdown(self) -> None:
        self._call("shutdown")

    # -- context -------------------------------------------------------------

    def system_prompt_block(self) -> str:
        if self._hindsight is None:
            return ""
        return system_prompt_block(
            read_only=self._read_only,
            user_tag=self._user_tag,
            disabled_reason=self._personal_disabled_reason,
        )

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        self._call("queue_prefetch", query, session_id=session_id)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return self._call("prefetch", query, session_id=session_id) or ""

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        self._call("on_turn_start", turn_number, message, **kwargs)

    def on_session_switch(self, new_session_id: str, **kwargs: Any) -> None:
        self._session_id = str(new_session_id or "").strip()
        self._call("on_session_switch", new_session_id, **kwargs)

    # -- retention -----------------------------------------------------------
    #
    # Automatic capture is personal, and only when the speaker is known. Shared
    # knowledge is read by everyone, so it never absorbs a conversation wholesale
    # — it takes explicit writes through memory_retain(scope="shared").
    #
    # The ``**kwargs`` here is deliberate and load-bearing: the harness reads it
    # as consent to send the completed turn's ``messages`` list, which is worth
    # having the day the stock provider can use it. What it must not do is arrive
    # unfiltered at a provider that cannot — see _forwardable.

    def sync_turn(self, user_content: str, assistant_content: str, **kwargs: Any) -> None:
        if self._user_tag and not self._read_only:
            self._call("sync_turn", user_content, assistant_content, **kwargs)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._user_tag and not self._read_only:
            self._call("on_session_end", messages)

    # -- tools ---------------------------------------------------------------

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return tool_schemas(
            read_only=self._read_only,
            has_identity=bool(self._user_tag),
            unattended=self._unattended,
        )

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        if self._hindsight is None:
            return tool_error("Memory is unavailable.")
        if tool_name not in ("memory_retain", "memory_recall", "memory_reflect"):
            return tool_error(f"Unknown memory tool: {tool_name}")

        is_write = tool_name == "memory_retain"
        if is_write and self._read_only:
            # Backstop for the schema omission in prompts.tool_schemas. Reached
            # only if the model invents the call or a cached schema outlives a
            # config change.
            logger.warning("%s: refused a write on a read-only profile", PROVIDER_NAME)
            return tool_error(
                "Memory is read-only for this agent — there is no way to write to it "
                "from here, and retrying will not change that. Report the fact in "
                "your result instead; recording it is the front-door agent's job.",
                status="read_only",
            )
        # A write with no scope is the model declining to state intent, so the
        # default has to be the safe reading of that silence — and the safe
        # reading is not the same in every session.
        #
        # 'personal' held for all of them, which in an *unattended* session is
        # the single scope guaranteed to be refused a few lines below: cron and
        # the k8s event watcher may write, but only to shared, so their first
        # write always failed and none of them ever tried twice. There, shared
        # is not a fallback — it is the only writable scope in the room.
        #
        # In a group thread the opposite holds, and it is `_unattended` rather
        # than `_user_tag` that tells the two apart. A space is full of named
        # people whose personal memory exists and is merely unreachable from
        # here; defaulting an unqualified write to shared would publish one
        # participant's stated fact to the whole organisation, silently. So it
        # keeps defaulting to personal and keeps hitting the refusal below,
        # which is the behaviour SOUL.md calls a safety property.
        default_scope = "both"
        if is_write:
            default_scope = "shared" if self._unattended else "personal"
        scope = str(args.get("scope") or default_scope).strip().lower()
        if scope not in SCOPES or (is_write and scope == "both"):
            return tool_error(
                f"Invalid scope {scope!r} for {tool_name}. "
                f"Use {'personal or shared' if is_write else 'personal, shared, or both'}."
            )
        if scope in ("personal", "both") and not self._user_tag:
            # 'both' degrades to shared rather than failing: the shared half is
            # still answerable, and the system prompt has already explained why
            # the personal half is not.
            if scope == "personal":
                return tool_error(self._personal_disabled_reason
                                  or "Personal memory is unavailable in this session.")
            scope = "shared"

        if is_write:
            return self._retain(args, scope)
        return self._read(tool_name, args, scope)

    def _tags_for(self, scope: str) -> List[str]:
        if scope == "shared":
            return [SHARED_TAG]
        if scope == "personal":
            return [self._user_tag]
        return [self._user_tag, SHARED_TAG] if self._user_tag else [SHARED_TAG]

    def _retain(self, args: Dict[str, Any], scope: str) -> str:
        content = str(args.get("content") or "").strip()
        if not content:
            return tool_error("Missing required parameter: content")
        tags = self._tags_for(scope)
        item: Dict[str, Any] = {
            "content": content,
            "tags": tags,
            "observation_scopes": [tags],
            "strategy": SHARED_STRATEGY if scope == "shared" else PERSONAL_STRATEGY,
        }
        context = str(args.get("context") or "").strip()
        if context:
            item["context"] = context
        try:
            client.retain(self._hindsight, item)
        except Exception as e:
            logger.warning("%s: retain failed (scope=%s): %s", PROVIDER_NAME, scope, e)
            return tool_error(f"Failed to store the memory: {e}")
        return json.dumps({"result": f"Stored in {scope} memory."})

    def _read(self, tool_name: str, args: Dict[str, Any], scope: str) -> str:
        query = str(args.get("query") or "").strip()
        if not query:
            return tool_error("Missing required parameter: query")
        tags = self._tags_for(scope)
        if tool_name == "memory_recall":
            return self._recall(query, scope, tags)
        return self._reflect(query, scope, tags)

    def _searched(self, tool_name: str, query: str, scope: str, tags: List[str]) -> Dict[str, Any]:
        """Describe the search that was run, whatever its outcome.

        Returned alongside every read so that "nothing came back" is a statement
        about a query and a scope, not about the world. See NO_MATCH_GUIDANCE.
        """
        envelope: Dict[str, Any] = {
            "tool": tool_name,
            "query": query,
            "scope": scope,
            "bank": self._hindsight._bank_id,
            "tags": list(tags),
        }
        if tool_name == "memory_recall":
            types = getattr(self._hindsight, "_recall_types", None)
            if types:
                envelope["layer"] = list(types)
        return envelope

    def _recall(self, query: str, scope: str, tags: List[str]) -> str:
        searched = self._searched("memory_recall", query, scope, tags)
        try:
            results = client.recall(self._hindsight, query, tags)
        except Exception as e:
            logger.warning("%s: recall failed (scope=%s): %s", PROVIDER_NAME, scope, e)
            return tool_error(
                f"Memory is unreachable: {e}. {UNREACHABLE_GUIDANCE}",
                status="unreachable",
                searched=searched,
            )
        if not results:
            return json.dumps(
                {"status": "no_match", "searched": searched, "matches": 0,
                 "result": NO_MATCH_GUIDANCE}
            )
        lines = [f"{i}. {getattr(r, 'text', '') or ''}" for i, r in enumerate(results, 1)]
        return json.dumps(
            {"status": "found", "searched": searched, "matches": len(results),
             "result": "\n".join(lines)}
        )

    def _reflect(self, query: str, scope: str, tags: List[str]) -> str:
        searched = self._searched("memory_reflect", query, scope, tags)
        try:
            text = client.reflect(self._hindsight, query, tags)
        except Exception as e:
            logger.warning("%s: reflect failed (scope=%s): %s", PROVIDER_NAME, scope, e)
            return tool_error(
                f"Memory is unreachable: {e}. {UNREACHABLE_GUIDANCE}",
                status="unreachable",
                searched=searched,
            )
        if not text:
            return json.dumps(
                {"status": "no_match", "searched": searched, "result": NO_MATCH_GUIDANCE}
            )
        return json.dumps({"status": "found", "searched": searched, "result": text})

    # -- helper --------------------------------------------------------------

    def _call(self, method: str, *a: Any, **kw: Any):
        if self._hindsight is None:
            return None
        target = getattr(self._hindsight, method, None)
        if target is None:
            logger.warning("%s: the stock provider has no %s()", PROVIDER_NAME, method)
            return "" if method == "prefetch" else None
        try:
            return target(*a, **self._forwardable(method, target, kw))
        except Exception:
            # WARNING, not DEBUG. Everything reached from here is a memory read
            # or write, and there is no route by which one of them failing is
            # routine. #784 spent every attributed turn between 2026-08-13 and
            # 2026-08-18 raising a TypeError into this handler and writing
            # nothing, and a DEBUG line kept it out of agent.log, errors.log and
            # Cloud Logging alike. The symptom that did surface — a bank that
            # stays small — is indistinguishable from the retain mission doing
            # its job, so the log line is the only thing that can tell them
            # apart.
            logger.warning("%s: %s failed", PROVIDER_NAME, method, exc_info=True)
            return "" if method == "prefetch" else None

    def _forwardable(self, method: str, target: Any, kw: Dict[str, Any]) -> Dict[str, Any]:
        """Narrow ``kw`` to the keywords the stock provider's ``method`` accepts.

        This wrapper sits between two signatures it does not own. The harness
        decides what to pass by inspecting *ours* (``memory_manager``'s
        ``_provider_sync_accepts_messages`` reads any ``**kwargs`` as "send
        everything"), and the stock provider decides what it will take. When the
        base image moves, those two disagree and the call raises before it does
        any work.

        Filtering is the only fix that holds in both directions. Restating the
        target's parameters here would re-arm the same failure on the next bump
        — the mistake #780 removed from the Slack relay's ``register()`` shim —
        while forwarding blind is what broke this path to begin with (#784).
        Read the target instead: unknown keywords are dropped rather than
        raising, and a keyword the target *does* learn about later starts being
        forwarded with no change here.
        """
        if not kw:
            return kw
        if method not in self._accepts:
            try:
                params = inspect.signature(target).parameters.values()
            except (TypeError, ValueError):
                # Unintrospectable (a C function, an exotic callable). Forwarding
                # blind is what the wrapper did everywhere before this, so it is
                # no worse, and the handler above now says so if it fails.
                self._accepts[method] = None
            else:
                if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params):
                    self._accepts[method] = None
                else:
                    self._accepts[method] = frozenset(
                        p.name
                        for p in params
                        if p.kind
                        in (
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                            inspect.Parameter.KEYWORD_ONLY,
                        )
                    )
        accepted = self._accepts[method]
        if accepted is None:
            return kw
        dropped = sorted(k for k in kw if k not in accepted)
        if dropped and method not in self._reported_drops:
            # Once per method per process, at INFO. Every turn drops the same
            # keyword, so a line per call would be noise; a line per process is
            # what makes the condition findable in agent.log without waiting for
            # someone to query Postgres and notice the bank is empty.
            self._reported_drops.add(method)
            logger.info(
                "%s: %s() does not accept %s — dropping it from every forward",
                PROVIDER_NAME,
                method,
                ", ".join(dropped),
            )
        return {k: v for k, v in kw.items() if k in accepted}
