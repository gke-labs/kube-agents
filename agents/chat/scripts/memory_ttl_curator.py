#!/usr/bin/env python3
"""Keep the memory bank bounded without forgetting what it learned.

Hindsight never forgets. There is no TTL, no decay and no eviction anywhere in
its bank configuration or its API: a fact retained in 2026 is still a live row,
still an input to consolidation, and still occupying disk in 2036. Left alone,
the bank grows monotonically for as long as the deployment lives.

The obvious fix — expire old facts — destroys more than it removes, and not for
the reason you would guess. Hindsight keeps two layers in one table: the raw
facts extracted from what was said (`fact_type` `world`/`experience`), and the
consolidated `observation` layer the LLM maintains on top of them. Observations
are what recall actually returns; the Hindsight provider asks for
`types=["observation"]` and nothing else. But an observation is *derived*, and
Hindsight enforces that strictly — from `delete_stale_observations_for_memories`
in the engine's retain pipeline:

    For each observation referencing any of `fact_ids`: delete the observation
    row (its text is stale once even one source memory disappears), and reset
    `consolidated_at = NULL` on the surviving sources so they get
    re-consolidated.

Every removal path runs that: invalidating a fact, deleting a fact, deleting a
document. So "retire the old evidence, keep the conclusion" is not something the
API can be asked for directly. Retire a fact and its observations go with it,
rebuilt afterwards from whatever facts remain. If nothing remains, the knowledge
is gone.

**Distill, then retire.** Write the observation layer back down into the fact
layer *first*, as fresh facts, and only then retire the aged cohort. The old
observations are destroyed as always — and immediately reconsolidated from the
checkpoints, which say the same thing with a current date. What the bank knows
survives; what it is holding as evidence for that knowledge does not.

Two properties fall out of doing it in that order:

- **Age stops being a correctness question.** On its own, age is a bad staleness
  signal: re-confirming a fact writes a *new* row and bumps the observation's
  `proof_count` rather than refreshing the original's date, so a plain sweep
  retires claims that are still perfectly true. Under distill-then-retire that is
  harmless — the claim already survives in the checkpoint. Age only has to be a
  good guess about which *rows* are redundant, not about which *facts* are false.
- **Checkpoints are not privileged.** Each run retires the previous run's
  checkpoints once its own have landed — a fresher distillation of the same
  observation layer makes the older one pure redundancy — so exactly one
  generation is ever live. Without that, a weekly run against a six-month TTL
  would leave twenty-six near-identical copies of everything in the bank,
  inflating `proof_count` and crowding recall with its own exhaust.

Checkpoints are written under the `checkpoint` retain strategy, which
`kube_agents_memory` provisions on the bank and which pins extraction to
`chunks`. That matters: Hindsight's default extraction would re-summarise the
observation, and re-summarising a summary every cycle is a game of telephone that
walks the bank away from what was actually said. In `chunks` mode the chunk is
stored as it arrived, so one checkpoint in is one fact out, unchanged.

`verbatim` was the obvious choice and is the wrong one. It preserves the text too,
but it still calls the extraction LLM to attach entities and dates — and asks it to
re-emit the observation inside a JSON response schema, which at scale fails often
enough (5% of observations in the re-test) to abort every pass at the safety check.
`chunks` runs no LLM at all, so the write is fast and cannot fail that way; the
price is that checkpoints carry no extracted entities and the graph retriever
cannot see them. See docs/designs/memory.md for the run that established this.

**Every checkpoint must land back in the scope it came from.** One bank holds
every user's memories, kept apart by a scope tag (`user:<id>`, or `scope:shared`
for organisation-wide knowledge), and recall matches tags with `any_strict` —
which returns tagged rows *only*. A checkpoint written without its scope tag is
therefore not merely mis-filed: it consolidates into an untagged observation that
no recall will ever match, so the distil reports success and the knowledge is
gone at the next retire. Each checkpoint carries its source observation's tags
and pins `observation_scopes` to the scope tag among them, and an observation
that carries no scope tag at all aborts the run rather than being written
blind.

Nothing here is destructive in the irreversible sense. Invalidation moves a row
to an archive table with its reason and its causal edges recorded, and
`PATCH {"state": "valid"}` puts it back.

**Deferred: nothing schedules this yet.** Running it is an operator action, and it
**reports without acting unless told otherwise** — pass `--commit` (or set
`MEMORY_TTL_COMMIT=1`) to make it write. Read
docs/designs/memory.md before you do; retiring the evidence
costs more than it saves while recall reads the observation layer alone.

    kubectl exec -n kubeagents-system deploy/platform-agent-gateway \
        -c platform-agent -- /opt/hermes/.venv/bin/python3 \
        /opt/data/scripts/memory_ttl_curator.py --ttl-days 180

Connection settings come from $HERMES_HOME/hindsight/config.json — the same file
the provider reads — so there is no second place to keep the URL in sync.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Must match kube_agents_memory.CHECKPOINT_STRATEGY / DEFAULT_BANK_ID /
# SHARED_TAG / USER_TAG_PREFIX. Not imported from the plugin: this script runs as
# a bare subprocess with no Hermes profile on the path, while the plugin is
# loaded inside the agent. The curator verifies the strategy is actually present
# on the bank before writing, so a drift between the two is caught at runtime
# rather than producing silently paraphrased checkpoints.
CHECKPOINT_STRATEGY = "checkpoint"
DEFAULT_BANK_ID = "kube-agents-memory"
SHARED_TAG = "scope:shared"
USER_TAG_PREFIX = "user:"

# Checkpoints are marked by their `context`, not by a tag and not by a document
# id.
#
# Not a tag, because tags are what Hindsight scopes consolidation by: a marker
# tag on every checkpoint would put checkpoint-derived observations in a
# different scope from the live facts on the same subject, so the two would
# never merge and the bank would carry two parallel accounts of everything.
#
# Not a document id, because the caller-supplied one is not what comes back — a
# unit's `document_id` is a server-generated UUID. `context` is stored and
# returned verbatim, which is what the post-write verification needs.
CHECKPOINT_CONTEXT = "distilled memory checkpoint (kube-agents TTL curator)"

DEFAULT_TTL_DAYS = 180

# Below this, the bank is not worth curating: the distil-and-reconsolidate cycle
# costs LLM calls, and a bank of a few dozen facts has no crowding problem to
# solve. Guards against the curator doing real work on a fresh deployment.
DEFAULT_MIN_UNITS = 200

PAGE_SIZE = 200
RETIRED_TYPES = ("world", "experience")



def load_hindsight_config() -> dict:
    """Read the provider's own config so this script cannot drift from it.

    Deliberately a few duplicated lines rather than an import of the provider's
    own loader: this runs as a bare subprocess with no Hermes profile on the
    path, so the plugin package is not importable.
    """
    home = os.environ.get("HERMES_HOME", "/opt/data")
    path = Path(home) / "hindsight" / "config.json"
    if not path.exists():
        sys.exit(f"No Hindsight config at {path}. Is the provider deployed?")
    try:
        return json.loads(path.read_text()) or {}
    except (OSError, ValueError) as e:
        sys.exit(f"Could not read {path}: {e}")


def log(message: str) -> None:
    """Report on stderr.

    The cron job is `deliver: local`, where a run with empty stdout is treated as
    silent. Keeping stdout clear means a weekly curation does not turn into a
    weekly chat message, while an operator running this by hand still sees
    everything.
    """
    print(message, file=sys.stderr, flush=True)


class Hindsight:
    """Minimal HTTP client for the operations this script needs.

    Not `hindsight_client`: the generated client has no binding for
    `PATCH /memories/{id}` (the invalidate call this is built around) and its
    `list_memories` cannot filter by curation state, which the retire pass
    depends on. Reaching for raw HTTP for two calls and the client for the rest
    would be worse than either.
    """

    def __init__(self, base_url: str, api_key: str | None = None, tenant: str = "default"):
        self._base = base_url.rstrip("/")
        self._prefix = f"/v1/{tenant}"
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    # A 500 from `retain` is not in this set, and that omission is the point;
    # see the comment on `retain`.
    RETRY_CODES = (429, 500, 502, 503, 504)

    def call(self, method: str, path: str, body: dict | None = None,
             timeout: int = 300, retries: int = 6,
             retry_codes: tuple[int, ...] | None = None) -> dict:
        """Issue a request, backing off on rate limits.

        Extraction and consolidation both go through the shared LiteLLM pool, and
        a curation pass over a large bank is exactly the kind of burst that pool
        answers with 429s. Retrying is not optional here.
        """
        url = self._base + self._prefix + path
        data = json.dumps(body).encode() if body is not None else None
        codes = self.RETRY_CODES if retry_codes is None else retry_codes
        delay = 5
        for attempt in range(retries):
            request = urllib.request.Request(url, data=data, method=method, headers=self._headers)
            try:
                return json.loads(urllib.request.urlopen(request, timeout=timeout).read() or "{}")
            except urllib.error.HTTPError as e:
                retryable = e.code in codes
                if not retryable or attempt == retries - 1:
                    raise RuntimeError(f"HTTP {e.code} on {method} {path}: {e.read()[:300]!r}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == retries - 1:
                    raise RuntimeError(f"{type(e).__name__} on {method} {path}: {e}") from e
            time.sleep(delay)
            delay = min(delay * 2, 120)
        raise RuntimeError(f"exhausted retries on {method} {path}")  # pragma: no cover

    def bank_config(self, bank_id: str) -> dict:
        return (self.call("GET", f"/banks/{bank_id}/config", timeout=60) or {}).get("config") or {}

    def count(self, bank_id: str) -> int:
        """Total units in the bank, without paging through them to find out."""
        page = self.call("GET", f"/banks/{bank_id}/memories/list?limit=1", timeout=60)
        return int(page.get("total") or 0)

    def units(self, bank_id: str, **filters) -> list[dict]:
        """Every memory unit matching `filters`, paged to exhaustion.

        `memories/list` has no date filter, so age is applied by the caller.
        """
        query = "&".join(f"{k}={v}" for k, v in filters.items() if v is not None)
        found: list[dict] = []
        offset = 0
        while True:
            page = self.call(
                "GET", f"/banks/{bank_id}/memories/list?limit={PAGE_SIZE}&offset={offset}&{query}",
                timeout=120,
            )
            items = page.get("items") or []
            found.extend(items)
            offset += len(items)
            if len(items) < PAGE_SIZE or offset >= int(page.get("total") or 0):
                return found

    def retain(self, bank_id: str, items: list[dict]) -> dict:
        # Synchronous: the retire pass must not start until the checkpoints are
        # durable, and "async accepted" says nothing about that.
        #
        # A 500 is deliberately not retried here, unlike everywhere else. Retain
        # is not atomic and not idempotent: when one item fails extraction the
        # request returns 500 having already persisted the items that succeeded,
        # so a retry re-writes them. Retrying six times on a 295-item call is how
        # a single malformed-JSON response from the extraction LLM turned a bank
        # of 646 units into 1,959, most of them duplicate checkpoints. The
        # transient codes are still worth retrying; this one is deterministic in
        # the content, so retrying only multiplies the damage.
        return self.call("POST", f"/banks/{bank_id}/memories",
                         {"items": items, "async": False}, timeout=900,
                         retry_codes=(429, 502, 503, 504))

    def invalidate(self, bank_id: str, memory_id: str, reason: str) -> dict:
        # The request field is `reason`; the field it lands in — and the one the
        # list endpoint returns — is `invalidation_reason`. Sending the latter is
        # accepted and silently ignored (the model drops unknown fields), which
        # retires the row with no record of why. Verified against a live bank.
        return self.call("PATCH", f"/banks/{bank_id}/memories/{memory_id}",
                         {"state": "invalidated", "reason": reason})

    def consolidate(self, bank_id: str) -> dict:
        return self.call("POST", f"/banks/{bank_id}/consolidate", {}, timeout=120)


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def unit_age_anchor(unit: dict) -> datetime | None:
    """When this row entered the bank.

    `mentioned_at` (ingestion), not `date` (the event the fact is about). A fact
    can describe something from years ago and still have been learned yesterday;
    it is the row's residency that this script is trimming, and `date` may be
    absent entirely for content retained as timeless.
    """
    return parse_time(unit.get("mentioned_at")) or parse_time(unit.get("date"))


def scope_tags(unit: dict) -> list[str]:
    """The isolation tags on a unit — the ones recall filters by.

    Everything else a unit carries is topical or provenance (`session:<id>` and
    friends), and must not be mistaken for a scope: consolidating by a session id
    would make every session its own island.
    """
    tags = unit.get("tags") or []
    return [t for t in tags if t == SHARED_TAG or str(t).startswith(USER_TAG_PREFIX)]


def build_checkpoints(observations: list[dict]) -> tuple[list[dict], list[str]]:
    """Turn the observation layer into retain items, one per observation.

    Returns the items and a list of complaints. A complaint is fatal to the run,
    not to the item: an observation this cannot place is knowledge that would be
    retired without a home, and the caller stops rather than losing it.
    """
    items: list[dict] = []
    problems: list[str] = []
    for observation in observations:
        text = (observation.get("text") or "").strip()
        if not text:
            continue
        scopes = scope_tags(observation)
        if len(scopes) != 1:
            problems.append(
                f"observation {observation.get('id')} has "
                f"{'no scope tag' if not scopes else f'{len(scopes)} scope tags {scopes}'}"
            )
            continue
        items.append({
            "content": text,
            "context": CHECKPOINT_CONTEXT,
            # Every tag, so topical filters keep working; the scope pinned
            # explicitly, so the `session:<id>` tags that ride along on
            # conversational facts cannot fragment it.
            "tags": observation.get("tags") or scopes,
            "observation_scopes": [scopes],
            "strategy": CHECKPOINT_STRATEGY,
        })
    return items, problems


def curate(api: Hindsight, bank_id: str, *, ttl_days: int, min_units: int,
           commit: bool, now: datetime) -> dict:
    """Run one distill-then-retire pass over the bank."""
    summary = {"bank": bank_id, "skipped": None, "distilled": 0, "retired": 0}
    cutoff = now - timedelta(days=ttl_days)

    # Cheapest guard first, so a fresh deployment costs one request.
    total = api.count(bank_id)
    if total < min_units:
        summary["skipped"] = f"{total} units < min {min_units}"
        return summary

    if CHECKPOINT_STRATEGY not in (api.bank_config(bank_id).get("retain_strategies") or {}):
        # Without it Hindsight falls back to `concise`, which rewrites the text.
        # A paraphrased checkpoint is worse than no checkpoint, because the
        # retire pass that follows would trust it.
        summary["skipped"] = f"no {CHECKPOINT_STRATEGY!r} retain strategy on the bank"
        return summary

    candidates = []
    for fact_type in RETIRED_TYPES:
        candidates.extend(api.units(bank_id, type=fact_type, state="valid"))
    # `anchor < now` is redundant for any real TTL but keeps a `--ttl-days 0`
    # test run from sweeping up the checkpoints it is about to write.
    aged = [u for u in candidates
            if (anchor := unit_age_anchor(u)) and anchor < cutoff and anchor < now]
    if not aged:
        summary["skipped"] = f"nothing older than {ttl_days}d"
        return summary

    observations = api.units(bank_id, type="observation")
    if not observations:
        # Retiring now would delete the facts and leave nothing behind. Either
        # consolidation has not run yet or it is failing; both want a human.
        summary["skipped"] = f"{len(aged)} facts are due but the bank has no observations to distil"
        return summary

    items, problems = build_checkpoints(observations)
    if problems:
        # Nothing has been written yet, so stopping here costs only the reads.
        # Proceeding would checkpoint most of the bank and retire all of it.
        summary["skipped"] = (f"aborted: {len(problems)} observation(s) cannot be scoped — "
                              f"{problems[0]}" + (f" (+{len(problems) - 1} more)"
                                                  if len(problems) > 1 else ""))
        return summary

    # Last run's checkpoints. `candidates` was read before anything was written,
    # so every checkpoint in it belongs to an earlier generation by construction
    # — no timestamp comparison, and so no exposure to clock skew between this
    # pod and the Hindsight service.
    superseded = [u for u in candidates if u.get("context") == CHECKPOINT_CONTEXT]
    aged_ids = {u["id"] for u in aged}
    doomed = aged + [u for u in superseded if u["id"] not in aged_ids]

    if not commit:
        summary.update(distilled=len(items), retired=len(doomed), skipped="dry run")
        return summary

    # --- Distil: the observation layer, unchanged, as new facts ----------------
    # One item per call, which is slower than it looks like it needs to be and is
    # not negotiable. Retain fails or succeeds per request, and because the request
    # is neither atomic nor idempotent a failed multi-item call leaves its
    # successful prefix behind — so there is no batch size at which a retry or a
    # per-item fallback does not duplicate rows. That is how a 646-unit bank became
    # 1,959 during the re-test. Sending them one at a time makes a bad item cost
    # exactly that item. The `chunks` extraction mode removes the failure this was
    # first written for (a per-request extraction LLM emitting malformed JSON), but
    # not the non-atomicity, which is a property of the endpoint.
    #
    # Failures are counted, not swallowed: the landed check below still refuses to
    # retire anything unless every checkpoint is durable.
    unwritable = 0
    for index, item in enumerate(items):
        try:
            api.retain(bank_id, [item])
        except RuntimeError as e:
            unwritable += 1
            print(f"  {bank_id}: checkpoint {index} of {len(items)} could not be "
                  f"written — {e}", file=sys.stderr)

    # --- Verify before retiring ------------------------------------------------
    # The whole design rests on the checkpoints existing. If retain reported
    # success but fewer rows landed than were sent, stop: a partial distil
    # followed by a full retire is the one way this script can lose knowledge.
    #
    # Counted as a delta against the pre-write listing, not by timestamp: one
    # item does not always mean one row (`chunks` mode emits a fact per chunk, so
    # an observation longer than the bank's chunk size lands as several), and a
    # delta needs no clock the two services have to agree on.
    landed = len([u for u in api.units(bank_id, type="world", state="valid")
                  if u.get("context") == CHECKPOINT_CONTEXT]) - len(superseded)
    if landed < len(items):
        detail = (f"{unwritable} rejected by retain" if unwritable
                  else "retain reported success")
        summary["skipped"] = (f"aborted before retiring: {landed}/{len(items)} "
                              f"checkpoints landed ({detail})")
        return summary
    summary["distilled"] = landed

    # --- Retire ----------------------------------------------------------------
    reason = f"kube-agents TTL: retained more than {ttl_days}d ago, distilled into observations"
    superseded_reason = "kube-agents TTL: superseded by a fresher checkpoint"
    for unit in doomed:
        api.invalidate(bank_id, unit["id"],
                       reason if unit["id"] in aged_ids else superseded_reason)
        summary["retired"] += 1

    # Invalidation already queues consolidation, but a distil that retired
    # nothing would otherwise leave the checkpoints unconsolidated until the
    # next write to the bank.
    api.consolidate(bank_id)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ttl-days", type=int,
                        default=int(os.environ.get("MEMORY_TTL_DAYS", DEFAULT_TTL_DAYS)),
                        help="Retire facts retained more than this many days ago.")
    parser.add_argument("--min-units", type=int,
                        default=int(os.environ.get("MEMORY_TTL_MIN_UNITS", DEFAULT_MIN_UNITS)),
                        help="Leave the bank alone below this size.")
    parser.add_argument("--bank", action="append", metavar="BANK_ID",
                        help="Bank to curate (repeatable; default: the configured one). "
                             "Mainly for testing against a scratch bank.")
    parser.add_argument("--commit", action="store_true",
                        default=os.environ.get("MEMORY_TTL_COMMIT", "") not in ("", "0", "false"),
                        help="Actually write. Without it the run only reports.")
    args = parser.parse_args()

    if args.ttl_days < 0:
        parser.error("--ttl-days cannot be negative")

    config = load_hindsight_config()
    # Same precedence the plugin uses: the file first, the environment second.
    # The shipped config carries no api_url — the operator derives the endpoint
    # from the namespace and passes HINDSIGHT_API_URL — but an operator-written
    # file still wins, so the two never disagree silently.
    api_url = (str(config.get("api_url") or "").strip()
               or os.environ.get("HINDSIGHT_API_URL", "").strip())
    if not api_url:
        sys.exit("No Hindsight endpoint: the config has no api_url and "
                 "HINDSIGHT_API_URL is unset.")
    api = Hindsight(api_url, config.get("api_key") or config.get("apiKey"))

    now = datetime.now(timezone.utc)
    bank_ids = args.bank or [str(config.get("bank_id") or "").strip() or DEFAULT_BANK_ID]

    mode = "commit" if args.commit else "dry run"
    log(f"curating {len(bank_ids)} bank(s), ttl={args.ttl_days}d, {mode}")
    failed = False
    for bank_id in bank_ids:
        try:
            result = curate(api, bank_id, ttl_days=args.ttl_days, min_units=args.min_units,
                            commit=args.commit, now=now)
        except RuntimeError as e:
            failed = True
            log(f"  {bank_id}: FAILED — {e}")
            continue
        note = f" ({result['skipped']})" if result["skipped"] else ""
        log(f"  {bank_id}: distilled={result['distilled']} retired={result['retired']}{note}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
