#!/usr/bin/env python3
"""Move a file-based memory store into Hindsight at startup, then delete it.

A deployment that predates the retrieval provider has its memory sitting in
Markdown on the PVC, and switching the provider does not move it: the new
provider never reads those files, so the day the image rolls, everything the
agent had learned becomes invisible while remaining perfectly intact on disk.
That is the worst of both — the knowledge is neither reachable nor gone, and
nobody notices until somebody asks a question that used to work.

This script closes that gap. It runs from the entrypoint on every start, does
nothing at all when there is nothing to move, and otherwise reads each store,
retains its entries into the bank under the right scope, verifies they landed,
and only then removes the file.

**Two layouts, because there were two providers.**

    $HERMES_HOME/MEMORY.md                  Hermes built-in, shared
    $HERMES_HOME/USER.md                    Hermes built-in, personal
    $HERMES_HOME/memories/MEMORY.md         multiuser_memory, shared
    $HERMES_HOME/memories/users/<id>.md     multiuser_memory, personal

Both use the same `\\n§\\n` entry delimiter, so parsing is shared; what differs
is who each file belongs to.

**Whose memories are these?** A shared store is unambiguous — everything in it
becomes `scope:shared`. A personal store has to end up tagged `user:<id>` with
the *same* id the live provider will tag that person with, or the migration
succeeds and the person still sees nothing.

`multiuser_memory` named its files `<sanitized>_<sha256(raw)[:12]>.md`, where the
sanitizer replaced every character outside `[alnum-_.]` with `_`. The sanitized
half is lossy — `alice@corp.com` and `alice_corp.com` produce the same stem — but
the twelve hex characters are a checksum over the *raw* id, so the original can be
recovered by search and then confirmed: substitute candidate characters back into
the underscore positions, hash, and accept only an exact match. Where nothing
matches, the file is left alone and reported. Guessing an owner is worse than not
migrating: a personal memory filed under the wrong tag is a leak, and one filed
under a tag nobody carries is a silent loss.

The built-in `USER.md` carries no identity at all — it belongs to whoever ran a
single-user agent. It is migrated only when `--user-id` (or
`MEMORY_IMPORT_USER_ID`) says who that was, and skipped otherwise.

**One retain call per entry.** Hindsight collapses a multi-item retain into a
single document that keeps one item's `context` as the label for all of them, so
a batched migration would arrive with most of its provenance overwritten. Each
entry is therefore its own call, carrying a `context` that names the file it came
from and a short digest of the entry itself.

**That context is also the resume log.** The digest inside it identifies the
entry by content rather than by position, so a run that is interrupted — a
restart mid-import, a Hindsight outage halfway through — picks up exactly where
it stopped, and an entry already in the bank is never retained twice. Nothing
local has to be trusted for this; the bank is the record.

**Deletion is gated on verification, per entry.** A file is removed only once
every one of its entries is present in the bank. An entry the extractor discarded
as non-durable leaves no unit, so the file stays and the run says which entry it
was. That is deliberately conservative: this script's one unrecoverable mistake
would be deleting the last copy of something.

What survives the deletion is a receipt under
`$HERMES_HOME/hindsight/imported/`, holding the source path, its sha256, the
entry count and the timestamp — and none of the text, since leaving the content
readable on the PVC is the thing being undone.

Observations are not part of the gate. Recall reads the observation layer, so a
migrated memory is not answerable until consolidation runs; consolidation is
triggered at the end of the run, but it is derived state that Hindsight rebuilds
from the facts on its own. The facts are what must not be lost, and the facts are
what is checked.

Run by hand against a scratch bank:

    kubectl exec -n kubeagents-system deploy/platform-agent-gateway \\
        -c platform-agent -- /opt/hermes/.venv/bin/python3 \\
        /opt/data/scripts/memory_file_import.py --dry-run

Connection settings come from $HERMES_HOME/hindsight/config.json — the same file
the provider reads — so there is no second place to keep the URL in sync.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Must match kube_agents_memory.DEFAULT_BANK_ID / SHARED_TAG / USER_TAG_PREFIX /
# PERSONAL_STRATEGY / SHARED_STRATEGY, and tools/memory_tool.ENTRY_DELIMITER.
# Not imported from either: this runs as a bare subprocess from the entrypoint
# with no Hermes profile on the path.
DEFAULT_BANK_ID = "kube-agents-memory"
SHARED_TAG = "scope:shared"
USER_TAG_PREFIX = "user:"
PERSONAL_STRATEGY = "personal"
SHARED_STRATEGY = "shared"
ENTRY_DELIMITER = "\n§\n"

# The `context` every migrated entry carries, so the provenance of a fact that
# came from the old store is legible in the bank and in recall output.
CONTEXT_SUFFIX = "migrated from the file memory store"

# Candidate characters for the underscores `multiuser_memory` wrote into its
# filenames, most likely first. Only characters outside `[alnum-_.]` were
# replaced, so `.` is absent; `_` leads because a literal underscore in the raw
# id is the case that needs no substitution at all.
UNDERSCORE_CANDIDATES = ("_", "@", "+", ":", " ", "/", "|", ",", ";", "#", "&", "=", "!", "*")

# Reconstruction is a product over the underscore positions. Six positions at
# fourteen candidates is a few million hashes — seconds, and already far past any
# real chat identity. Beyond that, report rather than burn the pod's startup.
MAX_RECOVERABLE_UNDERSCORES = 6

PAGE_SIZE = 200


def log(message: str) -> None:
    """Report on stderr, which is where the entrypoint's output goes."""
    print(message, file=sys.stderr, flush=True)


def load_hindsight_config(home: Path) -> dict:
    """Read the provider's own config so this script cannot drift from it."""
    path = home / "hindsight" / "config.json"
    if not path.exists():
        sys.exit(f"No Hindsight config at {path}. Is the provider deployed?")
    try:
        return json.loads(path.read_text()) or {}
    except (OSError, ValueError) as e:
        sys.exit(f"Could not read {path}: {e}")


class Hindsight:
    """Minimal HTTP client for the three operations this script needs.

    The same shape as `memory_ttl_curator.Hindsight`, and separate for the same
    reason the curator gives: two bare subprocesses that cannot import each other
    or the plugin, each carrying only the calls it makes.
    """

    def __init__(self, base_url: str, api_key: str | None = None, tenant: str = "default"):
        self._base = base_url.rstrip("/")
        self._prefix = f"/v1/{tenant}"
        self._headers = {"Content-Type": "application/json"}
        if api_key:
            self._headers["Authorization"] = f"Bearer {api_key}"

    def call(self, method: str, path: str, body: dict | None = None,
             timeout: int = 300, retries: int = 6) -> dict:
        """Issue a request, backing off on rate limits.

        Extraction runs through the shared LiteLLM pool, and a migration is a
        burst of retains against it. Retrying is not optional here.
        """
        url = self._base + self._prefix + path
        data = json.dumps(body).encode() if body is not None else None
        delay = 5
        for attempt in range(retries):
            request = urllib.request.Request(url, data=data, method=method, headers=self._headers)
            try:
                return json.loads(urllib.request.urlopen(request, timeout=timeout).read() or "{}")
            except urllib.error.HTTPError as e:
                retryable = e.code in (429, 500, 502, 503, 504)
                if not retryable or attempt == retries - 1:
                    raise RuntimeError(f"HTTP {e.code} on {method} {path}: {e.read()[:300]!r}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt == retries - 1:
                    raise RuntimeError(f"{type(e).__name__} on {method} {path}: {e}") from e
            time.sleep(delay)
            delay = min(delay * 2, 120)
        raise RuntimeError(f"exhausted retries on {method} {path}")  # pragma: no cover

    def landed(self, bank_id: str) -> set[str]:
        """Every migration key already in the bank.

        This is the resume log — read from the bank rather than from anything
        local, so an entry that is present is known to be present rather than
        recorded as having been sent.
        """
        found: set[str] = set()
        offset = 0
        while True:
            page = self.call("GET", f"/banks/{bank_id}/memories/list?limit={PAGE_SIZE}&offset={offset}",
                             timeout=120)
            items = page.get("items") or []
            for unit in items:
                key = key_of(str(unit.get("context") or ""))
                if key:
                    found.add(key)
            offset += len(items)
            if len(items) < PAGE_SIZE or offset >= int(page.get("total") or 0):
                return found

    def retain(self, bank_id: str, item: dict) -> dict:
        # One item, synchronously: "async accepted" would tell us nothing about
        # whether the entry is safe to delete from disk.
        return self.call("POST", f"/banks/{bank_id}/memories",
                         {"items": [item], "async": False}, timeout=900)

    def consolidate(self, bank_id: str) -> dict:
        return self.call("POST", f"/banks/{bank_id}/consolidate", {}, timeout=120)


def sanitize_user_id(user_id: str) -> str:
    """Mirror of `kube_agents_memory.sanitize_user_id`.

    Must stay identical: this produces the tag the migrated entries are filed
    under, and the provider produces the tag they are read back with. The
    trailing digest is what makes the tag collision-free; see the provider's
    docstring for why a readable-only tag is not safe here.
    """
    raw = str(user_id or "").strip()
    if not raw:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", raw)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-_")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{cleaned}_{digest}" if cleaned else digest


def recover_raw_user_id(stem: str) -> str | None:
    """Recover the identity behind a `multiuser_memory` filename, or None.

    The filename is `<sanitized>_<sha256(raw)[:12]>`, and the sanitizer mapped
    every character outside `[alnum-_.]` to `_` before stripping the edges. The
    hash is over the raw id, so it decides: substitute candidates into the
    underscore positions until one reconstruction hashes to the recorded prefix.

    Returns None when nothing matches — the file then stays where it is. The
    edges are the honest limit: `.strip("_")` discarded any leading or trailing
    separator outright, and no search recovers a character that was deleted
    rather than replaced.
    """
    match = re.fullmatch(r"(?P<body>.*)_(?P<digest>[0-9a-f]{12})", stem)
    if not match:
        return None
    body, digest = match.group("body"), match.group("digest")

    positions = [i for i, c in enumerate(body) if c == "_"]
    if len(positions) > MAX_RECOVERABLE_UNDERSCORES:
        return None

    chars = list(body)
    for substitution in itertools.product(UNDERSCORE_CANDIDATES, repeat=len(positions)):
        for position, char in zip(positions, substitution):
            chars[position] = char
        candidate = "".join(chars)
        if hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12] == digest:
            return candidate
    return None


def read_entries(path: Path) -> list[str]:
    """Split a store into its entries, dropping the empties."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise RuntimeError(f"could not read {path}: {e}") from e
    if not text:
        return []
    return [entry.strip() for entry in text.split(ENTRY_DELIMITER) if entry.strip()]


class Source:
    """One file to migrate, and the scope everything in it belongs to."""

    def __init__(self, path: Path, label: str, tag: str, strategy: str):
        self.path = path
        self.label = label
        self.tag = tag
        self.strategy = strategy

    def context_for(self, entry: str, index: int) -> str:
        """The provenance label a migrated entry carries into the bank.

        Written to be read by a person — which file, which entry — while
        embedding the digest that `key_of` reads back out.
        """
        return f"{self.label} entry {index + 1} [{digest_of(entry)}] ({CONTEXT_SUFFIX})"

    def key_for(self, entry: str) -> str:
        """What a resumed run matches on: the file, and the entry's content.

        Content-addressed and position-free, so a store that was edited between
        an interrupted run and its resume — an entry removed, the rest shifted
        up — resolves to what already landed rather than re-importing the tail
        under new positions.
        """
        return f"{self.label}#{digest_of(entry)}"


def digest_of(entry: str) -> str:
    return hashlib.sha256(entry.encode("utf-8")).hexdigest()[:8]


CONTEXT_PATTERN = re.compile(
    r"^(?P<label>.+) entry \d+ \[(?P<digest>[0-9a-f]{8})\] \(" + re.escape(CONTEXT_SUFFIX) + r"\)$"
)


def key_of(context: str) -> str | None:
    """Recover a migration key from a unit's context, or None if it is not ours.

    The bank holds far more than migrated entries; everything else has a context
    this does not match, and is ignored.
    """
    match = CONTEXT_PATTERN.match(context)
    return f"{match.group('label')}#{match.group('digest')}" if match else None


def discover(home: Path, builtin_user_id: str | None) -> tuple[list[Source], list[str]]:
    """Find the stores on this volume. Returns the sources and what was skipped."""
    sources: list[Source] = []
    skipped: list[str] = []

    for path, label in ((home / "MEMORY.md", "MEMORY.md"),
                        (home / "memories" / "MEMORY.md", "memories/MEMORY.md")):
        if path.is_file():
            sources.append(Source(path, label, SHARED_TAG, SHARED_STRATEGY))

    builtin_user = home / "USER.md"
    if builtin_user.is_file():
        if sanitize_user_id(builtin_user_id or ""):
            tag = f"{USER_TAG_PREFIX}{sanitize_user_id(builtin_user_id)}"
            sources.append(Source(builtin_user, "USER.md", tag, PERSONAL_STRATEGY))
        else:
            skipped.append(
                "USER.md carries no identity — the built-in store is single-user. "
                "Re-run with --user-id <who it belonged to> to migrate it; it is "
                "left in place until then."
            )

    users_dir = home / "memories" / "users"
    if users_dir.is_dir():
        for path in sorted(users_dir.glob("*.md")):
            raw = recover_raw_user_id(path.stem)
            if raw is None:
                skipped.append(
                    f"memories/users/{path.name}: could not recover the identity behind "
                    f"the filename, so its owner's tag is unknown. Left in place."
                )
                continue
            sanitized = sanitize_user_id(raw)
            if not sanitized:
                # Nothing survived sanitizing, so there is no tag to file this
                # under that the provider would ever read back.
                skipped.append(
                    f"memories/users/{path.name}: the recovered identity sanitizes to "
                    f"nothing, so it has no reachable tag. Left in place."
                )
                continue
            sources.append(Source(path, f"memories/users/{path.name}",
                                  f"{USER_TAG_PREFIX}{sanitized}", PERSONAL_STRATEGY))

    return sources, skipped


def receipt_path(home: Path, source: Source) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", source.label).strip("-")
    return home / "hindsight" / "imported" / f"{slug}.json"


def write_receipt(home: Path, source: Source, digest: str, entries: int) -> None:
    """Record that the file was migrated — counts and hashes, never the text.

    The point of the migration is that the content stops being readable on the
    volume, so a receipt that quoted it would undo the deletion it documents.
    """
    path = receipt_path(home, source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "source": source.label,
        "sha256": digest,
        "tag": source.tag,
        "entries": entries,
        "migrated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2) + "\n", encoding="utf-8")


def migrate(api: Hindsight, bank_id: str, home: Path, source: Source, *,
            landed: set[str], commit: bool) -> dict:
    """Import one file and, if every entry is accounted for, delete it."""
    result = {"source": source.label, "entries": 0, "imported": 0, "already": 0,
              "missing": 0, "removed": False, "note": None}

    entries = read_entries(source.path)
    result["entries"] = len(entries)
    if not entries:
        # An empty store holds nothing to lose, so it can go without a retain.
        if commit:
            source.path.unlink()
            result["removed"] = True
        result["note"] = "empty"
        return result

    keys = [source.key_for(entry) for entry in entries]

    for index, entry in enumerate(entries):
        if keys[index] in landed:
            result["already"] += 1
            continue
        if not commit:
            result["imported"] += 1
            continue
        api.retain(bank_id, {
            "content": entry,
            "context": source.context_for(entry, index),
            "tags": [source.tag],
            # Pinned explicitly: an untagged observation is one that `any_strict`
            # recall will never match, which is a silent loss dressed as success.
            "observation_scopes": [[source.tag]],
            "strategy": source.strategy,
        })
        result["imported"] += 1

    if not commit:
        result["note"] = "dry run"
        return result

    # Re-read the bank rather than trusting the loop above. A retain that
    # returned 200 but produced no unit — the extractor finding nothing durable
    # in the entry — is exactly the case the delete must not be allowed past.
    present = api.landed(bank_id)
    landed.update(present)
    absent = [i + 1 for i, key in enumerate(keys) if key not in present]
    if absent:
        result["missing"] = len(absent)
        shown = ", ".join(str(i) for i in absent[:5])
        result["note"] = (f"kept: entr{'y' if len(absent) == 1 else 'ies'} {shown}"
                          f"{' and more' if len(absent) > 5 else ''} produced no memory unit")
        return result

    digest = hashlib.sha256(source.path.read_bytes()).hexdigest()
    write_receipt(home, source, digest, len(entries))
    source.path.unlink()
    result["removed"] = True
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", type=Path,
                        default=Path(os.environ.get("HERMES_HOME", "/opt/data")),
                        help="Profile directory holding the stores.")
    parser.add_argument("--bank", default=None,
                        help="Bank to import into (default: the provider's constant).")
    parser.add_argument("--user-id", default=os.environ.get("MEMORY_IMPORT_USER_ID") or None,
                        help="Owner of the built-in USER.md. Without it that file is skipped.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report what would move, write nothing, delete nothing.")
    args = parser.parse_args()

    sources, skipped = discover(args.home, args.user_id)
    for note in skipped:
        log(f"[memory-import] SKIP {note}")
    if not sources:
        # The common case by far: every start after the one that migrated.
        return

    config = load_hindsight_config(args.home)
    # Same precedence the plugin uses: the file first, the environment second.
    # The shipped config carries no api_url — the operator derives the endpoint
    # from the namespace and passes HINDSIGHT_API_URL — but an operator-written
    # file still wins, so the two never disagree silently.
    api_url = (str(config.get("api_url") or "").strip()
               or os.environ.get("HINDSIGHT_API_URL", "").strip())
    if not api_url:
        sys.exit("No Hindsight endpoint: the config has no api_url and "
                 "HINDSIGHT_API_URL is unset.")
    # The bank name is the provider's constant, not a config value, for the
    # reason `client.apply_scoping` gives: a stale bank_id on the PVC used to win.
    bank_id = args.bank or DEFAULT_BANK_ID
    api = Hindsight(api_url, config.get("api_key") or config.get("apiKey"))

    commit = not args.dry_run
    log(f"[memory-import] {len(sources)} file store(s) to migrate into {bank_id}"
        f"{'' if commit else ' (dry run)'}")

    try:
        landed = api.landed(bank_id)
    except RuntimeError as e:
        # Hindsight not up yet is the likely cause on a cold start. Leave every
        # file where it is; the next start tries again.
        log(f"[memory-import] FAILED to read the bank, nothing migrated — {e}")
        sys.exit(1)

    failed = False
    for source in sources:
        try:
            result = migrate(api, bank_id, args.home, source, landed=landed, commit=commit)
        except (RuntimeError, OSError) as e:
            failed = True
            log(f"[memory-import] {source.label}: FAILED — {e}")
            continue
        note = f" ({result['note']})" if result["note"] else ""
        log(f"[memory-import] {source.label} -> {source.tag}: "
            f"{result['entries']} entries, {result['imported']} imported, "
            f"{result['already']} already present, "
            f"{'removed' if result['removed'] else 'kept'}{note}")
        failed = failed or bool(result["missing"])

    if commit:
        # Recall reads observations, so nothing migrated is answerable until this
        # runs. It is not part of the delete gate — Hindsight rebuilds the layer
        # from the facts — but leaving it to the next write would mean the agent
        # comes up apparently having forgotten everything.
        try:
            api.consolidate(bank_id)
        except RuntimeError as e:
            log(f"[memory-import] WARN consolidation did not start: {e}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
