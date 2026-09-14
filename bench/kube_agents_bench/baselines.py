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
line; seven ordinary nightlies on ``main`` satisfy it in seven. Pooling stops at
the bar rather than reading the whole file, which is what gives recency for
free: a case that starts failing has its old passing lines pushed out of the
window by the new failing ones, and de-admits itself without anyone editing
the store.

THE RECORD'S VERDICT IS COMPUTED, NEVER DECLARED. What the store says about a
case comes from its screening evidence at the CURRENT version key, not from a
task file. Three consequences, all of them the point: a task file cannot
speak for its own record; bumping any version resets every verdict to stale
until it is re-screened; and a key with no record is reported STALE rather
than silently compared against a baseline measured on different software.
Whether that verdict DECIDES admission or only recommends it is the mode
below; in the default the reviewed roster edit is what stops a pull request
admitting its own case in the diff that makes it pass.

WHO DECIDES IS A MODE, AND THE DEFAULT IS THE ROSTER. ``EVAL_ADMISSION_MODE``
selects it. In ``roster`` mode, the default, ``BOOTSTRAP_ADMITTED`` decides
rung 4 outright -- a listed case blocks on collapse, an unlisted case never
does -- and the record's verdict rides on every decision as a recommendation
(``record: would admit``, ``would demote``, ``collecting``, ``stale``,
``none``) that a roster pull request cites. In ``record`` mode a full window
at the current key decides either way and the list is the fallback while the
record cannot judge. The default is a decision (#1493, 2026-09-14), not a
stopgap: nobody should be able to move a case into or out of the blocking
set without the eval crew knowing, and a roster edit reviewed in a pull
request is that knowledge. See :meth:`BaselineStore.admission`.

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

from .evidence_store import (
    EvidenceSource,
    StoreUnreachable,
    _key_segments,
    is_gcs,
    open_backend,
)

__all__ = [
    "ADMISSION_MODES",
    "ADMISSION_MODE_ENV",
    "ADMISSION_MODE_RECORD",
    "ADMISSION_MODE_ROSTER",
    "ADMITTED_BY_BOOTSTRAP",
    "ADMITTED_BY_NEITHER",
    "ADMITTED_BY_RECORD",
    "DEFAULT_ADMISSION_MODE",
    "RECORD_VERDICT_NONE",
    "Admission",
    "AdmissionBar",
    "BaselineEvidence",
    "BaselineRecord",
    "BaselineStore",
    "StoreUnreachable",
    "VersionKey",
    "Versions",
    "append_record",
    "is_gcs",
    "load_versions",
    "utc_now",
]

#: Screening evidence must be at least this fraction of passing runs. 19/20.
DEFAULT_ADMISSION_RATE = 0.95

#: ...over at least this many runs. A case that passed 1 of 1 has proved
#: nothing, and admitting it would let a single lucky run arm the collapse
#: rule against every future pull request.
DEFAULT_ADMISSION_MIN_RUNS = 20

#: Who decided a case's admission. ``record``: record mode only -- the store
#: held a full window at the current key and its rate decided, either way.
#: ``bootstrap``: ``BOOTSTRAP_ADMITTED`` admitted it -- in roster mode
#: whatever the store holds, in record mode because there was no full window.
#: ``neither``: not on the list and not admitted by the record.
ADMITTED_BY_RECORD = "record"
ADMITTED_BY_BOOTSTRAP = "bootstrap"
ADMITTED_BY_NEITHER = "neither"

#: The reason a listed case has always been given. Byte-identical to what the
#: list produced before the record could overrule it, and pinned by the
#: store-unset golden; the store's own state is appended after it only when
#: the store holds something for the case.
BOOTSTRAP_REASON = "admitted by BOOTSTRAP_ADMITTED (transition bridge)"
BOOTSTRAP_STATE_PREFIX = "; the record cannot judge it yet: "
#: Appended when the record turns away a case the list still names.
RECORD_OVERRIDES_SUFFIX = (
    " -- the record overrides BOOTSTRAP_ADMITTED, which still names this case"
)

#: Who decides admission, from ``EVAL_ADMISSION_MODE``. ``roster``: the
#: ``BOOTSTRAP_ADMITTED`` list decides and the record recommends. ``record``:
#: a full window at the current key decides either way and the list is the
#: fallback. The default is the roster, by decision (#1493, 2026-09-14): a
#: case enters or leaves the blocking set through a reviewed roster edit that
#: cites the record, never through the record alone. The module docstring
#: has the reasoning in full.
ADMISSION_MODE_ENV = "EVAL_ADMISSION_MODE"
ADMISSION_MODE_ROSTER = "roster"
ADMISSION_MODE_RECORD = "record"
ADMISSION_MODES = frozenset({ADMISSION_MODE_ROSTER, ADMISSION_MODE_RECORD})
DEFAULT_ADMISSION_MODE = ADMISSION_MODE_ROSTER

#: The record's recommendation, carried on every verdict as ``record_verdict``
#: in both modes and rendered beside the decider in roster mode. One short
#: phrase per state: ``would admit`` and ``would demote`` are a full window's
#: verdict; ``collecting``, ``stale`` and ``none`` are the states short of one.
RECORD_VERDICT_PREFIX = "record: "
RECORD_VERDICT_NONE = RECORD_VERDICT_PREFIX + "none"
#: Roster mode, full window: the reason carries the record's verdict and says
#: who answered it, so a log line and the verdict column read the same.
ROSTER_DECIDES_SUFFIX = (
    f" -- advisory; the roster decides ({ADMISSION_MODE_ENV}={ADMISSION_MODE_ROSTER})"
)
NOT_ON_ROSTER_REASON = "not named in BOOTSTRAP_ADMITTED"
#: Joins the list's sentence to the record's verdict in an admission reason.
REASON_JOINER = "; "


def _validate_mode(raw: str) -> str:
    """The one place an admission mode is checked, so every caller says the
    same thing about a bad one: the variable, the value, the two it accepts."""
    if raw not in ADMISSION_MODES:
        raise ValueError(
            f"{ADMISSION_MODE_ENV}={raw!r} is not one of "
            f"{', '.join(sorted(ADMISSION_MODES))}"
        )
    return raw


def admission_mode_from_env(env: dict[str, str] | None = None) -> str:
    """``EVAL_ADMISSION_MODE``, validated. Unset or empty is the default.

    An unknown value raises rather than falling back: ``records`` silently
    read as ``roster`` would be the same silent-disarm class a misspelled
    ``BOOTSTRAP_ADMITTED`` entry is, and the gate reports that one loudly.
    """
    src = env if env is not None else os.environ
    return _validate_mode(
        (src.get(ADMISSION_MODE_ENV) or DEFAULT_ADMISSION_MODE).strip().lower()
    )


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
class Admission:
    """Whether a case may reach rungs 4 and 6, the one-line why, and who said so.

    ``source`` is one of :data:`ADMITTED_BY_RECORD`,
    :data:`ADMITTED_BY_BOOTSTRAP` or :data:`ADMITTED_BY_NEITHER`. It is
    reported per case so a verdict says which authority admitted a case, and
    a case the record turned away despite its name being on the bootstrap
    list is visibly the record's decision rather than a missing name. In
    roster mode it is never ``record``.

    ``record_verdict`` is the record's recommendation in both modes --
    ``record: would admit (...)``, ``would demote (...)``, ``collecting
    n/m``, ``stale (key ...)`` or ``none`` -- so a roster pull request can
    cite what the record said whichever mode produced the verdict.
    """

    admitted: bool
    reason: str
    source: str
    record_verdict: str = RECORD_VERDICT_NONE


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

    @property
    def label(self) -> str:
        """The key as one readable token: the GCS layout's own prefix.

        ``<setup-id>/<judge-model>/<sv>-f<n>-v<n>``, built by the same
        function :mod:`.evidence_store` files objects with, so a verdict
        naming a key and an ``ls`` of the bucket cannot disagree.
        """
        return "/".join(_key_segments(self.to_dict()))

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
        mode: str,
        bootstrap: frozenset[str] = frozenset(),
    ) -> tuple[bool, str]:
        """Whether the case may reach rung 4, and the one-line why.

        :meth:`admission` with the source dropped, for callers that only need
        the boolean and the sentence. ``mode`` has no default here on
        purpose: with an empty ``bootstrap`` the roster answer is a constant
        False, so a caller must say which answer it is asking for.
        """
        decision = self.admission(case_id, key, bar=bar, bootstrap=bootstrap, mode=mode)
        return decision.admitted, decision.reason

    def _record_verdict(
        self,
        case_id: str,
        key: VersionKey | None,
        evidence: BaselineEvidence | None,
        bar: AdmissionBar,
    ) -> str:
        """What the record says about the case, as one short phrase.

        Computed the same way in both modes; only what is DONE with it
        differs. ``would demote`` is the full window's refusal whether or not
        the list names the case, because the phrase is the record's verdict
        and not a statement about the roster.
        """
        if key is None or evidence is None:
            if key is not None and self._records.get(case_id):
                return f"{RECORD_VERDICT_PREFIX}stale (key {key.label})"
            return RECORD_VERDICT_NONE
        if evidence.runs < bar.min_runs:
            return f"{RECORD_VERDICT_PREFIX}collecting {evidence.runs}/{bar.min_runs}"
        if evidence.admits(bar):
            return (
                f"{RECORD_VERDICT_PREFIX}would admit "
                f"({evidence.passes}/{evidence.runs} at key {key.label})"
            )
        return (
            f"{RECORD_VERDICT_PREFIX}would demote ({evidence.passes}/{evidence.runs}, "
            f"below {bar.rate:.0%} over {bar.min_runs})"
        )

    def _pre_admission_state(
        self, case_id: str, key: VersionKey | None, evidence: BaselineEvidence | None, bar: AdmissionBar
    ) -> str:
        """The four states short of admission, in the store's own words.

        Distinct on purpose: only the last is a problem with the case, and a
        build log has to tell "we have not measured this yet" from "we
        measured it and it is not reliable enough", which are the same boolean
        and completely different problems.
        """
        if key is None:
            return "the run carries no version key, so no baseline matches it"
        if evidence is None:
            known = len(self._records.get(case_id, []))
            if known:
                return (
                    f"stale: {known} baseline record(s) exist for this case but "
                    f"none at the current key ({key.setup_id}, judge "
                    f"{key.judge_model}, fleet {key.fleet}, verifiers "
                    f"{key.verifiers}) -- re-screen before this case can collapse"
                )
            return "no screening evidence for this case yet"
        span = f"{evidence.lines} recorded run(s)"
        if evidence.runs < bar.min_runs:
            return (
                f"collecting: {evidence.passes}/{evidence.runs} runs recorded at "
                f"this key across {span}, {bar.min_runs - evidence.runs} more "
                f"needed before this case can collapse"
            )
        return (
            f"screened at {evidence.passes}/{evidence.runs} across {span}, below "
            f"the bar of {bar.rate:.0%} over {bar.min_runs} runs"
        )

    def admission(
        self,
        case_id: str,
        key: VersionKey | None,
        *,
        bar: AdmissionBar,
        mode: str,
        bootstrap: frozenset[str] = frozenset(),
    ) -> Admission:
        """Who admits the case -- the bootstrap list, the record, or nobody.

        ``mode`` is :data:`ADMISSION_MODE_ROSTER` or
        :data:`ADMISSION_MODE_RECORD`, required; the CLI reads it from
        ``EVAL_ADMISSION_MODE`` (:func:`admission_mode_from_env`, whose
        default is the roster). The record's verdict is computed the same way
        in both and carried on the result as ``record_verdict``.

        ROSTER MODE, THE DEFAULT: ``bootstrap`` decides. A listed case is
        admitted and an unlisted one is not, whatever the store holds, and
        the record's verdict is a recommendation the reason carries. When the
        store holds a full window the reason quotes that verdict and says the
        roster answered it, so a listed case the record would demote and an
        unlisted case the record would admit are both visible in the log
        without either changing what blocks. Short of a full window the
        listed case's reason names the store's state, as it always has, and
        a case the store holds nothing for gets the list's own sentence.

        RECORD MODE: the record governs once it holds a full window. With at
        least ``bar.min_runs`` runs at the current key the pooled rate
        decides, either way -- a case at 12/20 is turned away even if
        ``bootstrap`` names it -- and the list is the fallback for a case the
        record cannot yet judge: no evidence, evidence only at a superseded
        key, or fewer than ``bar.min_runs`` runs at this one.

        The list is deliberately an environment variable in the shell rather
        than a field in the store: in either mode it is the thing a reviewed
        pull request edits, and a case must not be able to admit itself in
        the diff that makes it pass.
        """
        _validate_mode(mode)
        evidence = (
            self.evidence_for(case_id, key, min_runs=bar.min_runs)
            if key is not None
            else None
        )
        verdict = self._record_verdict(case_id, key, evidence, bar)
        full_window = evidence is not None and evidence.runs >= bar.min_runs

        if full_window and mode == ADMISSION_MODE_RECORD:
            span = f"{evidence.lines} recorded run(s)"
            if evidence.admits(bar):
                return Admission(
                    True,
                    f"admitted on {evidence.passes}/{evidence.runs} screening runs "
                    f"across {span} (bar {bar.rate:.0%} over {bar.min_runs})",
                    ADMITTED_BY_RECORD,
                    verdict,
                )
            reason = self._pre_admission_state(case_id, key, evidence, bar)
            if case_id in bootstrap:
                reason += RECORD_OVERRIDES_SUFFIX
            return Admission(False, reason, ADMITTED_BY_RECORD, verdict)

        if case_id in bootstrap:
            reason = BOOTSTRAP_REASON
            if full_window:
                # Roster mode only: record mode returned above. The record
                # spoke; say what it said and that the list answered.
                reason += REASON_JOINER + verdict + ROSTER_DECIDES_SUFFIX
            elif self._records.get(case_id):
                reason += BOOTSTRAP_STATE_PREFIX + self._pre_admission_state(
                    case_id, key, evidence, bar
                )
            return Admission(True, reason, ADMITTED_BY_BOOTSTRAP, verdict)

        if full_window:
            reason = NOT_ON_ROSTER_REASON + REASON_JOINER + verdict + ROSTER_DECIDES_SUFFIX
        else:
            reason = self._pre_admission_state(case_id, key, evidence, bar)
        return Admission(False, reason, ADMITTED_BY_NEITHER, verdict)
