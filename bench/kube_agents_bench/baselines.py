# Copyright 2026 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The checked-in baseline store, the version key, and computed admission.

WHAT A BASELINE IS FOR. Two of the four suite rules need to know how a case
behaves on ``main``: collapse (rung 4) may only red a case that has PROVED it
passes reliably, and the aggregate rule compares this pull request's pass rate
against main's. Neither question can be answered from the pull request's own
run, so the answers are screened once and checked in under
``bench/baselines/``.

THE STORE IS APPEND-ONLY JSONL, one ``<case-id>.jsonl`` per case, one screening
campaign per line. Nothing is ever rewritten: a re-screen appends a line and
the older lines stay, so the file is the case's history and not just its
current state. That matters for three reasons. Re-screening after a model bump
becomes a one-line diff a reviewer can actually read, instead of a rewritten
blob. The old numbers stay available to answer "did this case get less
reliable, or was it always like this" — which is the question that decides
whether a case is worth keeping. And an append conflicts with a concurrent
append far less often than two rewrites of the same object conflict, which is
what makes a checked-in store survive more than a handful of cases.

Only runs on ``main`` append. A pull request's own run is graded against the
store and never writes to it, so a case cannot move the baseline it is about
to be judged against.

EVIDENCE ACCUMULATES; IT IS NOT ONE CAMPAIGN. The admission bar wants twenty
runs and an ordinary run of the presubmit is three repetitions, so a rule that
read only the newest line could never admit anything the routine job produced
— the store would ship empty and stay empty. :meth:`BaselineStore.evidence_for`
therefore pools the NEWEST lines at the current key until it holds ``min_runs``
runs. One deliberate twenty-run screening campaign satisfies that in a single
line; seven ordinary merges to ``main`` satisfy it in seven. Pooling stops at
the bar rather than reading the whole file, which is what gives recency for
free: a case that starts failing has its old passing lines pushed out of the
window by the new failing ones, and de-admits itself without anyone editing
the store.

ADMISSION IS COMPUTED, NEVER DECLARED. A case is admitted because the store
holds screening evidence for it at the CURRENT version key, not because a task
file says so. Three consequences, all of them the point: a pull request author
cannot self-admit their own case in the same diff that makes it pass; bumping
any version de-admits everything until it is re-screened; and a key with no
record is reported STALE rather than silently compared against a baseline
measured on different software.

THE VERSION KEY, AND WHY IT IS MOSTLY NOT OURS. Three of its five components
are produced by devops-bench and read off the run: ``setupId`` from
``manifest.json`` folds together the agent model, the harness and the
augmentation, and ``scoringVersion`` from ``rows.json`` names the roll-up
formula. Those cannot go stale, because devops-bench changes them when the
thing they name changes. Only ``fleet`` and ``verifiers`` are hand-declared
integers in ``VERSIONS.json``.

Why hand-bumped integers and not content hashes: a hash over ``verifiers.py``
changes on a comment typo, which de-baselines the whole suite — and under a
checked-in store, re-baselining costs a pull request rather than an on-demand
backfill. It is the same contract ``bench/pyproject.toml`` already asks of
contributors for the devops-bench SHA. The trade-off, stated plainly: a
behaviour change with no bump silently compares against a stale baseline. A
lint for that is later work, not this module.

THE JUDGE MODEL IS PINNED INDEPENDENTLY of the agent model, which is why it is
a separate component rather than being folded into ``setupId``. A drifting
judge moves every baseline at once, and a judge that tracks whatever the agent
is running cannot be told apart from an agent that got better.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .evidence_store import EvidenceSource, StoreUnreachable, is_gcs, open_backend

__all__ = [
    "StoreUnreachable",
    "is_gcs",
]

__all__ = [
    "AdmissionBar",
    "BaselineEvidence",
    "BaselineRecord",
    "BaselineStore",
    "VersionKey",
    "Versions",
    "append_record",
    "load_versions",
    "utc_now",
]

#: Screening evidence must be at least this fraction of passing runs. 19/20.
DEFAULT_ADMISSION_RATE = 0.95

#: ...over at least this many runs. A case that passed 1 of 1 has proved
#: nothing, and admitting it would let a single lucky run arm the collapse
#: rule against every future pull request.
DEFAULT_ADMISSION_MIN_RUNS = 20


@dataclass(frozen=True)
class AdmissionBar:
    """How much evidence admits a case."""

    rate: float = DEFAULT_ADMISSION_RATE
    min_runs: int = DEFAULT_ADMISSION_MIN_RUNS

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AdmissionBar:
        src = env if env is not None else os.environ
        return cls(
            rate=float(src.get("EVAL_ADMISSION_RATE", DEFAULT_ADMISSION_RATE)),
            min_runs=int(src.get("EVAL_ADMISSION_MIN_RUNS", DEFAULT_ADMISSION_MIN_RUNS)),
        )


@dataclass(frozen=True)
class Versions:
    """The two hand-declared halves of the key."""

    fleet: int
    verifiers: int


def load_versions(path: str | Path) -> Versions:
    """Read ``bench/baselines/VERSIONS.json``.

    A missing or malformed file is an error rather than a default. Defaulting
    would mean scoring against version 1 of something that might be version 3,
    which is the stale-baseline failure this whole module is built to make
    visible.
    """
    p = Path(path)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FileNotFoundError(f"{p}: cannot read the version pins: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"{p}: not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise ValueError(f"{p}: expected a JSON object")
    try:
        return Versions(fleet=int(doc["fleet"]), verifiers=int(doc["verifiers"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            f"{p}: needs integer 'fleet' and 'verifiers' keys: {exc}"
        ) from exc


@dataclass(frozen=True)
class VersionKey:
    """The five components a baseline record is filed under.

    Equality is exact on all five. There is no notion of a compatible-enough
    key: the point of the key is that a baseline measured on other software is
    not evidence about this one.
    """

    setup_id: str
    scoring_version: str
    judge_model: str
    fleet: int
    verifiers: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup_id": self.setup_id,
            "scoring_version": self.scoring_version,
            "judge_model": self.judge_model,
            "fleet": self.fleet,
            "verifiers": self.verifiers,
        }

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> VersionKey:
        return cls(
            setup_id=str(doc.get("setup_id") or ""),
            scoring_version=str(doc.get("scoring_version") or ""),
            judge_model=str(doc.get("judge_model") or ""),
            fleet=int(doc.get("fleet") or 0),
            verifiers=int(doc.get("verifiers") or 0),
        )

    @classmethod
    def from_run(
        cls,
        *,
        setup_id: str | None,
        scoring_version: str | None,
        judge_model: str | None,
        versions: Versions,
    ) -> VersionKey | None:
        """Build the key for a run, or None when the run does not carry one.

        None is returned rather than a key with empty components: a run whose
        ``manifest.json`` is missing cannot be matched against a baseline, and
        a key of empty strings would match another equally broken run's key.
        The caller reports that as stale, which is the honest answer.
        """
        if not setup_id or not scoring_version or not judge_model:
            return None
        return cls(
            setup_id=setup_id,
            scoring_version=scoring_version,
            judge_model=judge_model,
            fleet=versions.fleet,
            verifiers=versions.verifiers,
        )


def utc_now() -> str:
    """The ``recorded_at`` stamp, to the second. UTC, always."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_float(value: Any) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _pool_judged(sources: list[dict[str, Any] | None]) -> dict[str, dict[str, Any]]:
    """Combine per-record judged blocks into one mean per metric.

    Weighted by each block's own ``n``, so twenty runs of evidence outweigh
    three. A block missing a usable mean or a positive n is dropped rather
    than counted as zero, for the same reason ``score_value`` returns None on
    an absent key: an unmeasured metric is not a metric that scored nothing.
    """
    totals: dict[str, list[float]] = {}
    for block in sources:
        if not isinstance(block, dict):
            continue
        for metric, blob in block.items():
            if not isinstance(blob, dict):
                continue
            mean = _as_float(blob.get("mean"))
            count = _as_float(blob.get("n"))
            if mean is None or count is None or count <= 0:
                continue
            acc = totals.setdefault(str(metric), [0.0, 0.0])
            acc[0] += mean * count
            acc[1] += count
    return {
        metric: {"mean": total / count, "n": int(count)}
        for metric, (total, count) in totals.items()
        if count
    }


@dataclass(frozen=True)
class BaselineRecord:
    """One screening result: how a case behaved on main at one version key.

    ``runs`` counts SCORED repetitions only -- the ones that produced a pass or
    a fail. Repetitions that rungs 1-3 blocked, or that died on infrastructure,
    are counted separately in :attr:`blocked` and :attr:`infra` and kept out of
    the rate. They belong in the file because dropping them silently would make
    a case that half-crashes look perfectly reliable, and out of the rate
    because rungs 1-3 block absolutely whether or not a case is admitted, so
    admission has no need to model them.
    """

    key: VersionKey
    runs: int
    passes: int
    recorded_at: str | None = None
    commit: str | None = None
    judged: dict[str, Any] | None = None
    blocked: int = 0
    infra: int = 0

    @property
    def rate(self) -> float | None:
        return (self.passes / self.runs) if self.runs else None

    def admits(self, bar: AdmissionBar) -> bool:
        return (
            self.runs >= bar.min_runs
            and self.rate is not None
            and self.rate >= bar.rate
        )

    def to_dict(self, case_id: str) -> dict[str, Any]:
        """The JSON object this record is written as. One line, in this order.

        Field order is chosen for the reviewer, not for the parser: a diff
        that adds a line should read as "this case, on this day, at this key,
        went this well" from left to right.
        """
        doc: dict[str, Any] = {
            "case": case_id,
            "recorded_at": self.recorded_at or utc_now(),
        }
        if self.commit:
            doc["commit"] = self.commit
        doc["key"] = self.key.to_dict()
        doc["runs"] = self.runs
        doc["passes"] = self.passes
        if self.blocked:
            doc["blocked"] = self.blocked
        if self.infra:
            doc["infra"] = self.infra
        if self.judged:
            doc["judged"] = self.judged
        return doc


@dataclass(frozen=True)
class BaselineEvidence:
    """Several records at one key, pooled into the answer admission needs."""

    key: VersionKey
    runs: int
    passes: int
    #: How many appended lines went into the pool.
    lines: int
    judged: dict[str, dict[str, Any]] = field(default_factory=dict)
    newest_at: str | None = None
    oldest_at: str | None = None

    @property
    def rate(self) -> float | None:
        return (self.passes / self.runs) if self.runs else None

    @property
    def judged_means(self) -> dict[str, float]:
        """Just the means, which is what rung 6 compares against."""
        return {
            metric: float(blob["mean"])
            for metric, blob in self.judged.items()
            if isinstance(blob, dict) and blob.get("mean") is not None
        }

    def admits(self, bar: AdmissionBar) -> bool:
        return (
            self.runs >= bar.min_runs
            and self.rate is not None
            and self.rate >= bar.rate
        )


def append_record(
    location: str | Path, case_id: str, record: BaselineRecord
) -> tuple[str, str]:
    """Append one screening line. The only writer here.

    ``location`` is a directory, or ``gs://bucket/prefix`` for the GCS backend.
    Returns where it was written and the exact line, so a caller can echo it
    into a build log or a Prow artifact without re-reading the store.

    Deliberately a module function and not a :class:`BaselineStore` method. The
    store is a read snapshot taken at load time; giving it a write method would
    invite the idea that the in-memory object is the authority, when the store
    is, and another process may have appended to it since.
    """
    line = json.dumps(record.to_dict(case_id))
    return open_backend(location).append(case_id, line), line


def _parse_source(source: EvidenceSource) -> list[BaselineRecord]:
    """Parse one case's raw lines into records, oldest first.

    Errors name the source and the line index within it. On GCS a source is a
    whole case prefix rather than a single object, so the index is a position
    in the concatenation, not an object name -- close enough to find the bad
    line, and the alternative is one subprocess per object.
    """
    parsed: list[BaselineRecord] = []
    for line_no, line in enumerate(source.text.splitlines(), start=1):
        # Blank lines are tolerated: an append that raced a trailing newline
        # should not take the presubmit down.
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError as exc:
            raise ValueError(f"{source.label}:{line_no}: not valid JSON: {exc}") from exc
        if not isinstance(entry, dict):
            raise ValueError(f"{source.label}:{line_no}: expected a JSON object")
        case_id = str(entry.get("case") or source.case_id)
        if case_id != source.case_id:
            raise ValueError(
                f"{source.label}:{line_no}: declares case {case_id!r} but is filed "
                f"as {source.case_id!r}; the location is the join key"
            )
        parsed.append(
            BaselineRecord(
                key=VersionKey.from_dict(entry.get("key") or {}),
                runs=int(entry.get("runs") or 0),
                passes=int(entry.get("passes") or 0),
                recorded_at=entry.get("recorded_at"),
                commit=entry.get("commit"),
                judged=entry.get("judged"),
                blocked=int(entry.get("blocked") or 0),
                infra=int(entry.get("infra") or 0),
            )
        )
    return parsed


class BaselineStore:
    """``bench/baselines/<case-id>.jsonl``, one file per case.

    One screening campaign per line, in the order they were run. Lines are
    only ever appended, so a file read bottom-up is the case's history from
    newest to oldest.
    """

    def __init__(self, records: dict[str, list[BaselineRecord]]):
        self._records = records
        #: case id -> how many of its oldest objects the read left out. Empty
        #: on the local backend, and empty on GCS until the cap actually binds.
        self.truncated: dict[str, int] = {}

    @classmethod
    def load(cls, location: str | Path) -> BaselineStore:
        """Read every case's evidence from ``location``.

        A directory, or ``gs://bucket/prefix``. A missing directory or an empty
        prefix is an empty store, not an error: that is the state this ships in
        and the state a fresh checkout is in before anything has been screened.

        Raises :class:`ValueError` on bytes that will not parse and
        :class:`StoreUnreachable` when the store could not be read at all. The
        gate treats those very differently -- see :mod:`.evidence_store`.
        """
        backend = open_backend(location)
        records: dict[str, list[BaselineRecord]] = {}
        for source in backend.sources():
            records[source.case_id] = _parse_source(source)
        store = cls(records)
        store.truncated = dict(getattr(backend, "truncated", {}) or {})
        return store

    def record_for(self, case_id: str, key: VersionKey | None) -> BaselineRecord | None:
        """The NEWEST screening record for this case at this exact key.

        Last line wins. Re-screening at a key that already has evidence is an
        append, so the most recent campaign is the one that describes the
        software as it stands; the earlier lines are history, not candidates.
        """
        if key is None:
            return None
        for record in reversed(self._records.get(case_id, [])):
            if record.key == key:
                return record
        return None

    def history_for(self, case_id: str) -> list[BaselineRecord]:
        """Every record for a case, oldest first, across all version keys."""
        return list(self._records.get(case_id, []))

    def evidence_for(
        self,
        case_id: str,
        key: VersionKey | None,
        *,
        min_runs: int = DEFAULT_ADMISSION_MIN_RUNS,
    ) -> BaselineEvidence | None:
        """Pool the newest lines at this key until ``min_runs`` runs are held.

        Returns None when there is nothing at this key -- which is the state
        every case is in before it has been screened, and is reported as
        "collecting" rather than as a failure.

        Whole lines only. Stopping mid-line to hit ``min_runs`` exactly would
        mean inventing a sub-record that was never measured, so a pool of
        three-repetition lines overshoots to 21 runs rather than pretending to
        20. The overshoot is evidence, so counting it is honest; the point of
        the bound is to keep old lines from propping up a case that has since
        got worse, and one extra line does not do that.
        """
        if key is None:
            return None
        matching = [r for r in self._records.get(case_id, []) if r.key == key]
        if not matching:
            return None

        pooled: list[BaselineRecord] = []
        runs = 0
        for record in reversed(matching):
            pooled.append(record)
            runs += record.runs
            if runs >= min_runs:
                break

        return BaselineEvidence(
            key=key,
            runs=runs,
            passes=sum(r.passes for r in pooled),
            lines=len(pooled),
            judged=_pool_judged([r.judged for r in pooled]),
            newest_at=pooled[0].recorded_at,
            oldest_at=pooled[-1].recorded_at,
        )

    def is_admitted(
        self,
        case_id: str,
        key: VersionKey | None,
        *,
        bar: AdmissionBar,
        bootstrap: frozenset[str] = frozenset(),
    ) -> tuple[bool, str]:
        """Whether the case may reach rung 4, and the one-line why.

        ``bootstrap`` is the transition bridge. The store ships empty, so
        without it every case would stop blocking on the day this lands and
        the presubmit would grade nothing for as long as screening takes.
        Named cases keep their old blocking behaviour meanwhile. It is
        deliberately an environment list in the shell rather than a field in
        the store: a bridge that is inconvenient to extend is a bridge people
        take down.
        """
        if case_id in bootstrap:
            return True, "admitted by BOOTSTRAP_ADMITTED (transition bridge)"
        if key is None:
            return False, "the run carries no version key, so no baseline matches it"
        evidence = self.evidence_for(case_id, key, min_runs=bar.min_runs)
        if evidence is None:
            known = len(self._records.get(case_id, []))
            if known:
                return False, (
                    f"stale: {known} baseline record(s) exist for this case but "
                    f"none at the current key ({key.setup_id}, judge "
                    f"{key.judge_model}, fleet {key.fleet}, verifiers "
                    f"{key.verifiers}) -- re-screen before this case can collapse"
                )
            return False, "no screening evidence for this case yet"
        span = f"{evidence.lines} recorded run(s)"
        if evidence.admits(bar):
            return True, (
                f"admitted on {evidence.passes}/{evidence.runs} screening runs "
                f"across {span} (bar {bar.rate:.0%} over {bar.min_runs})"
            )
        if evidence.runs < bar.min_runs:
            # Not a failure -- this is the store filling up. Said in its own
            # words so a build log distinguishes "we have not measured this
            # yet" from "we measured it and it is not reliable enough", which
            # are the same boolean and completely different problems.
            return False, (
                f"collecting: {evidence.passes}/{evidence.runs} runs recorded at "
                f"this key across {span}, {bar.min_runs - evidence.runs} more "
                f"needed before this case can collapse"
            )
        return False, (
            f"screened at {evidence.passes}/{evidence.runs} across {span}, below "
            f"the bar of {bar.rate:.0%} over {bar.min_runs} runs"
        )
