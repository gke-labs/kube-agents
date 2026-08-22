"""Per-user and shared memory for the Chat Agent, in one Hindsight bank.

Everyone's memory lives in a single bank. What separates one person's memories
from another's is a **scope tag** carried by every fact: ``user:<id>`` for a
private memory, ``scope:shared`` for one the whole organisation can read. Recall
asks for the current user's tag plus the shared tag; nothing else can come back.

Hindsight has all the machinery for this — tags on retain, tag filters on recall,
tag-scoped consolidation. What it does not have is any way to get the *current
user's id* into that configuration. ``{user}`` substitution exists but is wired
to exactly one setting: ``_resolve_bank_id_template`` is called for ``bank_id``
and nothing else (upstream ``plugins/memory/hindsight/__init__.py``), while
``retain_tags`` and ``recall_tags`` are read as literal strings. Configure
``retain_tags: "user:{user_id}"`` and every user is tagged with the characters
``user:{user_id}``. That is the gap this wrapper closes, and it is most of what
it does: resolve the identity, then hand the stock provider the right tags.

Three upstream behaviours make the difference between isolation and a leak, and
each is pinned in ``client.apply_scoping`` rather than left to configuration:

* **``any_strict``, never ``any``.** Hindsight's tag matcher treats ``any``/``all``
  as *"matching tags **or** no tags at all"* — only the ``_strict`` variants
  exclude untagged rows (``engine/search/tags.py``). The plugin's default is
  ``any``. In a single bank that default hands every untagged memory to every
  user, so ``_recall_tags_match`` is forced to ``any_strict``.

* **Reflect ignores tags.** ``hindsight_reflect`` and reflect-mode prefetch both
  call ``areflect(bank_id, query, budget)`` with no tag arguments, so a reflect
  would reason across every user in the bank. The REST API and the client both
  accept ``tags``/``tags_match`` — it is only the plugin that omits them — so
  ``memory_reflect`` is implemented against the client directly, and prefetch is
  pinned to ``recall`` mode.

* **Observation scopes are pinned explicitly.** Recall returns *observations*,
  not raw facts, so isolation is only real if the observation layer is scoped
  too. Hindsight's default (``combined``) scopes an observation by the full tag
  set of its sources — and the stock provider attaches a ``session:<id>`` tag to
  every auto-retained turn. That would make each session its own scope, so
  nothing a user said last week would ever consolidate with what they say today.
  Setting ``observation_scopes`` to an explicit ``[[scope_tag]]`` fixes both
  halves at once: one durable scope per user, immune to whatever per-call
  provenance tags ride along. Hindsight's own consolidator documents this as the
  intended use of explicit scopes.

A fourth upstream behaviour is corrected rather than pinned. The stock read
tools answer an empty result with the string ``"No relevant memories found."``,
which a model reads as *no such record exists* — a claim it will then make
confidently about a store that holds the fact. ``memory_recall`` and
``memory_reflect`` therefore name their outcome (``found`` / ``no_match`` /
``unreachable``) and report the search they ran, so absence is attributable to a
query and a scope. See ``prompts.NO_MATCH_GUIDANCE``.

The two banks this replaced also carried a mission each — what the bank is for,
and what is worth extracting into it. One bank has one ``retain_mission``, but
``retain_mission`` is a per-bank *configurable* field, and configurable fields
can be overridden per item by a named **retain strategy**. So the personal and
shared extraction guidance survives as strategies (``personal``/``shared``)
rather than as separate banks, with ``retain_default_strategy`` making the
personal one apply to automatic capture.

Everything else is still the stock provider, loaded through
``load_memory_provider("hindsight")``. Not forking its ~92 KB implementation is
deliberate: a Hermes base-image bump brings Hindsight fixes along with it and
there is no merge to redo.

**Layout.** This module is the entry point and nothing else. The implementation
is four modules, split along the lines of what would break if each changed:

* ``config_schema`` — the bank name, the two scope tags, the retain strategies
  and missions provisioned onto the bank, and the profile-config reads. Changing
  anything here is a data-migration question, and three consumers outside the
  package restate these values.
* ``prompts`` — everything the model reads: system prompt variants, the tool
  schemas, and the guidance that keeps ``no_match`` from being reported as
  nonexistence. Changing anything here is an agent-behaviour question.
* ``client`` — the only module that reaches into the stock Hindsight provider or
  its generated client. A Hermes base-image bump lands here.
* ``session`` — the provider class: identity resolution, the three session
  states, and tool dispatch.
"""

from .client import (
    apply_budget,
    apply_scoping,
    ensure_bank,
    hindsight_is_available,
    load_hindsight,
)
from .config_schema import (
    BANK_MISSION,
    CHECKPOINT_STRATEGY,
    DEFAULT_BANK_ID,
    PERSONAL_RETAIN_MISSION,
    PERSONAL_STRATEGY,
    PROVIDER_NAME,
    RETAIN_STRATEGIES,
    SCOPES,
    SHARED_RETAIN_MISSION,
    SHARED_STRATEGY,
    SHARED_TAG,
    TAGS_MATCH,
    USER_TAG_PREFIX,
    VALID_BUDGETS,
    memory_is_read_only,
    thread_sessions_are_per_user,
)
from .prompts import (
    MEMORY_ABSENCE_RULE,
    NO_IDENTITY_NOTICE,
    NO_MATCH_GUIDANCE,
    RECALL_PREAMBLE,
    SHARED_SCOPE_TEST,
    SHARED_SESSION_NOTICE,
    SYSTEM_PROMPT_BODY,
    SYSTEM_PROMPT_HEADER,
    SYSTEM_PROMPT_READ_ONLY,
    SYSTEM_PROMPT_SHARED_ONLY,
    UNREACHABLE_GUIDANCE,
)
from .session import KubeAgentsMemoryProvider, sanitize_user_id

__all__ = [
    "BANK_MISSION",
    "CHECKPOINT_STRATEGY",
    "DEFAULT_BANK_ID",
    "KubeAgentsMemoryProvider",
    "MEMORY_ABSENCE_RULE",
    "NO_IDENTITY_NOTICE",
    "NO_MATCH_GUIDANCE",
    "PERSONAL_RETAIN_MISSION",
    "PERSONAL_STRATEGY",
    "PROVIDER_NAME",
    "RECALL_PREAMBLE",
    "RETAIN_STRATEGIES",
    "SCOPES",
    "SHARED_RETAIN_MISSION",
    "SHARED_SCOPE_TEST",
    "SHARED_SESSION_NOTICE",
    "SHARED_STRATEGY",
    "SHARED_TAG",
    "SYSTEM_PROMPT_BODY",
    "SYSTEM_PROMPT_HEADER",
    "SYSTEM_PROMPT_READ_ONLY",
    "SYSTEM_PROMPT_SHARED_ONLY",
    "TAGS_MATCH",
    "UNREACHABLE_GUIDANCE",
    "USER_TAG_PREFIX",
    "VALID_BUDGETS",
    "apply_budget",
    "apply_scoping",
    "ensure_bank",
    "hindsight_is_available",
    "load_hindsight",
    "memory_is_read_only",
    "register",
    "sanitize_user_id",
    "thread_sessions_are_per_user",
]


def register(ctx) -> None:
    ctx.register_memory_provider(KubeAgentsMemoryProvider())
