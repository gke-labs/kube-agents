"""Everything the model reads: prompt blocks, result guidance, tool schemas.

Collected in one module because it is all one contract. The system prompt tells
the model what memory is, the tool schemas tell it how to ask, and the guidance
strings tell it how to read an answer — and the three have to agree. When they
drift the failure is silent: the model keeps calling the tool and keeps
misreporting what came back.

The one rule that appears in all three is `MEMORY_ABSENCE_RULE`. It is stated in
the prompt as well as in each result because the injected memory block has no
return value to carry it: when nothing matches, no block appears at all, and
silence is the one outcome a tool cannot annotate.
"""

from typing import Any, Dict, List

from .config_schema import SCOPES

SHARED_SESSION_NOTICE = (
    "Personal memory is unavailable in this conversation. It is a shared thread "
    "that more than one person can post in, and the harness cannot attribute a "
    "message to its sender here, so nothing may be read from or written to a "
    "person's private memory. Shared memory still works; personal memory works "
    "in a direct message."
)
NO_IDENTITY_NOTICE = (
    "Personal memory is unavailable because this session carries no user "
    "identity. Only shared memory is reachable here."
)

RECALL_PREAMBLE = (
    "# Memory\n"
    "Durable facts recalled from previous sessions — both what you know about "
    "the person you are talking to and what the organisation has recorded for "
    "everyone. Use them to answer directly and to resolve possessives before "
    "delegating. Do not look these up with a tool — they are already here. "
    "These are the entries that matched this turn, not an index of everything "
    "recorded; something absent here may still be in memory."
)

# What a read reports about itself.
#
# The stock tool answers an empty recall with the bare string "No relevant
# memories found." A model reads that as *no such record exists* and will then
# say so with full confidence: in the scale test, a specialist reported a real
# ADR as "zero records — its content isn't recorded anywhere retrievable" while
# the ADR's text sat in the store it was nominally reading. The failure is in the
# return value, not the model. Three outcomes are not interchangeable, and the
# tool has to name which one happened rather than leaving it to be inferred:
#
#   found       — the store answered, and matched
#   no_match    — the store answered, and matched nothing *for this query*
#   unreachable — the store did not answer; nothing was searched at all
#
# Every return therefore also carries a `searched` envelope — bank, scope tags,
# query, layer — so an empty result is attributable to a search that was run,
# rather than reading as a property of the world.
NO_MATCH_GUIDANCE = (
    "The store was reachable and answered: this query matched nothing. That is "
    "not the same as the record not existing. Recall is a semantic search that "
    "returns top matches over the consolidated layer, so a record phrased "
    "differently, held under a scope not searched here, or retained but not yet "
    "consolidated will not surface. Do not report that something does not exist "
    "on the strength of this result — report that the search did not surface it, "
    "and try an exact identifier, different wording, or a wider scope."
)
UNREACHABLE_GUIDANCE = (
    "Memory could not be reached, so nothing was searched. This is a failure of "
    "the store, not an absence of records. Say that memory was unavailable; do "
    "not answer as though it were empty."
)

# The rule the return values above exist to make followable.
MEMORY_ABSENCE_RULE = (
    "Memory is a search, not an index. Neither the injected entries nor a "
    "`memory_recall` result is a list of everything recorded, and a read can "
    "fail to reach the store entirely. Never state that a record does not exist "
    "because memory did not return it — say that you could not find it, and name "
    "which it was: the search matched nothing, or memory was unavailable."
)

SYSTEM_PROMPT_HEADER = "# Memory"
SYSTEM_PROMPT_BODY = (
    "You have long-term memory. Relevant entries are injected into your context "
    "automatically each turn and are retained automatically — in the normal case "
    "you do not call a tool at all. It holds two kinds of fact:\n"
    "- **Personal** — private to the person you are talking to.\n"
    "- **Shared** — visible to everyone in the organisation.\n"
    "\n"
    "`memory_recall` searches and `memory_reflect` synthesises across both by "
    "default; use them only when the injected memories are not enough. "
    "`memory_retain` writes a personal fact unless you pass `scope: \"shared\"`, "
    "which you should do only for facts that are true for everybody, never for "
    "one person's preferences.\n"
    "\n"
    + MEMORY_ABSENCE_RULE
)
SYSTEM_PROMPT_SHARED_ONLY = (
    "You have long-term memory, but only the **shared** part of it is reachable "
    "here — the facts this organisation has recorded for everyone. Relevant "
    "entries are injected into your context automatically. `memory_recall` and "
    "`memory_reflect` search them; `memory_retain` adds to them.\n"
    "\n"
    + MEMORY_ABSENCE_RULE
)
SYSTEM_PROMPT_READ_ONLY = (
    "You can **read** the organisation's shared long-term memory: standard "
    "procedures, platform conventions and defaults, cluster and environment "
    "inventory, ownership, and the history of releases and infrastructure "
    "changes. Relevant entries are injected into your context automatically; "
    "`memory_recall` searches them and `memory_reflect` synthesises across them "
    "when you need something the injected entries do not cover.\n"
    "\n"
    "You **cannot write** to memory, and there is no tool that would let you. "
    "What you conclude during a task is a finding, not a recorded fact: report "
    "it in your result. Do not cache what you read here into a file, a skill or "
    "a note for later — a private copy goes stale the moment shared memory is "
    "corrected, and nobody can review it. Read it again next time.\n"
    "\n"
    + MEMORY_ABSENCE_RULE
)


def system_prompt_block(*, read_only: bool, user_tag: str, disabled_reason: str) -> str:
    """The memory section of the system prompt, in the variant this session gets."""
    if read_only:
        return f"{SYSTEM_PROMPT_HEADER}\n{SYSTEM_PROMPT_READ_ONLY}"
    if not user_tag:
        block = f"{SYSTEM_PROMPT_HEADER}\n{SYSTEM_PROMPT_SHARED_ONLY}"
        return block + (f"\n\n{disabled_reason}" if disabled_reason else "")
    return f"{SYSTEM_PROMPT_HEADER}\n{SYSTEM_PROMPT_BODY}"


def tool_schemas(*, read_only: bool) -> List[Dict[str, Any]]:
    """The three memory tools, minus the write tool on a read-only profile.

    A read-only profile is not shown the write tool at all. Advertising it and
    refusing the call would spend a turn and read as a transient failure worth
    retrying; the absent schema is unambiguous. The refusal in
    ``handle_tool_call`` stays as the backstop.
    """
    scope_read = {
        "type": "string",
        "enum": list(SCOPES),
        "description": (
            "Which memory to search: 'personal' (this user only), 'shared' "
            "(facts everyone sees), or 'both'. Defaults to 'both'."
        ),
    }
    scope_write = {
        "type": "string",
        "enum": ["personal", "shared"],
        "description": (
            "Where to store it. 'personal' is private to this user and is the "
            "default. Use 'shared' ONLY for a fact the user states as true for "
            "the whole team or organisation — every user can read it."
        ),
    }
    retain: List[Dict[str, Any]] = [] if read_only else [
        {
            "name": "memory_retain",
            "description": (
                "Store a durable fact in long-term memory immediately. Routine "
                "facts are captured automatically at the end of a session; use "
                "this when the fact is needed sooner or the user asks you to "
                "remember it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "The fact to store, phrased to stand on its own."},
                    "scope": scope_write,
                    "context": {"type": "string", "description": "Short label (e.g. 'user preference', 'team standard')."},
                },
                "required": ["content"],
            },
        }
    ]
    return retain + [
        {
            "name": "memory_recall",
            "description": (
                "Search long-term memory. Relevant memories are already recalled "
                "into your context each turn; use this only for something you "
                "need now and cannot see there. Returns a `status` of `found`, "
                "`no_match` (searched, matched nothing) or `unreachable` (the "
                "store did not answer) — the last two are not evidence that a "
                "record does not exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for."},
                    "scope": scope_read,
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_reflect",
            "description": (
                "Synthesize a reasoned answer across long-term memories, rather "
                "than returning individual matches. Use for open questions about "
                "the user's history."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The question to reflect on."},
                    "scope": scope_read,
                },
                "required": ["query"],
            },
        },
    ]
