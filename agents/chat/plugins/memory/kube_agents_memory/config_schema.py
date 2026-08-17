"""The names, tags and bank payload this provider pins.

Everything here is a constant the deployment depends on staying stable: the bank
it writes to, the two tags that are the isolation boundary, the retain
strategies provisioned onto the bank, and the two profile-config reads that
decide what a session is allowed to do.

Three consumers outside the plugin restate these values and must be kept in
step: `agents/chat/scripts/memory_file_import.py`,
`agents/chat/scripts/memory_ttl_curator.py`, and the tests under
`tests/memory/`. The scripts say so at the top of their own constant blocks and
run as bare subprocesses with no Hermes profile on the path, which is why they
copy rather than import.
"""

import logging

logger = logging.getLogger(__name__)

PROVIDER_NAME = "kube_agents_memory"
DEFAULT_BANK_ID = "kube-agents-memory"

# The tag every organisation-wide fact carries. Anything without it is only ever
# returned to the one user whose tag it bears.
SHARED_TAG = "scope:shared"
USER_TAG_PREFIX = "user:"

# Excludes untagged rows. See the package docstring — `any` would not.
TAGS_MATCH = "any_strict"

SCOPES = ("personal", "shared", "both")
VALID_BUDGETS = ("low", "mid", "high")

# Retain strategies. `retain_mission` steers what the extractor keeps, and it is
# in Hindsight's per-bank configurable-field set, so a named strategy can carry
# its own — which is how one bank still gets two different editorial policies.
PERSONAL_STRATEGY = "personal"
SHARED_STRATEGY = "shared"

# The strategy the TTL curator writes its checkpoints under.
#
# `memory_ttl_curator.py` would keep the bank bounded by distilling the
# observation layer back into facts and then retiring the aged originals. It is
# deferred and on no schedule yet, but the strategy is still provisioned here so the
# bank is ready for it rather than needing a migration later. A checkpoint is
# only sound if it carries the observation's text *unchanged* — re-summarising a
# summary every cycle is a game of telephone, and the bank would drift away from
# what was actually said.
#
# `chunks` extraction is what guarantees that, and it is the mode that runs no
# LLM at all: `extract_facts_from_contents` dispatches to `_extract_facts_chunks`
# before it takes any LLM queue or lock, storing each chunk as-is. `verbatim` was
# the obvious choice and is the wrong one — it also preserves the text, but it
# still calls the LLM to attach entities and dates, and that call is asked to
# re-emit the observation inside a JSON response schema. Measured against a
# 207-observation bank, 10 of them (5%) came back as `JSONDecodeError` from the
# extraction LLM, which is enough to abort every curator pass: an observation
# that cannot be checkpointed is one whose evidence must not be retired. The
# price of `chunks` is that checkpoints carry no extracted entities, so the graph
# retriever cannot see them; semantic and BM25 retrieval are unaffected.
CHECKPOINT_STRATEGY = "checkpoint"

PERSONAL_RETAIN_MISSION = (
    "Extract only durable facts about this person. Keep their location, "
    "timezone, role and responsibilities; the clusters, projects and "
    "environments they call their own; and the working preferences they state. "
    "Phrase each fact to stand alone and name the person rather than saying "
    "'the user'. "
    "Drop the state of individual tasks and tickets, decisions scoped to one "
    "piece of work, and anything the assistant itself did — that is a record of "
    "a conversation, not a fact about a person, and it stops being true once "
    "the work closes. Drop anything that holds for the whole team; that is "
    "shared knowledge, not personal."
)

# Shared knowledge is loaded from documents, not conversation. Hindsight's
# default extraction assumes dialogue and keeps asides that read as commitments.
SHARED_RETAIN_MISSION = (
    "Extract durable, self-contained operational facts. Each fact must stand "
    "alone without the surrounding document: name the cluster, environment, "
    "component, version or date it is about rather than saying 'this' or "
    "'the above'. Keep procedures, thresholds, ownership, defaults, and dated "
    "changes. Preserve exact identifiers, versions and dates verbatim. Drop "
    "narrative framing, TODOs, unresolved proposals, and anything true only "
    "while a document was being written."
)

RETAIN_STRATEGIES = {
    PERSONAL_STRATEGY: {"retain_mission": PERSONAL_RETAIN_MISSION},
    SHARED_STRATEGY: {"retain_mission": SHARED_RETAIN_MISSION},
    CHECKPOINT_STRATEGY: {"retain_extraction_mode": "chunks"},
}

# What the bank is for, and how to answer from it.
#
# One field, not two. Hindsight's `set_mission` and `set_reflect_mission` are
# both deprecated aliases for `create_bank(bank_id, mission=...)`, so a bank has
# a single `mission` and it is what reflect reasons against; the text has to say
# what the bank holds *and* how to answer from it.
BANK_MISSION = (
    "The working memory of a Kubernetes platform team's assistant. It holds two "
    "kinds of knowledge, separated by tag. Shared knowledge is true for "
    "everybody: standard operating procedures, platform conventions and "
    "defaults, on-call and timezone facts, cluster and environment inventory, "
    "and the history of releases and infrastructure changes. Personal knowledge "
    "is about one individual: where they work and in which timezone, which "
    "clusters, projects and environments are theirs, how they prefer work to be "
    "done, and what they are responsible for. "
    "When answering, cite the specific procedure, version or dated change that "
    "supports the answer, and say plainly when the record does not cover the "
    "question rather than generalising from adjacent facts."
)


def memory_is_read_only() -> bool:
    """Read ``memory.read_only`` from the active profile's config.yaml.

    Profile-scoped: ``load_config()`` resolves through ``HERMES_HOME``, and a
    kanban worker is launched with ``HERMES_HOME`` pointed at its own profile
    directory (``hermes_cli/kanban_db.py`` — ``env["HERMES_HOME"] =
    resolve_profile_env(profile_arg)``). So the platform specialist reads
    ``profiles/platform/config.yaml`` and the Chat Agent reads its own.

    It is a setting rather than something derived from the session because the
    two identity-less cases are not the same. A shared chat space has humans in
    it who can vouch for a shared write; a dispatcher-spawned specialist has
    nobody. Only the second is read-only, and only the profile config knows
    which one it is.

    Defaults to False — a profile that says nothing keeps the write tools.
    """
    try:
        from hermes_cli.config import load_config
        config = load_config() or {}
        memory = config.get("memory")
        if isinstance(memory, dict):
            return bool(memory.get("read_only", False))
    except Exception as e:
        logger.debug("Could not read memory.read_only, assuming writable: %s", e)
    return False


def thread_sessions_are_per_user() -> bool:
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
