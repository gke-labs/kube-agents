"""Where baseline evidence physically lives.

Two backends, one record format, identical semantics. The scorer never touches
a file or an object; it asks a backend for ``(case_id, label, text)`` triples
and gets the same thing either way.

LOCAL is the default and stays the default. The store travels with the
checkout, so a developer running the gate needs no credential and no network,
and every unit test is hermetic. It is also how the format is documented.

GCS is the intended production home, for one reason: on the local backend
something has to *commit* the file, and the CI job that measures the
evidence has no push credential. Every way of giving it one -- a bot with write
access to ``main``, a pull request per merge, a weekly batched pull request --
was worse than the problem. See ``docs/designs/eval-scorer.md``.

The GCS layout is one immutable object per batch, filed under its version key::

    gs://<bucket>/<prefix>/<case-id>/<setup-id>/<judge-model>/<sv>-f<n>-v<n>/<stamp>-<build>.jsonl

never appended to, because the grant this is built for is
``roles/storage.objectCreator`` -- create yes, overwrite and delete no. That
makes append-only an IAM guarantee rather than a convention, which is strictly
stronger than git, where a force-push can rewrite history. Object names begin
with an ISO-8601 UTC stamp so lexical order is chronological and the reader
gets newest-first for free.

THE KEY IS IN THE PATH because evidence is only ever pooled within one key --
``evidence_for()`` discards every line measured on different software. Filing
by key means a prefix stops growing the moment the key changes: a model bump
freezes the old directory forever and starts a new one, so no single prefix
grows without bound while the software moves. It also makes the store
navigable, which a hash would not: listing a case shows which setups have been
screened, and ``*/gemini-3.1-pro-preview/**`` finds every case a given judge
scored.

THE PATH IS AN INDEX, NEVER THE TRUTH. Every record carries its own ``key`` and
the reader filters on that, not on where the object sat. A name that disagrees
with its contents loses, which is the only safe way round for something a
future writer could get wrong.

``gcloud storage`` is shelled out to rather than importing
``google-cloud-storage``. The bench package has no GCP dependency today and
this is not worth acquiring one for; ``gcloud`` is already present wherever
this runs.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: How many of the newest objects a GCS read will pull, per case *per key*.
#:
#: Per key, not per case, and that matters: capping a case as a whole would
#: sort its keys' directories against each other and could drop the current
#: key's evidence to keep a superseded key's, silently de-admitting the case.
#:
#: 200 objects is roughly 600 runs at three repetitions -- two orders of
#: magnitude past the twenty the admission bar wants -- so this never binds in
#: practice. It is here to bound a read that would otherwise grow without limit
#: as a key accumulates years of history. When it does bind, the reader says
#: so: a cap that is silent reads as "I considered everything" when it did not.
DEFAULT_MAX_OBJECTS = 200

#: Seconds before a `gcloud storage` call is treated as unreachable.
DEFAULT_TIMEOUT = 60


class StoreUnreachable(RuntimeError):
    """The store could not be reached at all.

    Deliberately distinct from a parse error, and the two are handled
    differently by the gate. Bytes that arrived and will not parse are a
    corrupt store and stop the job; a store that cannot be reached degrades to
    advisory with a loud banner, because a network blip redding every pull
    request is the failure mode that gets gates switched off.
    """


@dataclass(frozen=True)
class EvidenceSource:
    """One case's raw lines, and something to name it by in an error."""

    case_id: str
    label: str
    text: str


def is_gcs(location: str | Path) -> bool:
    return str(location).startswith("gs://")


def _sanitize(text: str) -> str:
    """One path segment. Anything that could add a level or confuse a shell goes.

    Dots survive, because the judge model is spelled with them and the point of
    this layout is that a human can read it.
    """
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in str(text))


def _key_segments(key: dict | None) -> list[str]:
    """The version key as directories: setup, judge, then the three integers.

    Readable on purpose. `ls` on a case shows which setups were screened, and
    `*/<judge>/**` finds every case a judge scored -- neither of which a hash
    would answer without opening a record.

    A record with no key is filed under ``unkeyed/`` rather than dropped.
    ``bench-gate record`` already skips those, so this is the belt to that
    braces: the writer must never be the reason a merge to main loses data.
    """
    if not key:
        return ["unkeyed"]
    versions = (
        f"{key.get('scoring_version') or 'unknown'}"
        f"-f{key.get('fleet')}-v{key.get('verifiers')}"
    )
    return [
        _sanitize(key.get("setup_id") or "unknown-setup"),
        _sanitize(key.get("judge_model") or "unknown-judge"),
        _sanitize(versions),
    ]


def max_objects_from_env() -> int:
    """``EVAL_BASELINE_MAX_OBJECTS``, or the default.

    A junk or non-positive value falls back rather than raising. This bounds a
    read; it is not a correctness knob, and a typo in it must not be the reason
    a merge to main cannot be graded.
    """
    raw = os.environ.get("EVAL_BASELINE_MAX_OBJECTS", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_OBJECTS
    return value if value > 0 else DEFAULT_MAX_OBJECTS


def open_backend(location: str | Path) -> LocalBackend | GcsBackend:
    """Pick a backend from the location string. ``gs://`` means GCS."""
    if is_gcs(location):
        return GcsBackend(str(location), max_objects=max_objects_from_env())
    return LocalBackend(location)


class LocalBackend:
    """``<directory>/<case-id>.jsonl``, appended to in place."""

    def __init__(self, directory: str | Path):
        self.root = Path(directory)

    def describe(self) -> str:
        return str(self.root)

    def sources(self) -> list[EvidenceSource]:
        """Every ``<case>.jsonl`` in the directory.

        A missing directory is an empty store, not an error: that is the state
        a fresh checkout is in before anything has been screened.
        """
        if not self.root.is_dir():
            return []

        # A leftover `<case>.json` is refused rather than ignored. Skipping it
        # would read as "this case has never been screened", which silently
        # de-admits the case instead of saying the format changed.
        for stray in sorted(self.root.glob("*.json")):
            if stray.name != "VERSIONS.json":
                raise ValueError(
                    f"{stray}: the store is JSONL now; rename it to "
                    f"{stray.stem}.jsonl, one record per line"
                )

        found: list[EvidenceSource] = []
        for path in sorted(self.root.glob("*.jsonl")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(f"{path}: cannot read: {exc}") from exc
            found.append(EvidenceSource(path.stem, str(path), text))
        return found

    def append(self, case_id: str, line: str) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / f"{case_id}.jsonl"

        # A file whose last line has no newline would swallow the next append
        # into it, turning two records into one unparseable one. Cheap to
        # prevent, and the only way it happens -- a half-written append -- is
        # exactly the case where nobody is watching.
        prefix = ""
        if path.exists() and path.stat().st_size:
            with path.open("rb") as fh:
                fh.seek(-1, os.SEEK_END)
                if fh.read(1) != b"\n":
                    prefix = "\n"

        with path.open("a", encoding="utf-8") as fh:
            fh.write(prefix + line + "\n")
        return str(path)


class GcsBackend:
    """One immutable object per batch under ``gs://<bucket>/<prefix>/<case>/``."""

    def __init__(
        self,
        location: str,
        *,
        max_objects: int = DEFAULT_MAX_OBJECTS,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.location = location.rstrip("/")
        self.max_objects = max_objects
        self.timeout = timeout
        self.truncated: dict[str, int] = {}

    def describe(self) -> str:
        return self.location

    def _run(self, args: list[str], stdin: str | None = None) -> str:
        try:
            done = subprocess.run(
                ["gcloud", "storage", *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except FileNotFoundError as exc:  # no gcloud on PATH at all
            raise StoreUnreachable(f"gcloud not available: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise StoreUnreachable(
                f"gcloud storage {args[0]} timed out after {self.timeout}s"
            ) from exc
        if done.returncode != 0:
            raise StoreUnreachable(
                f"gcloud storage {args[0]} failed ({done.returncode}): "
                f"{(done.stderr or '').strip()[:400]}"
            )
        return done.stdout

    def _list(self) -> list[str]:
        """Every object URL under the prefix, or [] if the prefix is empty.

        An empty prefix is an empty store. gcloud reports "matched no objects"
        as a non-zero exit, which must not be mistaken for the bucket being
        unreachable -- one is the ordinary state before anything is recorded,
        the other disarms the gate.
        """
        try:
            out = self._run(["ls", f"{self.location}/**"])
        except StoreUnreachable as exc:
            if "matched no objects" in str(exc).lower():
                return []
            raise
        return [
            line.strip()
            for line in out.splitlines()
            if line.strip().startswith("gs://") and line.strip().endswith(".jsonl")
        ]

    def sources(self) -> list[EvidenceSource]:
        """Every case's objects, grouped by case and ordered chronologically.

        The case is the first segment under the prefix, whatever the depth
        below it, so this reads the key-partitioned layout and the flat one
        the same way.

        Ordering survives the nesting because a key determines its directory:
        all of one key's records land in one directory and sort by stamp
        within it. ``evidence_for()`` filters to a single key before it walks,
        so it never sees the interleaving between directories.
        """
        by_case: dict[str, dict[str, list[str]]] = {}
        for url in self._list():
            if not url.startswith(self.location + "/"):
                continue
            relative = url[len(self.location) + 1 :]
            if "/" not in relative:  # an object sitting directly under the prefix
                continue
            case_id = relative.split("/", 1)[0]
            parent = url.rsplit("/", 1)[0]
            by_case.setdefault(case_id, {}).setdefault(parent, []).append(url)

        found: list[EvidenceSource] = []
        for case_id, groups in sorted(by_case.items()):
            urls: list[str] = []
            for _, group in sorted(groups.items()):
                group.sort()  # names start with an ISO stamp: chronological
                if len(group) > self.max_objects:
                    dropped = len(group) - self.max_objects
                    self.truncated[case_id] = self.truncated.get(case_id, 0) + dropped
                    group = group[-self.max_objects :]
                urls.extend(group)
            text = self._run(["cat", *urls])
            found.append(EvidenceSource(case_id, f"{self.location}/{case_id}/", text))
        return found

    def append(self, case_id: str, line: str) -> str:
        """Write one new object. Never overwrites, by construction and by IAM.

        The directory comes from the record's own version key and the name from
        its own ``recorded_at``, so the object files itself under the software
        it was measured on and sorts into place chronologically. The build id
        keeps two batches in the same second from colliding.
        """
        doc: dict = {}
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                doc = parsed
        except ValueError:
            pass

        stamp = str(doc.get("recorded_at") or "unknown")
        build = os.environ.get("BUILD_ID") or os.environ.get("PROW_JOB_ID") or "local"
        name = _sanitize(f"{stamp}-{build}")
        parts = [self.location, _sanitize(case_id), *_key_segments(doc.get("key"))]
        url = "/".join([*parts, f"{name}.jsonl"])
        self._run(["cp", "-", url], stdin=line + "\n")
        return url
