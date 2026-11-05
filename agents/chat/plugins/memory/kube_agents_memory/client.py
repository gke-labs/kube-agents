"""Everything that touches the stock Hindsight provider or its client.

The wrapper never reimplements Hindsight; it loads the stock provider, pins the
attributes that make one bank safe for many users, and calls the generated
client directly for the three operations where the stock *tool* would lose
something — a per-call scope, a tag filter, or the difference between "matched
nothing" and "did not answer".

Every attribute set in `apply_scoping` is read by the stock provider at call
time, which is why overriding after its `initialize()` is enough and no
config-file contract is needed. That also makes this module the one place to
look when a Hermes base-image bump changes an attribute name: nothing else in
the package reaches into the provider.

Functions here raise on failure and return data. Turning an outcome into
something the model reads is `session.py`'s job — see `prompts.py` for why the
three outcomes must stay distinguishable.
"""

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from plugins.memory import load_memory_provider

from .config_schema import (
    BANK_MISSION,
    DEFAULT_BANK_ID,
    PERSONAL_STRATEGY,
    PROVIDER_NAME,
    RETAIN_STRATEGIES,
    SHARED_TAG,
    TAGS_MATCH,
    VALID_BUDGETS,
)
from .prompts import RECALL_PREAMBLE

logger = logging.getLogger(__name__)

# Bank config this process has already settled, so the common case costs nothing.
# Deliberately per-process rather than persisted: it is only a cache, and
# re-applying after a restart is idempotent.
_bank_provisioned: set = set()


def hindsight_is_available() -> bool:
    """Answer ``is_available`` with no bank built yet.

    Hermes asks the provider this *before* ``initialize()`` and drops it outright
    if it says no (``agent_init.py``), so the question has to be answerable from
    a standing start. Hindsight's own answer is stateless — it reads
    ``$HERMES_HOME/hindsight/config.json`` — so an uninitialised instance gives
    the same verdict the real one would.
    """
    probe = load_memory_provider("hindsight")
    return bool(probe is not None and probe.is_available())


def load_hindsight(session_id: str, kwargs: Dict[str, Any]) -> Optional[MemoryProvider]:
    """Load and initialize the stock provider, or return None if it will not run."""
    provider = load_memory_provider("hindsight")
    if provider is None:
        logger.warning("%s: could not load the hindsight provider", PROVIDER_NAME)
        return None
    try:
        provider.initialize(session_id, **kwargs)
    except Exception as e:
        logger.warning("%s: hindsight initialize failed: %s", PROVIDER_NAME, e)
        return None
    provider._recall_prompt_preamble = RECALL_PREAMBLE
    return provider


def apply_scoping(provider: MemoryProvider, *, user_tag: str, read_only: bool) -> None:
    """Pin the tag scoping that makes one bank safe for many users."""
    # One bank per deployment, not per user, and its name is a constant here
    # rather than a setting: the Hindsight config file is image-owned, but a
    # bank_id left in a hand-edited copy on the PVC used to win and silently
    # move every memory into a bank nobody was reading. Say so and ignore it.
    config = getattr(provider, "_config", None) or {}
    configured = str(config.get("bank_id") or "").strip()
    if configured and configured != DEFAULT_BANK_ID:
        logger.warning("%s: ignoring bank_id %r from the Hindsight config; this "
                       "provider is single-bank and pins %r",
                       PROVIDER_NAME, configured, DEFAULT_BANK_ID)
    provider._bank_id_template = ""
    provider._bank_id = DEFAULT_BANK_ID

    # Read side. Shared facts are visible to everyone; personal ones only to
    # their owner. `any_strict` is what excludes untagged rows from both.
    recall_tags = [user_tag] if user_tag else []
    recall_tags.append(SHARED_TAG)
    provider._recall_tags = recall_tags
    provider._recall_tags_match = TAGS_MATCH

    # Reflect-mode prefetch calls areflect() with no tag arguments, so it
    # would read across every user. Recall mode applies the filter above.
    if getattr(provider, "_prefetch_method", "recall") != "recall":
        logger.warning("%s: forcing recall-mode prefetch (reflect prefetch ignores "
                       "tag filters and would cross users)", PROVIDER_NAME)
    provider._prefetch_method = "recall"

    # Write side. Automatic capture is always personal — shared facts are
    # written deliberately, through the tool. With no identity there is
    # nobody to attribute a turn to, so nothing is captured automatically.
    if user_tag and not read_only:
        provider._retain_tags = [user_tag]
        provider._tags = [user_tag]
        # One durable scope per user. Without this the `session:<id>` tag the
        # provider adds to each turn would put every session in a scope of
        # its own, and nothing would ever consolidate across them.
        provider._observation_scopes = [[user_tag]]
    else:
        provider._retain_tags = []
        provider._tags = None
        provider._observation_scopes = None
        provider._auto_retain = False

    apply_budget(provider)


def apply_budget(provider: MemoryProvider) -> None:
    """Honour ``recall_budget`` from the Hindsight config, if it is valid.

    Hindsight resolves it into ``_budget`` and reads that attribute on every
    recall and reflect. Unset leaves its own resolution (``mid``) in place.
    """
    config = getattr(provider, "_config", None) or {}
    value = str(config.get("recall_budget") or "").strip().lower()
    if value in VALID_BUDGETS:
        provider._budget = value


def ensure_bank(provider: MemoryProvider) -> None:
    """Provision the bank's mission and retain strategies, creating it if needed.

    None of this can be seeded ahead of time: a Hindsight bank does not exist
    until something is written to it. Doing it here means the first session
    provisions it, a deleted bank comes back correctly, and there is no
    manual step for an operator to forget.

    ``retain_strategies`` is the load-bearing part. ``personal`` and
    ``shared`` carry the two extraction missions that used to be two banks,
    ``retain_default_strategy`` points automatic capture at the personal one,
    and ``checkpoint`` is what the TTL curator writes under. A missing
    strategy is not an error to Hindsight — ``apply_strategy`` logs a warning
    and silently uses the bank default — so the curator checks for its own
    before it will run.

    The comparison is the sentinel for "already done": the bank-level
    ``mission`` is not part of the ``get_bank_config`` payload (that returns
    ``{bank_id, config}``, and mission is bank metadata), so it cannot be
    compared cheaply. Mission and strategies are always written together,
    which makes the strategies a sound proxy for both.

    Costs one read per process and two writes only when something changed.
    Failures are logged and swallowed — an unguided bank is worse than a
    guided one, but it still works, and memory must never be the reason a
    session fails to start.
    """
    bank_id = str(getattr(provider, "_bank_id", "") or "").strip()
    if not bank_id or bank_id in _bank_provisioned:
        return
    # Recorded before the attempt, not after: if the API is down, every
    # subsequent session in this process would otherwise retry a call that is
    # already known to be failing, on the session-creation path.
    _bank_provisioned.add(bank_id)
    try:
        client = provider._get_client()
        config = (client.get_bank_config(bank_id) or {}).get("config") or {}
        if (config.get("retain_strategies") == RETAIN_STRATEGIES
                and config.get("retain_default_strategy") == PERSONAL_STRATEGY):
            return
        # create_bank doubles as the update path — it is what Hindsight's own
        # deprecated set_mission() calls — and leaves existing facts intact.
        # It must come first: it is the call that creates the bank, and
        # update_bank_config only edits one that exists.
        client.create_bank(bank_id=bank_id, mission=BANK_MISSION)
        client.update_bank_config(
            bank_id,
            retain_strategies=RETAIN_STRATEGIES,
            retain_default_strategy=PERSONAL_STRATEGY,
        )
        logger.info("%s: provisioned mission and retain strategies on bank %s",
                    PROVIDER_NAME, bank_id)
    except Exception as e:
        logger.warning("%s: could not provision bank %s: %s", PROVIDER_NAME, bank_id, e)


def retain(provider: MemoryProvider, item: Dict[str, Any]) -> None:
    """Write one item, tags and strategy already resolved by the caller.

    Written against the client rather than delegated to ``hindsight_retain``
    because the stock tool merges per-call tags with the instance's own and
    offers no per-call ``observation_scopes`` or ``strategy``. A shared fact
    must not inherit the caller's ``user:`` tag — it would consolidate into
    that person's scope and become invisible to everyone else.
    """
    bank_id = provider._bank_id
    provider._run_hindsight_operation(
        lambda client: client.aretain_batch(bank_id=bank_id, items=[item], retain_async=False)
    )


def recall(provider: MemoryProvider, query: str, tags: List[str]) -> List[Any]:
    """Search one scope and return the raw results.

    Written against the client rather than delegated to ``hindsight_recall``
    for two reasons. The stock tool collapses an empty result set to the
    string "No relevant memories found." and a transport failure to a generic
    tool error, which is exactly the conflation this replaces; and it filters
    on the instance's own ``_recall_tags``, so a narrower scope could only be
    served by mutating that attribute around the call and restoring it after.
    One direct call serves every scope and keeps the outcome distinguishable.
    """
    kwargs: Dict[str, Any] = {
        "bank_id": provider._bank_id,
        "query": query,
        "budget": getattr(provider, "_budget", "mid"),
        "max_tokens": getattr(provider, "_recall_max_tokens", 4096),
        "tags": tags,
        "tags_match": TAGS_MATCH,
    }
    types = getattr(provider, "_recall_types", None)
    if types:
        kwargs["types"] = list(types)
    response = provider._run_hindsight_operation(lambda client: client.arecall(**kwargs))
    return list(getattr(response, "results", None) or [])


def reflect(provider: MemoryProvider, query: str, tags: List[str]) -> str:
    """Synthesize across memories, with the tag filter the stock tool omits.

    ``hindsight_reflect`` calls ``areflect(bank_id, query, budget)`` and
    stops there, so in a shared bank it would reason over everyone. The API
    and the generated client both accept ``tags``/``tags_match``; only the
    plugin leaves them out. Mental models are excluded because they are
    bank-level and not tag-scoped — this deployment creates none, so the
    exclusion costs nothing and removes the one remaining unscoped path.
    """
    bank_id = provider._bank_id
    budget = getattr(provider, "_budget", "mid")
    response = provider._run_hindsight_operation(
        lambda client: client.areflect(
            bank_id=bank_id, query=query, budget=budget,
            tags=tags, tags_match=TAGS_MATCH, exclude_mental_models=True,
        )
    )
    return str(getattr(response, "text", "") or "").strip()


def _resolve_curator_bin() -> str:
    """Return the path to the ``curator`` CLI binary if installed, else ``""``."""
    configured = os.environ.get("CURATOR_BIN", "").strip()
    if configured:
        return configured
    if os.path.isfile("/usr/local/bin/curator") and os.access("/usr/local/bin/curator", os.X_OK):
        return "/usr/local/bin/curator"
    return shutil.which("curator") or ""


def recall_decisions(query: str, max_results: int = 4) -> List[str]:
    """Query structured DEC-*/LES-* records over NATS via ``curator recall``.

    Returns an empty list when ``NATS_URL`` is unset or ``curator`` is not
    installed so unit tests and environments without the NATS bus are
    unaffected.
    """
    q = str(query or "").strip()
    if not q or not os.environ.get("NATS_URL", "").strip():
        return []
    curator_bin = _resolve_curator_bin()
    if not curator_bin:
        return []
    try:
        proc = subprocess.run(
            [curator_bin, "recall", q],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return []
        payload = json.loads(proc.stdout)
    except Exception as e:
        logger.debug("%s: NATS decision recall failed: %s", PROVIDER_NAME, e)
        return []

    records_by_id = {
        str(rec.get("id") or ""): rec
        for rec in (payload.get("records") or [])
        if isinstance(rec, dict) and rec.get("id")
    }
    formatted: List[str] = []
    for item in payload.get("results") or []:
        if not isinstance(item, dict):
            continue
        rec_id = str(item.get("id") or "").strip()
        doc_id = str(item.get("document_id") or "").strip()
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        is_okf = (
            rec_id.startswith(("DEC-", "LES-"))
            or doc_id.startswith("okf:")
            or rec_id in records_by_id
        )
        if not is_okf:
            continue
        rec_meta = records_by_id.get(rec_id) or {}
        status = str(rec_meta.get("status") or "").strip().lower()
        header = f"[{rec_id}]" if rec_id else "[Record]"
        if status == "rejected":
            reason = str(rec_meta.get("reviewer_reason") or "").strip()
            note = f" (Reason: {reason})" if reason else ""
            header = f"{header} [STATUS: REJECTED — do not enforce as active policy{note}]"
        elif status == "proposed":
            pr_ref = str(rec_meta.get("pull_request") or "").strip()
            pr_note = f" in {pr_ref}" if pr_ref else ""
            header = f"{header} [STATUS: PROPOSED — awaiting pull request review{pr_note}; not yet active policy]"
        elif status:
            header = f"{header} [status: {status}]"
        formatted.append(f"{header}\n{text}")
        if len(formatted) >= max_results:
            break
    return formatted


def _file_memory_pr_card(
    record_id: str,
    title: str,
    rel_path: str,
    branch: str,
    pr_markdown: str,
    repo: str = "",
) -> str | None:
    """File an idempotent Kanban card instructing the ``platform`` agent to open the OKF PR.

    The card uses ``submit-suggestion`` (``/v1/vcs/*`` + local ``/opt/vcs/bin/git``)
    in the sandbox so ``curator`` never needs sandbox or VCS access.
    """
    if not record_id or not rel_path or not branch or not pr_markdown:
        return None
    try:
        from hermes_cli.kanban import run_slash
    except Exception:
        return None

    import re
    import shlex

    repo_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", repo or "default").strip("-").lower()
    idem_key = f"memory-pr-{repo_slug}-{record_id}"
    card_title = f"Open Decision Memory PR for {record_id}: {title}"[:200]
    repo_flag = f" --repo {shlex.quote(repo)}" if repo else ""
    body = (
        f"Curator synthesized **{record_id}** (`{rel_path}`) with `status: active` "
        "in its frontmatter for team review.\n\n"
        "Open the pull request on the GitOps repository using the **submit-suggestion** "
        "skill (`/v1/vcs/*` + local `git` in the sandbox):\n\n"
        f"1. Run `python3 /opt/data/skills/submit-suggestion/scripts/submit_suggestion.py "
        f"prepare{repo_flag} --branch {shlex.quote(branch)}` and read the `workspace` path.\n"
        f"2. Write the exact Markdown below to `<workspace>/{rel_path}` (creating parent "
        "directories if needed).\n"
        f"3. Run `python3 /opt/data/skills/submit-suggestion/scripts/submit_suggestion.py "
        f"submit{repo_flag} --branch {shlex.quote(branch)} "
        f"--title {shlex.quote(f'docs(knowledge): record {record_id} ({title})')[:120]} "
        f"--body {shlex.quote(f'Records {record_id} (`{rel_path}`) in Decision Memory with `status: active` upon merge.')}`.\n\n"
        f"### `{rel_path}`\n\n"
        f"```markdown\n{pr_markdown.strip()}\n```\n"
    )
    cmd = (
        f"create --json --assignee platform "
        f"--idempotency-key {shlex.quote(idem_key)} "
        f"--body {shlex.quote(body)} "
        f"{shlex.quote(card_title)}"
    )
    try:
        out = str(run_slash(cmd)).strip()
        start = out.find("{")
        end = out.rfind("}")
        if start != -1 and end > start:
            return str(json.loads(out[start : end + 1]).get("id") or "") or None
    except Exception as e:
        logger.debug("%s: could not file memory PR card for %s: %s", PROVIDER_NAME, record_id, e)
    return None


def emit_shared_decision(content: str, context: str = "") -> Dict[str, Any] | None:
    """Publish a proposed decision over NATS via ``curator emit`` and queue a PR card.

    Returns the parsed ``curator emit`` JSON payload when ``curator`` succeeds
    so the caller can report the allocated ``DEC-*`` ID and PR branch without
    storing a duplicate unreviewed copy in ``kube-agents-memory``. Returns
    ``None`` when ``NATS_URL`` is unset or ``curator`` is unavailable.
    """
    text = str(content or "").strip()
    if not text or not os.environ.get("NATS_URL", "").strip():
        return None
    curator_bin = _resolve_curator_bin()
    if not curator_bin:
        return None
    title = text.splitlines()[0][:80].strip()
    cmd = [
        curator_bin,
        "emit",
        "--kind",
        "proposed",
        "--type",
        "decision",
        "--title",
        title,
        "--summary",
        f"{text} ({context})" if context else text,
        "--rule",
        text,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25, check=False)
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        payload = json.loads(proc.stdout)
    except Exception as e:
        logger.debug("%s: NATS decision emit failed: %s", PROVIDER_NAME, e)
        return None

    rec_id = str(payload.get("record_id") or "").strip()
    rel_path = str(payload.get("path") or "").strip()
    branch = str(payload.get("branch") or "").strip()
    rec_title = str(payload.get("title") or title).strip()
    pr_markdown = str(payload.get("pr_markdown") or "").strip()
    event_obj = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    repo = str(event_obj.get("repo") or "").strip()
    if rec_id and rel_path and branch and pr_markdown:
        card_id = _file_memory_pr_card(
            record_id=rec_id,
            title=rec_title,
            rel_path=rel_path,
            branch=branch,
            pr_markdown=pr_markdown,
            repo=repo,
        )
        if card_id:
            payload["card_id"] = card_id
    return payload
