#!/usr/bin/env python3
"""The self-improvement ledger: what has been found, how often, and what was filed.

The loop is hourly and stateless -- a Job that scaffolds an emptyDir, runs one
agent turn, and exits -- so everything that has to survive a run lives here. Two
things do: the occurrence counts the gate reads (docs/designs/self-improvement.md
sec. 7.2), and the record of which findings already became a pull request, so the
next run recognises its own work rather than filing it again.

Storage is one ConfigMap in the install's own namespace, granted by
`resourceNames` on a Role so the grant cannot reach a second object. It is
deliberately NOT a ConfigMap the agent's Deployment references: the operator
SHA256-hashes the ConfigMaps it owns into the agent's pod-template annotations,
so a ledger in that set would roll the Platform Agent on every write -- an
hourly restart caused by the thing that is supposed to be observing it without
touching it (sec. 10).

Everything above `load`/`save` is pure: no Kubernetes import at module scope, so
the fingerprint, the rolling count and the gate are unit-testable without a
cluster. agents/selfimprove/scripts/test_selfimprove_ledger.py is where that
is exercised.
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import json
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

LEDGER_VERSION = 1

#: (connect, read) for the two API calls this module makes. The client defaults
#: to waiting forever, and the ledger write is the last thing a run does -- a
#: hang there discards every finding the hour produced, silently, because the
#: pod is killed by `activeDeadlineSeconds` before it can report.
API_TIMEOUT = (5, 15)

#: Ordered worst-first, which is also the order the gate reports in.
SEVERITIES = ("critical", "high", "medium", "low")

#: How sure the run was, as SOUL.md sec. 5 asks for it. Unlike severity this is
#: not normalised to a default -- an unrecognised or absent value is stored as
#: "" and renders as "unstated", because guessing "medium" on the agent's behalf
#: would put a confidence in front of a reviewer that nothing ever claimed.
CONFIDENCES = ("high", "medium", "low")

#: The seven signal classes of sec. 4. `forge` is issue/PR creation; `other` is
#: the catch-all row that still has to clear the same evidence bar.
SIGNALS = (
    "errors",
    "inefficiency",
    "latency",
    "responses",
    "delivery",
    "forge",
    "other",
)

#: Occurrence counts older than this stop contributing. The gate is expressed
#: per day, so the window is a day.
COUNT_WINDOW_HOURS = 24

#: The fewest separate runs that must have reported a finding before any rule
#: promotes it, whatever the finding says about itself.
#:
#: `severity` is the last gate input the agent still writes. `evaluate_gate`
#: picks the rule by it, so the threshold a finding has to clear is chosen by
#: the text being graded -- and against the shipped rules (`critical` at one
#: occurrence) a finding on its first ever sighting, self-graded `critical`,
#: was promoted. That is the failure `occurrences_in_window` and
#: `record_finding` are each written to prevent, arriving through the one field
#: they left alone: an agent reading production logs is reading text anyone
#: with a code path into them can write, so a single field it controls must not
#: be enough to open a pull request.
#:
#: The floor is the narrowest fix that states the invariant directly: **no
#: finding is promoted on one run's say-so.** Severity still chooses how much
#: more corroboration than this is needed -- `high` at 3 and `medium` at 5 are
#: untouched -- it just cannot ask for less. Two rather than a larger number
#: because the cost lands on the honest case: a real `critical` now waits for
#: the next run, which is an hour on the shipped schedule, and every hour of
#: that delay buys nothing further once a second independent investigation has
#: confirmed the finding.
#:
#: What it does not do is stop an inflated grade jumping the queue: the
#: promotion order is still severity-first, so a corroborated finding that
#: calls itself `critical` is considered ahead of an honest `high`. That
#: residue is bounded in a way the promotion was not -- ordering only reorders
#: findings that have already cleared this floor and their own rule, and a
#: finding the budget defers keeps its counts and is reconsidered next run --
#: so it costs a delay rather than a pull request nobody earned.
MIN_CORROBORATING_RUNS = 2

#: Ten years, as an upper bound on `cooldownHours`. Not a policy limit -- it is
#: the point past which the arithmetic stops working. `prune` computes
#: `now - timedelta(hours=cooldown)`, and a large enough value walks that date
#: off the bottom of `datetime` and raises `OverflowError: date value out of
#: range` mid-run. An operator writing a huge number means "never re-file this",
#: and ten years delivers that intent on any install that will ever exist.
MAX_COOLDOWN_HOURS = 24 * 365 * 10

#: Ceiling on `maxPullRequestsPerDay` and `minOccurrencesPerDay`, for the same
#: reason as the one above and with the same non-policy character. YAML spells
#: infinity `.inf`, which is a reasonable-looking way to write "no ceiling" or
#: "never", and `int(float("inf"))` raises `OverflowError` -- a crash the gate's
#: `except (TypeError, ValueError)` does not catch. A million delivers either
#: intent: an hourly run cannot open a million pull requests, and no finding can
#: be seen a million times in a 24-hour window.
MAX_GATE_COUNT = 1000000

#: Runs kept for the "did this loop stop finding things, or stop running?"
#: question. Small on purpose: the ledger is a ConfigMap, capped at 1MiB by the
#: API server, and an unbounded history is the only part of it that grows
#: without a finding behind it.
RUN_HISTORY = 48

#: Promotions kept per finding. A promoted finding outlives the sighting window
#: -- `prune` holds the row until the last promotion is older than both the
#: retention period and the cooldown -- so these are the longest-lived entries
#: in the ledger and the list most likely to reach the size cap. Ten is well
#: past what any question asks: the cooldown check reads the most recent
#: promotion, and a finding filed ten times is a problem for a human rather than
#: a record to keep growing.
MAX_PROMOTIONS = 10

#: Cap on the agent-supplied prose in one entry, across `evidence`, `summary`,
#: `proposed_fix` and `user_impact` together. Truncating one finding's evidence
#: costs a reviewer some context; letting it push the ledger past
#: LEDGER_MAX_BYTES costs every future run, because `save` then raises and
#: nothing is recorded at all.
#:
#: Across the four rather than each: this was a per-field cap, and a finding
#: filling every field cost about 49KiB, so sixteen findings -- one unremarkable
#: run -- exceeded the 768KiB ledger cap. `_shed_to_fit` is the backstop for a
#: ledger that is over anyway; this is what keeps one run from getting it there,
#: which is what `EntrySizeTests` has always claimed.
MAX_ENTRY_TEXT_BYTES = 16 * 1024

#: The share of that budget any one of the four fields may take. Equal shares
#: rather than a running budget spent in field order, because the order would
#: decide which field survives a verbose run and there is no honest reason to
#: rank them: whichever of the four is oversized is the one that pays.
MAX_FIELD_TEXT_BYTES = MAX_ENTRY_TEXT_BYTES // 4

#: `title` and `location` are agent-supplied too, and were the two that escaped
#: the cap above. They get their own, far smaller, because they are not prose:
#: SOUL.md sec. 4 asks for a title that describes a class of problem and a
#: location that is a `path:line`, and anything approaching these lengths is
#: already the kind of title that will not fingerprint stably twice. Both are
#: also rendered into the next run's brief and into the filing prompt, so an
#: unbounded one is unbounded prompt content as well as an unbounded ledger.
MAX_TITLE_CHARS = 300
MAX_LOCATION_CHARS = 500

#: A refusal's `reason`, which is the one free-text field `prune` cannot age
#: out: the row survives for as long as the refusal does, and the refusal does
#: not expire. The filing skill's refusals are one sentence, so this is well
#: clear of anything a turn writes on purpose and short enough that a ledger
#: full of nothing but refusals still fits under `LEDGER_MAX_BYTES`.
MAX_REFUSAL_REASON_CHARS = 1000


def utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def to_iso(when: _dt.datetime) -> str:
    return when.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text: str) -> Optional[_dt.datetime]:
    """Parse a ledger timestamp, returning None rather than raising.

    A ledger is data the previous run wrote and a human may have edited with
    `kubectl edit`. One unparseable timestamp must not take the run down, so
    every caller treats None as "outside the window" -- the conservative answer,
    since it withholds a promotion rather than granting one.
    """
    if not text:
        return None
    try:
        cleaned = text.strip().replace("Z", "+00:00")
        parsed = _dt.datetime.fromisoformat(cleaned)
    except (ValueError, AttributeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


# --------------------------------------------------------------------------
# Fingerprinting
# --------------------------------------------------------------------------

#: Substitutions that turn one occurrence of a finding into the class of
#: findings it belongs to.
#:
#: Ordered, and the order is the whole correctness argument: each pattern must
#: run before any looser one that would eat its input. `<TS>` before `<HEX>`
#: before the digit sweep, or a UUID is chewed into `<N>-<N>-<N>` and two
#: different shapes collide on the dashes that survive.
#:
#: Every pattern is case-insensitive even though `normalise` lowercases first.
#: They were not, and it was silent: `\d{4}-\d{2}-\d{2}[T ]…` cannot match
#: `2026-08-22t09:14:03z`, so every timestamp fell through to the digit sweep
#: and came out as `<N>-<N>-22t09:<N>:03z` -- stable within one second and
#: different from the next sighting, which is the exact failure fingerprinting
#: exists to prevent. Belt and braces: the flag costs nothing and the
#: lowercasing is now not load-bearing.
_NORMALISERS: Tuple[Tuple[re.Pattern, str], ...] = (
    # ISO-8601 timestamps, with or without fractional seconds and offset.
    (
        re.compile(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
            re.I,
        ),
        "<TS>",
    ),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<UUID>"),
    # A Kubernetes pod name's generated suffix: <name>-<replicaset>-<pod>.
    (re.compile(r"-[0-9a-f]{8,10}-[0-9a-z]{5}\b", re.I), "-<POD>"),
    # Bare hex runs long enough to be an id rather than a number: git shas,
    # session ids, trace ids. A digit is required somewhere in the run because
    # `[0-9a-f]{7,}` alone also matches English -- `defaced`, `effaced` -- and
    # any path component spelled in those letters, which the substitution then
    # takes out of the location along with the directory it named. Some ids are
    # all letters (a 7-character sha prefix is, about once in fourteen hundred)
    # and those now normalise to themselves, which is the under-normalising
    # direction `normalise` errs in on purpose.
    (re.compile(r"\b(?=[0-9a-f]{7,}\b)[0-9a-f]*\d[0-9a-f]*\b", re.I), "<HEX>"),
    (re.compile(r"\s+"), " "),
)

#: Applied to locations only. A line number drifts on every commit that touches
#: the file above it, so `platformagent_manifests.go:412` and
#: `platformagent_manifests.go:418` are the same place and must fingerprint the
#: same.
#:
#: Deliberately NOT applied to titles, and this is the one judgement call in the
#: module. Sweeping digits out of a title collapses "skill 1 fails" and
#: "skill 2 fails" into one finding whose count is the sum of two unrelated
#: bugs -- and an inflated count is what the gate reads, so over-normalising
#: here does not merely lose information, it manufactures promotions. The
#: counts a title might legitimately carry ("retried 40 times") are the ones
#: SOUL.md sec. 4 already tells the agent to put in the evidence instead, so
#: the case this rule would have covered should not arise.
#:
#: All three forms are here because only the first was, and a location that
#: writes its line number in words split into a fresh row per commit:
#: `k8s-operator/foo.go line 412` and `k8s-operator/foo.go line 418` are the
#: same place, and neither matched `:\d+`, so neither reached `<LINE>` and the
#: two hashed differently. A finding whose identity moves like that sits at one
#: occurrence for ever and never promotes, which is the failure `fingerprint`
#: describes and the one nothing reports while it is happening.
_LOCATION_NORMALISERS: Tuple[Tuple[re.Pattern, str], ...] = (
    # `#L412`, `#L412-L418`: what a GitHub permalink pastes as.
    (re.compile(r"#l\d+(?:\s*-\s*l?\d+)?\b", re.I), ":<LINE>"),
    # "line 412", ", around line 412", ": lines 412-418". The leading class
    # eats the punctuation the agent put between the path and the words, so
    # what is left joins the path the way `path:412` already did.
    (
        re.compile(
            r"[\s,;:(]*\b(?:at|around|near|approx(?:\.|imately)?)?\s*lines?\s*#?\s*\d+"
            r"(?:\s*[-–]\s*\d+)?",
            re.I,
        ),
        ":<LINE>",
    ),
    (re.compile(r":\d+(?::\d+)?\b"), ":<LINE>"),
)

#: The first `path.ext:<LINE>` token in a normalised location, which is the part
#: of the field the agent is not free to vary. Everything after it is prose it
#: writes differently each run, and hashing that prose is what split one live
#: finding across three rows -- see `location_key`.
_PRIMARY_LOCATION = re.compile(r"[a-z0-9_.\-/]+\.[a-z0-9]+:<LINE>")

#: The same thing for a location that names a file and no line at all --
#: `selfimprove_run.py (the filing turn)` and `selfimprove_run.py` are one
#: place described twice, and without this the prose after the path went into
#: the hash.
#:
#: Stricter about the extension than the pattern above, which can afford to be
#: loose because `:<LINE>` already says the token is a file reference. With
#: nothing to anchor on, `[a-z0-9]+` would read the `e.g` of "e.g. the gateway"
#: and the `1.21` of a version string as filenames and collapse every location
#: containing one onto a single row. Two to six characters starting with a
#: letter covers `.go`, `.py`, `.yaml`, `.tf` and every other extension in this
#: tree.
_FILE_REFERENCE = re.compile(r"[a-z0-9_.\-/]*[a-z0-9_\-/]\.[a-z][a-z0-9]{1,5}(?![a-z0-9])")


def normalise(text: str) -> str:
    """Strip the parts of a message that differ between two sightings of one bug.

    The point is stability across runs, not readability: the result is hashed,
    never shown. Over-normalising collapses two real findings into one, which
    the ledger cannot tell you about; under-normalising files the same finding
    every hour, which it very much can. When in doubt this errs towards
    under-normalising -- a duplicate is visible in the ledger, a collision is
    not.

    Trailing sentence punctuation goes because whether the agent ends a title
    with a full stop is a writing-style coin flip and nothing else: the same
    sentence written twice, once with the stop and once without, is one finding
    and hashed to two rows sitting at one sighting each.
    """
    out = (text or "").strip().lower()
    for pattern, replacement in _NORMALISERS:
        out = pattern.sub(replacement, out)
    return out.strip(" \t.,;:!?")


def normalise_location(text: str) -> str:
    """`normalise`, plus the line-number collapse. See _LOCATION_NORMALISERS."""
    out = normalise(text)
    for pattern, replacement in _LOCATION_NORMALISERS:
        out = pattern.sub(replacement, out)
    return out.strip()


def location_key(text: str) -> str:
    """The one file name in a location, with everything the agent varies discarded.

    `location` is free text and the agent writes it at whatever length it feels
    like. Two sightings of one finding arrived as

        k8s-operator/.../platformagent_manifests.go:1820 (the operator's
        hardcoded PATH env var: "...") and /opt/hermes/docker/stage2-hook.sh:480
        (`s6-setuidgid hermes ...`)

    and

        k8s-operator/.../platformagent_manifests.go:1820

    -- the same place, described twice. Hashing the whole string made them two
    findings. Four things vary between one spelling of a place and the next, and
    all four are dropped here:

    - The prose after the path, which is the case above.
    - The directory prefix. The same file arrives as a repository-relative path,
      as a bare name, and as the abbreviated `k8s-operator/.../foo.go` -- that
      last one straight out of the live ledger -- so only the name after the
      final slash is kept.
    - Whether a line number was given at all. `<LINE>` already stands in for the
      digits, but its presence still forked `selfimprove_run.py:412` away from
      `selfimprove_run.py`, so the anchor goes too.
    - Which of several files the agent names first. A location naming two files
      names them in whichever order the sentence came out, so the names are
      sorted and the first taken. Sorting rather than taking the first mentioned
      is the only part of this that is a choice: both are arbitrary, and this one
      at least gives the same answer when the same two files are listed the other
      way round. What neither survives is a later sighting that drops the file
      this one picked and keeps another.

    Names that came with a line number are considered alone, and the looser
    pattern only gets a turn when none did. A location often quotes a path that
    is not the site -- the live example above quotes a `PATH` env var, and
    `/opt/hermes/.venv/bin` in it reads as a file reference -- and a line number
    is the agent saying which reference it means.

    Falls back to the full normalised location when there is no file reference
    to find, which is the old behaviour: a location like "the gchat webhook" has
    nothing better to offer, and an empty fingerprint component would collide
    every such finding into one row.

    Reducing to a bare file name is a deliberate widening. `main.go` and
    `SKILL.md` are not unique in this tree, so two findings in two directories
    can now land on one row -- but only if their titles match word for word as
    well, and `fingerprint` argues why that direction is the safe one to err in.
    """
    out = normalise_location(text)
    for pattern in (_PRIMARY_LOCATION, _FILE_REFERENCE):
        names = {
            match.group(0).split(":", 1)[0].rsplit("/", 1)[-1] for match in pattern.finditer(out)
        }
        names.discard("")
        if names:
            return min(names, key=_name_rank)
    return out


def _name_rank(name: str) -> Tuple[bool, str]:
    """Sort key for the candidate file names in a location: dot-names last.

    `/opt/hermes/.venv/bin` reads as a file reference -- `.venv` is a dot and
    three letters -- and `.venv` sorts before every real filename, so a location
    quoting a PATH picked the virtualenv directory as the site of the finding.
    A dotfile can still be one, and is if it is the only candidate.
    """
    return (name.startswith("."), name)


def fingerprint(signal: str, title: str, location: str = "") -> str:
    """Identity for a finding across runs.

    Normalised title plus the one file reference in the location, because those
    are what stay the same when the same bug fires twice.

    Severity is deliberately NOT in it: a finding that gets re-graded is the
    same finding, and putting the grade in the identity would reset its
    occurrence count every time the agent changed its mind.

    `signal` is excluded for that same reason, and the argument survives only
    because a live run proved it. The classification is as much a judgement call
    as the grade -- the loop filed one finding as `errors` at 3:54 pm and the
    identical title as `inefficiency` at 5:07 pm -- so leaving it in the hash
    reset the count on a re-classification exactly the way severity would. It
    stays in the signature because every caller has one to hand and dropping the
    parameter would be a churnier change than ignoring it.

    Between them those two exclusions are what stop the failure `record_finding`
    warns about: a finding whose identity moves every run sits at a count of one
    forever and never promotes, and nothing reports that it is happening. The
    live ledger held three rows for one `/command` PATH finding, one per
    sighting, none of them ever able to reach `minOccurrencesPerDay`.

    Both exclusions narrow the identity, so the risk they add is two genuinely
    different findings colliding on one row. That is the safe direction to err
    in: a collision inflates a count and files one pull request carrying both
    sets of evidence, while the split it replaces silently files nothing at all.
    Titles here run to `MAX_TITLE_CHARS` and are specific enough that a
    collision needs two findings to agree on their whole sentence.

    Changing the material re-fingerprints every finding, so the rows already in
    a ledger orphan. The occurrence counts restart, which the gate does recover
    from within `minOccurrencesPerDay`. The promotions do not recover: a record
    stays on a row the new function can no longer produce, so the cooldown it
    holds can never fire, while the live row that now represents the finding
    carries none. Every run with budget then re-promotes a finding that is
    already filed and spends a whole turn rediscovering its own pull request.
    Narrowing the material here orphaned seven rows on the live install, two of
    them holding pull-request records.

    Re-keying is the repair, and it is mechanical rather than a guess: an
    orphaned row's own title and location hash to the live row's identity under
    the new function, so the rows that should merge identify themselves. Merge
    the sightings and promotions, keep the live row's assessment, and do it in
    the same change that moves the material.
    """
    material = "|".join([normalise(title), location_key(location)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# The ledger document
# --------------------------------------------------------------------------


def empty_ledger() -> Dict[str, Any]:
    return {"version": LEDGER_VERSION, "findings": {}, "runs": []}


def coerce(raw: Any) -> Dict[str, Any]:
    """Accept whatever is in the ConfigMap and return something with the right shape.

    A ledger that has been hand-edited into nonsense is recoverable -- the
    counts restart -- while a run that dies on it is not, because the run that
    would have rewritten the file is the one that crashed.

    Also re-keys, because this is the one funnel every read goes through. See
    `_rekey`.
    """
    if not isinstance(raw, dict):
        return empty_ledger()
    out = empty_ledger()
    findings = raw.get("findings")
    if isinstance(findings, dict):
        for key, value in findings.items():
            if isinstance(value, dict):
                out["findings"][str(key)] = value
    runs = raw.get("runs")
    if isinstance(runs, list):
        out["runs"] = [r for r in runs if isinstance(r, dict)][-RUN_HISTORY:]
    _rekey(out)
    return out


def _rekey(ledger: Dict[str, Any]) -> int:
    """Move every row whose key no longer matches its own title and location.

    `fingerprint` documents what changing its material costs: every row in every
    live ledger orphans, the occurrence counts restart, and -- the part that does
    not heal on its own -- the promotion records stay on keys the new function
    can never produce again, so the cooldowns they hold can never fire and the
    next run with budget re-files a pull request already sitting in a
    maintainer's queue. Seven rows went that way on the live install the first
    time the material moved, two of them holding pull requests.

    That repair is mechanical, so it is done here rather than written down as a
    runbook nobody runs. An orphaned row stores the title and the location it was
    keyed on, so it can say what its key ought to be, and the row it ought to
    merge with is whatever is already sitting at that key. Both sides keep their
    sightings and their promotions -- `merge_entries` is the same arithmetic a
    409 uses, for the same reason.

    Runs on every read, and after the first save it finds nothing: a ledger
    written by the current `fingerprint` is already at its own keys. The cost is
    one hash per row per load.

    A row with neither a title nor a location is left alone. Every one of them
    would hash to the same key and collapse onto its neighbours, which is a
    worse answer than leaving a hand-written row where its author put it.

    Returns how many rows moved, which is for tests; nothing in this module
    logs.
    """
    findings = ledger.get("findings")
    if not isinstance(findings, dict):
        return 0
    moved = 0
    for key in list(findings):
        entry = findings.get(key)
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "")
        location = str(entry.get("location") or "")
        if not title and not location:
            continue
        want = fingerprint(str(entry.get("signal") or ""), title, location)
        if want == key:
            continue
        entry["fingerprint"] = want
        # Recorded so a human reading the ledger can see why a row's key moved,
        # and see it on the row rather than having to infer it. Overwritten
        # rather than appended to: a list would grow by one on every future
        # change to the material, and the key before last answers nothing.
        entry["rekeyed_from"] = key
        existing = findings.get(want)
        findings[want] = merge_entries(existing, entry) if isinstance(existing, dict) else entry
        del findings[key]
        moved += 1
    return moved


def occurrences_in_window(entry: Dict[str, Any], now: _dt.datetime, hours: int = COUNT_WINDOW_HOURS) -> int:
    """How many RUNS saw this finding in the trailing window. The gate's number.

    One run contributes exactly one, whatever the run said it saw. That is the
    whole point, and it is a deliberate reading of the design's
    `minOccurrencesPerDay`: the loop reads log text, log text can contain
    anything anyone with a code path into the agent's logs cares to put there,
    and a count the agent writes is a count that text can dictate. A finding
    that says `occurrences: 9999` would clear every rule in the gate on its
    first sighting. Counted this way it cannot: `minOccurrencesPerDay: 5` means
    five separate hourly investigations independently found the same thing,
    which no single injected string can manufacture.

    What the run actually observed -- 4,000 lines matching in the log window --
    is kept per sighting and reported by `reported_occurrences_in_window`. It
    belongs in the pull request, where the design wants it and where a human
    reads it. It does not belong in the promotion decision.
    """
    cutoff = now - _dt.timedelta(hours=hours)
    total = 0
    for sighting in entry.get("sightings", []):
        if not isinstance(sighting, dict):
            continue
        at = from_iso(sighting.get("at", ""))
        if at is None or at < cutoff:
            continue
        total += 1
    return total


def reported_occurrences_in_window(
    entry: Dict[str, Any], now: _dt.datetime, hours: int = COUNT_WINDOW_HOURS
) -> int:
    """What the runs SAID they saw, summed over the window. Evidence, not a gate.

    Untrusted: this is the agent's own number, derived from log content it did
    not author. Put it in the pull request next to the query that produced it,
    so a maintainer can judge it. Never branch on it.
    """
    cutoff = now - _dt.timedelta(hours=hours)
    total = 0
    for sighting in entry.get("sightings", []):
        if not isinstance(sighting, dict):
            continue
        at = from_iso(sighting.get("at", ""))
        if at is None or at < cutoff:
            continue
        try:
            total += max(0, int(sighting.get("count", 1)))
        except (TypeError, ValueError):
            total += 1
    return total


#: What replaces a description field on a refused row that has gone stale. Its
#: own wording matters: a reader finding one of these has to know the row was
#: kept on purpose and that the missing text is not a bug in the run that wrote
#: it. Distinct from `SHED_MARKER`, which says the opposite thing -- that the
#: ledger was up against its cap.
TOMBSTONE_MARKER = "[dropped: kept for the refusal below, unseen since]"


def prune(
    ledger: Dict[str, Any],
    now: _dt.datetime,
    retain_days: int = 30,
    cooldown_hours: float = COUNT_WINDOW_HOURS,
) -> None:
    """Drop sightings outside the window and findings nothing has seen for a month.

    A promoted finding is kept past the sighting window, because its
    pull-request record is what stops the loop re-filing it after the cooldown.
    It is not kept *forever*, and the earlier version of this function was: the
    delete was guarded on `not entry.get("promotions")`, so every promoted
    finding became a permanent row. Bounding the `promotions` list inside those
    rows does not help when the rows themselves accumulate -- at the default
    three pull requests a day, a few hundred bytes each, an install reaches
    LEDGER_MAX_BYTES inside a year and then `save` raises on every subsequent
    run. That is the worst failure this file has: the ledger write is where a
    run's findings become durable, so a wedged ledger loses every later run's
    output, and nothing recovers it without someone deleting the ConfigMap.

    So a promotion record ages out too. What it has to outlive is the cooldown,
    which is the only thing that reads it -- hence `cooldown_hours`, and hence
    keeping promotions for whichever of that and `retain_days` is the longer
    period, rather than trusting `retain_days` to be it. An install with a
    90-day cooldown keeps 90 days of promotion records.

    A refused finding is the one row that does not age out at all, because the
    refusal it carries does not either. It is thinned instead of deleted --
    `_tombstone` has the argument, and the bound that makes keeping it
    affordable is `MAX_REFUSAL_REASON_CHARS`.
    """
    sighting_cutoff = now - _dt.timedelta(hours=COUNT_WINDOW_HOURS)
    finding_cutoff = now - _dt.timedelta(days=retain_days)
    promotion_cutoff = min(finding_cutoff, now - _dt.timedelta(hours=max(cooldown_hours, 0)))
    for key in list(ledger["findings"].keys()):
        entry = ledger["findings"][key]
        kept = []
        for sighting in entry.get("sightings", []):
            at = from_iso(sighting.get("at", "")) if isinstance(sighting, dict) else None
            if at is not None and at >= sighting_cutoff:
                kept.append(sighting)
        entry["sightings"] = kept
        promotions = entry.get("promotions")
        if isinstance(promotions, list) and len(promotions) > MAX_PROMOTIONS:
            # Newest kept: the cooldown reads the most recent one, so dropping
            # from the front is the only end that costs nothing.
            #
            # Except that it did cost something. `promotions_today` counts these
            # rows, so a promotion trimmed here stopped being charged against
            # `maxPullRequestsPerDay`, and at any cooldown under roughly
            # 24/MAX_PROMOTIONS hours -- `cooldownHours: 0` is a documented
            # setting -- one finding opened 24 pull requests in a day against a
            # ceiling of 20, with the counter saturating at 11. So the trim
            # spares everything the budget still has to see: the invariant is
            # that a promotion inside COUNT_WINDOW_HOURS survives `prune`, and
            # MAX_PROMOTIONS bounds only the older records, which nothing but
            # the cooldown reads.
            kept = promotions[-MAX_PROMOTIONS:]
            chargeable = []
            for promotion in promotions[:-MAX_PROMOTIONS]:
                at = from_iso(promotion.get("at", "")) if isinstance(promotion, dict) else None
                # An unparseable date is dropped here rather than held the way
                # `_promoted_since` holds one: `promotions_today` cannot count
                # it either, so keeping it protects no budget.
                if at is not None and at >= sighting_cutoff:
                    chargeable.append(promotion)
            entry["promotions"] = chargeable + kept
        last_seen = from_iso(entry.get("last_seen", ""))
        stale = last_seen is None or last_seen < finding_cutoff
        if not stale:
            continue
        if isinstance(entry.get("refused"), dict):
            _tombstone(entry)
            continue
        if not _promoted_since(entry, promotion_cutoff):
            del ledger["findings"][key]


def _tombstone(entry: Dict[str, Any]) -> None:
    """Cut a stale refused row down to the refusal, in place.

    A refusal does not expire -- `record_refusal` says so at length -- and
    deleting the row is how it expired anyway. The window that took is not
    small: a finding has to go 30 days unseen, but plenty do. A signal that
    comes in bursts, a defect on a code path an install exercises monthly, and
    above all an injected instruction, which is the refusal an attacker can
    time. Go quiet for a month, plant the same text again, and the row is gone
    with the hold on it; the loop pays the filing turn the marker exists to
    stop paying, and pays it again every month for as long as the attacker
    keeps that rhythm.

    So the row stays and loses its prose instead, in the same order and for the
    same reason `_shed_to_fit` sheds: the agent-written fields are the bytes,
    and nothing but a human reads them. What is left is what a maintainer needs
    to find the row and undo it by hand -- the fingerprint, the title, the
    location, and the refusal itself. A marker rather than a deletion so that a
    reader can tell a thinned row from one that never carried a summary.

    Self-healing if the finding comes back: `record_finding` writes the fresh
    text over these fields on the next sighting and clears `thinned`. That flag
    is the row saying what it is, rather than `_shed_to_fit` inferring it from
    an absent summary -- which would have read a refused finding that simply
    never carried one as a thinned row, and dropped a live hold to save bytes.
    """
    entry["thinned"] = True
    for field in ("evidence",) + DESCRIPTION_FIELDS:
        if entry.get(field) not in (None, "", [], TOMBSTONE_MARKER):
            entry[field] = TOMBSTONE_MARKER


def _promoted_since(entry: Dict[str, Any], cutoff: _dt.datetime) -> bool:
    """True when the entry carries a promotion at or after `cutoff`.

    A promotion whose timestamp will not parse counts as recent. Deleting a row
    because its date was malformed would let the loop re-file a finding it
    already filed, which is the one thing the promotion record exists to
    prevent; carrying a row too long costs bytes.
    """
    for promotion in entry.get("promotions") or []:
        if not isinstance(promotion, dict):
            return True
        at = from_iso(promotion.get("at", ""))
        if at is None or at >= cutoff:
            return True
    return False


def _clipped(value: str, limit: int) -> str:
    """`value` cut to `limit` characters, with an ellipsis marking the cut.

    Characters rather than bytes, unlike `_bounded`: these two fields go into a
    fingerprint, and a byte-wise cut through a multi-byte character would make
    the identity depend on the encoding of the truncation point.
    """
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _one_line(value: Any) -> str:
    """`value` as a single line, with every whitespace run collapsed to a space.

    These two fields are written by the agent and read back into the *next*
    run's brief, where they appear as one bullet each in a list. A title
    carrying a newline is therefore a title that can add lines to that list --
    a forged ledger row, or text that reads as the operator speaking rather
    than as data. `summarise_for_prompt` fences the block, which says whose
    words those are; this says how many lines they get, so the fence's shape is
    not something the content can argue with either.

    Free of fingerprint consequences: `normalise` already collapses `\\s+` to a
    space before hashing, so the identity of a finding is the same whether or
    not the newline survived to be stored.
    """
    return " ".join(str(value if value is not None else "").split())


def _clipped_bytes(value: str, limit: int) -> str:
    """`value` cut to `limit` bytes, with the cut marked. Never splits a character."""
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    kept = encoded[:limit].decode("utf-8", "ignore")
    return kept + "\n[truncated: %d bytes over the %d-byte cap on one field]" % (
        len(encoded) - limit,
        limit,
    )


def _json_size(value: Any) -> Optional[int]:
    """Bytes `value` costs in the serialised ledger, or None if it cannot go there."""
    try:
        return len(json.dumps(value, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return None


def _bounded(value: Any, limit: int = MAX_FIELD_TEXT_BYTES) -> Any:
    """`value` if it fits in `limit` bytes, otherwise cut down to something that does.

    Strings are truncated and marked. A list -- `evidence` is an array of
    strings when the agent follows the skill -- keeps whole elements until the
    budget is spent and then says how many it dropped, because the element is
    the unit a reviewer reads and an array cut to nothing tells them less than
    its first few lines would. Anything else is replaced by a note rather than
    half-serialised: a structure cut mid-way is not JSON and would fail the
    next `load`.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return _clipped_bytes(value, limit)
    if isinstance(value, list):
        kept: List[Any] = []
        spent = 0
        for position, item in enumerate(value):
            size = _json_size(item)
            if size is not None and spent + size <= limit:
                kept.append(item)
                spent += size
                continue
            # A string that does not fit can still contribute its opening
            # bytes, as long as what is left of the budget is worth reading; a
            # scrap of a few dozen bytes is noise next to the marker saying so.
            partial = isinstance(item, str) and (limit - spent) > 512
            if partial:
                kept.append(_clipped_bytes(item, limit - spent))
            dropped = len(value) - position - (1 if partial else 0)
            kept.append(
                "[dropped %d of %d entries: over the %d-byte cap on one field]"
                % (dropped, len(value), limit)
            )
            break
        return kept
    size = _json_size(value)
    if size is None:
        return "[dropped: not serialisable]"
    if size <= limit:
        return value
    return "[dropped: %d bytes, over the %d-byte cap on one field]" % (size, limit)


def _describe(entry: Dict[str, Any], field: str, value: Any) -> None:
    """Store one agent-written description field on `entry`, bounded.

    An empty value does not overwrite what is already there. A run that omits
    `proposed_fix` is not retracting the one the row carries -- it is a run that
    did not repeat it -- and blanking the field would take a reviewer's account
    of the finding away with nothing recording that it happened. A new row still
    gets the empty value, so the shape of an entry does not depend on what the
    first run chose to fill in.
    """
    if isinstance(value, str):
        value = value.strip()
    stored = _bounded(value)
    if field in entry and not stored:
        return
    entry[field] = stored


#: The three fields a re-report of a known finding is describing the same thing
#: with. `evidence` is deliberately not among them: SOUL.md sec. 4 asks each run
#: for its own fresh log lines, so evidence differing between two sightings is
#: the design working rather than a signal of anything.
DESCRIPTION_FIELDS = ("summary", "proposed_fix", "user_impact")

#: How much of the shorter description's vocabulary the two sightings have to
#: share before the ledger goes on believing they are the same finding.
#:
#: Low, and deliberately so. The check exists to catch a wholesale substitution
#: -- the same title and location with every descriptive field replaced -- and
#: the cost of the two errors is not symmetric. A miss leaves things as they
#: were. A false positive restarts an honest finding's count, which delays a
#: promotion by a run, and if it recurred every run it would be the "never
#: accumulates, never promotes, nothing says so" failure this module is most
#: afraid of. Two descriptions of one problem, written from the same logs by
#: the same model, share far more than a seventh of their words; two
#: descriptions of different problems share little more than their English.
DESCRIPTION_OVERLAP = 0.15

#: Words worth comparing: four characters or more, so that the count is not
#: dominated by articles and prepositions any two English paragraphs share.
_DESCRIPTION_WORD = re.compile(r"[a-z0-9][a-z0-9_.\-/]{3,}")

#: Below this many distinct words there is not enough of a description to say
#: anything about, and the check stands aside rather than guessing.
_DESCRIPTION_MIN_WORDS = 8


def _description_words(source: Dict[str, Any]) -> set:
    """The vocabulary of a finding's description, from an entry or a raw finding."""
    parts = []
    for field in DESCRIPTION_FIELDS:
        value = source.get(field)
        if isinstance(value, str):
            parts.append(value)
        elif value:
            parts.append(json.dumps(value, default=str))
    return set(_DESCRIPTION_WORD.findall(" ".join(parts).lower()))


def describes_the_same_thing(stored: Dict[str, Any], incoming: Dict[str, Any]) -> bool:
    """Is this run describing the finding the stored row accumulated its count for?

    A finding's identity is its title and its location, and the next run's
    brief hands it both and tells it to reuse them word for word -- which is
    what makes the count accumulate, and also what makes the count stealable. A
    run that echoes two strings it was given can land on a row four honest runs
    built up and rewrite every descriptive field underneath it: same
    fingerprint, five occurrences, promoted, and the pull request argues for
    whatever the fifth run wrote. That is not hypothetical for an agent whose
    input is production log text.

    Nothing here can tell a hijack from an honest re-report by authority --
    both arrive as one run's JSON -- so it is decided on the text. Overlap of
    the shorter description's vocabulary rather than of the union, because the
    honest case includes a terse re-report of a verbose original, and measuring
    against the longer of the two would read brevity as substitution.

    Stands aside when either side is too short to have a vocabulary. `True`
    then, not `False`: the check withholds its answer rather than inventing one,
    and the caller only acts on a definite no.
    """
    old = _description_words(stored)
    new = _description_words(incoming)
    if len(old) < _DESCRIPTION_MIN_WORDS or len(new) < _DESCRIPTION_MIN_WORDS:
        return True
    shared = len(old & new)
    return shared >= DESCRIPTION_OVERLAP * min(len(old), len(new))


def record_finding(
    ledger: Dict[str, Any],
    finding: Dict[str, Any],
    revision: str,
    now: Optional[_dt.datetime] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Merge one finding from this run into the ledger; return its fingerprint and entry.

    The agent supplies signal, severity, title, location, summary, evidence and
    an occurrence count. Everything else -- identity, first seen, the rolling
    count, the promotion history -- belongs to the ledger and is not the agent's
    to set. That split matters: an agent that could write its own occurrence
    count could talk itself past the gate.

    So the two fields that would let it are taken away here rather than
    documented as forbidden. The fingerprint is always recomputed, never read
    from the finding: a run that echoed back an existing fingerprint would
    inherit that finding's accumulated sightings and promote on its first
    appearance, and one that invented a fresh fingerprint each run would keep
    its count at one forever and never promote at all. And the occurrence count
    is stored as evidence but contributes exactly one to the window -- see
    `occurrences_in_window`.

    Recomputing is only half of it, and the half that is easy to overstate. The
    material is the title and the location, both of which the brief hands the
    next run and asks it to copy, so an agent can reach an existing row without
    guessing anything -- it is told how. What stops that being worth doing is
    below: a run whose description of the finding has nothing in common with
    the one the row accumulated its sightings for starts the count again. The
    invariant is that a row's occurrences belong to the description they were
    counted for, so echoing a known title buys the echoing run its own sighting
    and nothing else.
    """
    now = now or utcnow()
    signal = str(finding.get("signal", "other")).strip().lower()
    if signal not in SIGNALS:
        signal = "other"
    severity = str(finding.get("severity", "low")).strip().lower()
    if severity not in SEVERITIES:
        severity = "low"
    # Truncated before the fingerprint, not after, so the identity is taken over
    # the string that is actually stored. Fingerprinting the full title instead
    # would split two findings that differ only past the cap into two rows which
    # then store identical titles: indistinguishable to a reader, and each
    # counting its sightings alone, so the pair can be seen often enough to
    # promote without either row ever reaching the threshold.
    title = _clipped(_one_line(finding.get("title", "")), MAX_TITLE_CHARS)
    location = _clipped(_one_line(finding.get("location", "")), MAX_LOCATION_CHARS)
    fp = fingerprint(signal, title, location)

    try:
        count = max(1, int(finding.get("occurrences", 1)))
    except (TypeError, ValueError):
        count = 1

    stamp = to_iso(now)
    entry = ledger["findings"].get(fp)
    if entry is None:
        entry = {
            "fingerprint": fp,
            "first_seen": to_iso(now),
            "sightings": [],
            "promotions": [],
        }
        ledger["findings"][fp] = entry

    # What this run inherits by matching the fingerprint: the sightings other
    # runs left. A rewrite inside one run is not this -- the continuation brief
    # tells a second turn to edit its own entry in place and rewrite the
    # description freely -- so only sightings stamped by an earlier run count.
    inherited = [
        sighting
        for sighting in entry.get("sightings", [])
        if isinstance(sighting, dict) and sighting.get("at") != stamp
    ]
    if inherited and not describes_the_same_thing(entry, finding):
        # The row keeps its identity, its promotions and any refusal -- dropping
        # a promotion record here would turn a rewrite into a way to clear a
        # cooldown and re-file, which is worse than the theft it answers -- and
        # loses only the thing a rewrite has no claim on, which is the evidence
        # other runs accumulated for a different description. `first_seen`
        # stays for the same reason the promotions do: it belongs to the
        # identity, and the reset is recorded next to it rather than hidden by
        # moving it.
        entry["sightings"] = [
            sighting
            for sighting in entry.get("sightings", [])
            if isinstance(sighting, dict) and sighting.get("at") == stamp
        ]
        entry["description_reset"] = {
            "at": stamp,
            "revision": revision,
            "dropped": len(inherited),
        }

    entry["signal"] = signal
    entry["severity"] = severity
    entry["title"] = title
    entry["location"] = location
    _describe(entry, "summary", str(finding.get("summary", "")))
    _describe(entry, "evidence", finding.get("evidence"))
    _describe(entry, "proposed_fix", str(finding.get("proposed_fix", "")))
    # SOUL.md asks the agent for both, so both are kept. `confidence` is not a
    # gate input -- an agent that could raise its own confidence past a
    # threshold would be setting the same kind of field the fingerprint and the
    # occurrence count are taken away for. It travels to the pull-request body,
    # where a reviewer weighs it, and to the next run's brief, where a finding
    # last seen at `low` is the one to go back and confirm.
    confidence = str(finding.get("confidence", "")).strip().lower()
    entry["confidence"] = confidence if confidence in CONFIDENCES else ""
    _describe(entry, "user_impact", str(finding.get("user_impact", "")))
    entry["revision"] = revision
    entry["last_seen"] = to_iso(now)
    # The row is live again, so `_tombstone`'s mark comes off: the prose above
    # is this run's, not a marker, and `_shed_to_fit` must stop treating the row
    # as the cheapest thing in the ledger to drop.
    entry.pop("thinned", None)

    # One sighting per run, even when the run reports the same finding several
    # times over. The caller passes a single `now` for the whole run precisely
    # so this can tell "the same run again" from "the next run", and merging
    # rather than appending is what keeps `occurrences_in_window` counting runs:
    # an investigation that emitted its one finding five times would otherwise
    # clear `minOccurrencesPerDay: 5` by itself.
    sightings = entry.setdefault("sightings", [])
    if sightings and isinstance(sightings[-1], dict) and sightings[-1].get("at") == stamp:
        try:
            sightings[-1]["count"] = max(0, int(sightings[-1].get("count", 1))) + count
        except (TypeError, ValueError):
            sightings[-1]["count"] = count
    else:
        sightings.append({"at": stamp, "count": count})
    entry.setdefault("promotions", [])
    return fp, entry


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


def sanitise_cooldown_hours(raw: Any) -> Tuple[float, Optional[str]]:
    """One reading of `cooldownHours`, shared by the gate and by `prune`.

    Returns the hours to use, and a line explaining the substitution when the
    supplied value was not usable -- `None` when it was, so a caller can log
    only the interesting case.

    It is shared because it was not, and the two readings disagreed in the
    direction that matters. `prune` was handed a sanitised value while
    `evaluate_gate` re-parsed the raw one with a bare `float()`, so
    `cooldownHours: -5` was corrected for the function that trims storage and
    left intact for the function that decides whether to open a pull request.
    A negative timedelta is never greater than an elapsed one, so the cooldown
    check simply stopped holding anything and the loop re-filed a finding it
    had filed an hour before -- in as many words, the failure the sanitising
    was written to prevent, reached by sanitising only one of the two callers.
    `.inf` and `nan` were worse-behaved and easier to notice: `float()` accepts
    both, and `timedelta` then raises `OverflowError` and `ValueError` from
    inside the gate.

    So: not finite, or negative, and the default stands in. Finite and far too
    large is clamped rather than replaced, because unlike the others it is a
    coherent instruction -- see MAX_COOLDOWN_HOURS.
    """
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return float(COUNT_WINDOW_HOURS), (
            "cooldownHours=%r is not a number; using %s" % (raw, COUNT_WINDOW_HOURS)
        )
    if not math.isfinite(hours) or hours < 0:
        return float(COUNT_WINDOW_HOURS), (
            "cooldownHours=%r is not a usable number of hours; using %s"
            % (raw, COUNT_WINDOW_HOURS)
        )
    if hours > MAX_COOLDOWN_HOURS:
        return float(MAX_COOLDOWN_HOURS), (
            "cooldownHours=%r is past the %s-hour ceiling arithmetic allows; using that"
            % (raw, MAX_COOLDOWN_HOURS)
        )
    return hours, None


def sanitise_gate_count(raw: Any, key: str, default: int) -> Tuple[int, Optional[str]]:
    """Read one of the gate's two whole-number knobs.

    `maxPullRequestsPerDay` and `minOccurrencesPerDay` were each read with an
    `int()` guarded by `except (TypeError, ValueError)`, which is the wrong pair
    of exceptions: `int(float("inf"))` raises `OverflowError`. YAML spells
    infinity `.inf`, and both keys have an intent an operator might reach for it
    to express -- "no ceiling on pull requests", "never promote this severity".
    Either one killed the run, at the gate, after the investigation had already
    been paid for, and again every hour after that.

    So clamp rather than reject. Unlike a malformed cooldown, a huge count is a
    coherent instruction in both directions, and MAX_GATE_COUNT carries it out.
    Values that are not numbers at all, and NaN, fall back to `default` with a
    note; negatives clamp to zero, which is what the arithmetic downstream
    already made of them -- `max(0, budget - spent)` for one, a threshold no
    occurrence count can fail for the other -- but says so in the log instead of
    printing "the day's pull-request budget (-1) is spent".
    """
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return default, "%s=%r is not a number; using %s" % (key, raw, default)
    if math.isnan(number):
        return default, "%s=%r is not a number; using %s" % (key, raw, default)
    if number < 0:
        return 0, "%s=%r is negative; using 0" % (key, raw)
    if number > MAX_GATE_COUNT:
        return MAX_GATE_COUNT, "%s=%r is past the %s ceiling; using that" % (
            key,
            raw,
            MAX_GATE_COUNT,
        )
    return int(number), None


def gate_notes(gate: Dict[str, Any]) -> List[str]:
    """Everything `evaluate_gate`'s reading of its own numbers would complain about.

    `evaluate_gate` does not log -- it is imported by tests and returns its
    reasoning instead of printing it -- so the runner calls this to get the same
    complaints into the run log. Both sides call the same pure functions, which
    is the property that matters: this cannot report a value the gate then goes
    on to use differently.
    """
    gate = gate or {}
    notes = [
        sanitise_gate_count(gate.get("maxPullRequestsPerDay", 0), "maxPullRequestsPerDay", 0)[1],
        sanitise_cooldown_hours(gate.get("cooldownHours", COUNT_WINDOW_HOURS))[1],
    ]
    raw = gate.get("rules")
    if raw is not None and not isinstance(raw, list):
        notes.append(
            "gate.rules is a %s, not a list: no severity has a promotion rule, "
            "so nothing will be promoted" % type(raw).__name__
        )
    elif isinstance(raw, list):
        malformed = sum(1 for rule in raw if not isinstance(rule, dict))
        if malformed:
            notes.append(
                "gate.rules: ignoring %d of %d entries that are not mappings"
                % (malformed, len(raw))
            )
    for rule in _rules(gate):
        severity = str(rule.get("severity", "?")).strip().lower()
        notes.append(
            sanitise_gate_count(
                rule.get("minOccurrencesPerDay", 1),
                "minOccurrencesPerDay (severity %s)" % severity,
                1,
            )[1]
        )
    return [note for note in notes if note]


def _rules(gate: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The promotion rules in `gate`, with anything that is not one dropped.

    The gate arrives from Helm values by way of a ConfigMap, so `rules` is
    whatever the operator wrote there. `evaluate_gate` used to iterate it
    directly: a string iterated as characters and raised `AttributeError` on the
    first `.get`, which propagated out of the runner and lost the whole run's
    findings before anything could be written to the ledger -- hourly, with no
    row to show for it and `backoffLimit: 0` meaning no retry. `gate_notes`
    already stepped over non-mappings, so the two disagreed about the same
    config, and the one that logged was the forgiving one.

    Dropping rather than raising, and the same reading for both callers. A gate
    with no usable rules promotes nothing, which is the safe direction, and
    `gate_notes` says so in the run log.
    """
    raw = (gate or {}).get("rules")
    if not isinstance(raw, list):
        return []
    return [rule for rule in raw if isinstance(rule, dict)]


def _rule_for(severity: str, rules: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for rule in rules or []:
        if str(rule.get("severity", "")).strip().lower() == severity:
            return rule
    return None


def promotion_at(promotion: Any, now: _dt.datetime) -> Optional[_dt.datetime]:
    """When a promotion record happened, reading an unreadable `at` as *now*.

    `None` only when the record is not a mapping at all -- that is not a
    promotion, it is debris, and `coerce` drops it.

    A record whose `at` will not parse is a different thing: something did
    promote this finding, and the only part that is missing is when. Both
    readings of that were once "never", and both fail open. `promotions_today`
    skipped it, refunding a slot against `maxPullRequestsPerDay`; the cooldown
    scan skipped it too, leaving `last` unset, so the finding was immediately
    re-promotable. Together they let a run open more pull requests than the gate
    allows, and re-open one it had just filed.

    So an unreadable timestamp reads as now: charged against the day, and inside
    the cooldown. That errs towards filing less, which is the direction to err
    -- the cost is a finding held back, against a duplicate reaching a
    maintainer's queue. It is not free: a corrupt record holds its slot for as
    long as it is in the ledger. `MAX_PROMOTIONS` rolls it off by position
    rather than by date, so it does go, and `unreadable_promotions` exists so
    the run log says it is there rather than leaving a quiet loop to be
    explained later.
    """
    if not isinstance(promotion, dict):
        return None
    return from_iso(promotion.get("at", "")) or now


def unreadable_promotions(ledger: Dict[str, Any]) -> int:
    """How many promotion records carry an `at` that will not parse.

    Non-zero means the day's budget is being charged for records whose date is
    unknown, and that findings may be held inside a cooldown that has no end
    the ledger can show. See `promotion_at` for why that is the safe reading,
    and the run log for where it is reported.
    """
    total = 0
    for entry in ledger.get("findings", {}).values():
        for promotion in entry.get("promotions", []) or []:
            if isinstance(promotion, dict) and from_iso(promotion.get("at", "")) is None:
                total += 1
    return total


def promotions_today(ledger: Dict[str, Any], now: _dt.datetime) -> int:
    cutoff = now - _dt.timedelta(hours=COUNT_WINDOW_HOURS)
    total = 0
    for entry in ledger["findings"].values():
        for promotion in entry.get("promotions", []) or []:
            at = promotion_at(promotion, now)
            if at is not None and at >= cutoff:
                total += 1
    return total


def evaluate_gate(
    ledger: Dict[str, Any],
    gate: Dict[str, Any],
    fingerprints: Iterable[str],
    now: Optional[_dt.datetime] = None,
) -> Tuple[List[str], Dict[str, str]]:
    """Decide which of this run's findings become pull requests.

    Returns the promoted fingerprints, worst-severity-first, and a reason per
    fingerprint for everything considered -- including the promoted ones, so a
    run's log says why each decision went the way it did rather than only
    listing the survivors.

    Three conditions, in the order sec. 7.3 states them: the finding matches a
    promotion rule at its own severity with enough occurrences in the window; it
    has not been promoted inside the cooldown; and the day's budget is unspent.
    The budget is counted from the ledger, so promotions this run consumes it
    too -- otherwise a run finding six criticals would open six pull requests
    against a ceiling of two.

    Over all of it sits `MIN_CORROBORATING_RUNS`: severity is the finding's own
    account of itself, and with `critical: 1` shipped in the chart it was the
    one agent-written field that decided a promotion outright. The floor is the
    invariant that no single self-graded field turns a first sighting into a
    pull request -- severity still chooses how much more than the floor a
    finding needs, it just cannot choose less.
    """
    now = now or utcnow()
    rules = _rules(gate)
    budget, _ = sanitise_gate_count(
        gate.get("maxPullRequestsPerDay", 0), "maxPullRequestsPerDay", 0
    )
    cooldown, _ = sanitise_cooldown_hours(gate.get("cooldownHours", COUNT_WINDOW_HOURS))

    spent = promotions_today(ledger, now)
    remaining = max(0, budget - spent)

    candidates = []
    # Deduplicated, because a fingerprint appearing twice in one run would
    # otherwise be promoted twice -- two pull requests for one finding, and two
    # charges against maxPullRequestsPerDay. `record_finding` collapses repeats
    # before this is reached, so in practice this is the second line of defence
    # for a caller that assembles the list some other way.
    seen_fps = set()
    for fp in fingerprints:
        if fp in seen_fps:
            continue
        seen_fps.add(fp)
        entry = ledger["findings"].get(fp)
        if entry is None:
            continue
        candidates.append((SEVERITIES.index(entry.get("severity", "low")) if entry.get("severity") in SEVERITIES else len(SEVERITIES), fp, entry))
    candidates.sort(key=lambda item: (item[0], -occurrences_in_window(item[2], now)))

    promoted: List[str] = []
    reasons: Dict[str, str] = {}
    for _, fp, entry in candidates:
        refusal = entry.get("refused")
        if isinstance(refusal, dict):
            # A permanent refusal, not a deferral. The filing turn is told to
            # decline a fix to the loop's own gate, ledger or grants at any
            # severity, and no later run will decide differently -- the refusal
            # is about what the change would touch, not about the evidence. The
            # ordinary `SKIPPED` deliberately charges nothing and starts no
            # cooldown so a better-evidenced run can retry; applied to a
            # permanent refusal that same generosity promotes the finding again
            # every hour, and each promotion costs a filing turn's model budget
            # to reach the same no. The finding stays in the
            # ledger and keeps counting, which is the point -- a human reads it
            # there -- it is only never promoted again.
            reasons[fp] = "held: the filing turn refused this permanently (%s)" % (
                refusal.get("reason") or "no reason recorded"
            )
            continue
        severity = entry.get("severity", "low")
        rule = _rule_for(severity, rules)
        if rule is None:
            reasons[fp] = "held: no promotion rule for severity %s" % severity
            continue
        threshold, _ = sanitise_gate_count(
            rule.get("minOccurrencesPerDay", 1), "minOccurrencesPerDay", 1
        )
        # The floor is applied here rather than at `sanitise_gate_count`, which
        # reads the operator's number and should keep reporting what they wrote.
        required = max(threshold, MIN_CORROBORATING_RUNS)
        seen = occurrences_in_window(entry, now)
        if seen < required:
            if required > threshold:
                reasons[fp] = (
                    "held: %d occurrence(s) in %dh, rule wants %d but no finding is "
                    "promoted on one run's say-so (floor %d)"
                    % (seen, COUNT_WINDOW_HOURS, threshold, MIN_CORROBORATING_RUNS)
                )
            else:
                reasons[fp] = "held: %d occurrence(s) in %dh, rule wants %d" % (
                    seen,
                    COUNT_WINDOW_HOURS,
                    required,
                )
            continue
        last = None
        for promotion in entry.get("promotions", []) or []:
            at = promotion_at(promotion, now)
            if at is not None and (last is None or at > last):
                last = at
        if last is not None and (now - last) < _dt.timedelta(hours=cooldown):
            reasons[fp] = "held: promoted %s, inside the %gh cooldown" % (to_iso(last), cooldown)
            continue
        if remaining <= 0:
            reasons[fp] = "held: the day's pull-request budget (%d) is spent" % budget
            continue
        remaining -= 1
        promoted.append(fp)
        reasons[fp] = "promoted: %s at %d occurrence(s) in %dh" % (severity, seen, COUNT_WINDOW_HOURS)
    return promoted, reasons


def record_promotion(
    ledger: Dict[str, Any],
    fp: str,
    url: Optional[str],
    revision: str,
    now: Optional[_dt.datetime] = None,
    confirmed: bool = True,
) -> None:
    """Charge one promotion against the gate.

    `confirmed=False` records a filing turn that ended without printing a pull
    request URL. It counts for `promotions_today` and for the cooldown exactly
    like a confirmed one -- that is the point, since the pull request may exist
    -- and the flag is what lets a human reading the ledger tell the two apart.
    The key is omitted when the promotion is confirmed, so a row written before
    this existed reads the same as one written after it.
    """
    now = now or utcnow()
    entry = ledger["findings"].get(fp)
    if entry is None:
        return
    promotion = {"at": to_iso(now), "url": url or "", "revision": revision}
    if not confirmed:
        promotion["unconfirmed"] = True
    entry.setdefault("promotions", []).append(promotion)


def record_refusal(
    ledger: Dict[str, Any],
    fp: str,
    reason: str,
    revision: str,
    now: Optional[_dt.datetime] = None,
) -> None:
    """Mark a finding the filing turn will never file, at any severity.

    Distinct from `record_promotion` in the one way that matters: it charges
    nothing. `maxPullRequestsPerDay` limits how much the loop may ask of a
    maintainer's review queue, and a refusal put nothing in that queue, so
    spending a slot on it would let one out-of-bounds finding suppress the day's
    real ones. What it does instead is stop `promote` reaching this finding
    again -- see the `refused` branch there for why an hourly retry of a
    permanent no is worth this much machinery.

    Only the first refusal is kept. A second is the same answer to the same
    question, and overwriting the first would move its timestamp forward, which
    is the field telling a human how long the finding has been waiting on them.

    Of the three fields written, only `reason` is read by any code -- `promote`
    quotes it back so the hold says why. `at` and `revision` are there for
    someone reading the ConfigMap, which is also the only place this is
    reversible from: nothing clears a refusal, so an entry written in error
    stays until a maintainer deletes the key by hand.

    That the hold does not expire is deliberate, not an omission -- and for a
    while it expired anyway, because `prune` deletes a finding nothing has seen
    for 30 days and took the refusal with the row. It now keeps a refused row
    and thins it; see `_tombstone`. The refusal is about what a fix would have
    to touch -- the loop's own gate, ledger or
    grants -- and no amount of elapsed time changes that answer, so an expiry
    would only put the finding back in front of the filing turn to be refused
    again, which is the hourly retry this exists to stop. What is missing is a
    way to undo one, and it is missing because the loop has no writer for it: an
    agent that could clear its own refusals could clear the one holding a change
    to the gate. Clearing it is a maintainer's edit, so the repair is here:

        kubectl -n <ns> get configmap <ledger> -o yaml   # find the fingerprint
        # then remove that finding's "refused" key and patch the ConfigMap back

    A cruder version -- delete the whole finding row -- also works and costs the
    occurrence count, which the next run starts rebuilding.
    """
    entry = ledger["findings"].get(fp)
    if entry is None:
        return
    if isinstance(entry.get("refused"), dict):
        return
    entry["refused"] = {
        "at": to_iso(now or utcnow()),
        # Collapsed and clipped because this outlives everything else on the
        # row: `prune` thins a stale refused finding down to the title, the
        # location and this, and then keeps it indefinitely. The filing skill's
        # refusals are one sentence, so a turn writing more than
        # MAX_REFUSAL_REASON_CHARS was pasting something.
        "reason": _clipped(_one_line(reason or ""), MAX_REFUSAL_REASON_CHARS),
        "revision": revision,
    }


def record_run(
    ledger: Dict[str, Any],
    revision: str,
    outcome: str,
    found: int,
    promoted: int,
    note: str = "",
    now: Optional[_dt.datetime] = None,
    filed: int = 0,
) -> None:
    """Append one run to the history.

    `promoted` and `filed` are separate because they diverge in the two cases
    anyone reads this history to understand. Under report-only, findings clear
    the gate every run and nothing is ever filed -- collapsing the two would
    make the run record say the loop promoted nothing, when what it did was
    promote and then deliberately not file. And in fork/upstream mode a filing
    turn that fails leaves promoted > filed, which is usually the signal that
    the GitHub path is broken rather than that the gate is closed -- usually,
    because a healthy `record_refusal` leaves the same arithmetic. Read the
    finding's `refused` before concluding anything about GitHub from the gap.
    """
    now = now or utcnow()
    ledger.setdefault("runs", []).append(
        {
            "at": to_iso(now),
            "revision": revision,
            "outcome": outcome,
            "findings": found,
            "promoted": promoted,
            "filed": filed,
            "note": note,
        }
    )
    ledger["runs"] = ledger["runs"][-RUN_HISTORY:]


def summarise_for_prompt(ledger: Dict[str, Any], now: Optional[_dt.datetime] = None, limit: int = 40) -> str:
    """What the previous runs already know, in the form the agent is handed it.

    This is the loop's memory. Without it every run re-derives the same findings
    from scratch and reports each as new, which is both the largest waste of a
    run and the thing that makes the occurrence count meaningless.

    The location is here because identity depends on it: `fingerprint` hashes
    the title and the location's file reference together, and the skill tells a
    run to re-report a known finding with the same title *and* location. A
    summary that shows only the title asks for something it did not supply --
    the run picks a plausible location, the fingerprint differs, the count
    restarts at one, and nothing is ever promoted. The gate looks like it is
    working the whole time.

    Showing the signal class is for the reader rather than for identity, which
    no longer depends on it. That is deliberate slack: a run that re-classifies
    a finding it is otherwise re-reporting faithfully should not lose its count
    over the label, and one live afternoon produced exactly that.
    """
    now = now or utcnow()
    entries = sorted(
        ledger["findings"].values(),
        key=lambda e: (SEVERITIES.index(e["severity"]) if e.get("severity") in SEVERITIES else len(SEVERITIES), e.get("last_seen", "")),
    )
    if not entries:
        return "The ledger is empty: this is the first run, or nothing has been found yet."
    lines = []
    for entry in entries[:limit]:
        promotions = entry.get("promotions") or []
        filed = (" filed=%s" % promotions[-1].get("url", "?")) if promotions else ""
        lines.append(
            "- %s [%s/%s conf=%s] %s @ %s (seen %dx in %dh, last %s, at %s)%s"
            % (
                entry.get("fingerprint", "?"),
                entry.get("severity", "?"),
                entry.get("signal", "?"),
                entry.get("confidence") or "unstated",
                entry.get("title", "(untitled)"),
                entry.get("location") or "(not localised)",
                occurrences_in_window(entry, now),
                COUNT_WINDOW_HOURS,
                entry.get("last_seen", "?"),
                entry.get("revision", "?"),
                filed,
            )
        )
    if len(entries) > limit:
        lines.append("- ... and %d more" % (len(entries) - limit))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------

LEDGER_KEY = "ledger.json"

# 768KiB. The API server rejects a ConfigMap over 1MiB outright, so the ledger
# stops short of it while a `prune`d rewrite still fits.
LEDGER_MAX_BYTES = 768 * 1024

# How many times `save` will re-read, merge and retry against a conflicting
# writer before giving up. Four because each attempt costs one GET and one
# PATCH and the contended case is meant to be rare -- a run losing four races in
# a row is a standing second writer, which the 409 message says to go and find,
# not something a fifth attempt fixes.
LEDGER_WRITE_ATTEMPTS = 4


#: The `metadata.resourceVersion` each (namespace, name) was last seen at, set
#: by `load` and consumed by `save` as an optimistic-concurrency precondition.
#:
#: A module global rather than a return value because `load` and `save` are
#: called from opposite ends of the run and threading a token between them would
#: put a Kubernetes implementation detail through every caller. The runner is a
#: single-shot process handling one ledger, so there is one entry and nothing
#: races it.
_OBSERVED_RESOURCE_VERSION: Dict[Tuple[str, str], str] = {}

#: The (namespace, name) whose `ledger.json` was there but would not parse, set
#: by `load` and consumed by `save` as a refusal to write.
#:
#: Separate from the resourceVersion map because it means the opposite thing. A
#: version says "write against this"; this says "do not write at all", and the
#: two are set from the same read.
_UNPARSEABLE: Dict[Tuple[str, str], bool] = {}


def _api_client():
    """A CoreV1Api bound to whichever kubeconfig this process can find."""
    from kubernetes import client, config as kube_config  # noqa: PLC0415  (cluster-only import)

    try:
        kube_config.load_incluster_config()
    except Exception:  # pragma: no cover - only reachable outside a pod
        kube_config.load_kube_config()
    return client, client.CoreV1Api()


def _read(api, client, namespace: str, name: str) -> Tuple[Dict[str, Any], Optional[str], bool]:
    """The ledger, the resourceVersion it was read at, and whether it parsed.

    The third value distinguishes the two ways an empty ledger comes back. No
    ConfigMap, no key, or a key holding an empty string is a first run and parses
    fine. A key holding text that is not JSON is a ledger that exists and cannot
    be read, and the caller has to know: the empty dictionary returned alongside
    it is a placeholder, not history.
    """
    try:
        cm = api.read_namespaced_config_map(
            name=name, namespace=namespace, _request_timeout=API_TIMEOUT
        )
    except client.exceptions.ApiException as exc:
        if exc.status in (403, 404):
            return empty_ledger(), None, True
        raise
    version = getattr(cm.metadata, "resource_version", None)
    raw = (cm.data or {}).get(LEDGER_KEY)
    if not raw:
        return empty_ledger(), version, True
    try:
        return coerce(json.loads(raw)), version, True
    except (TypeError, ValueError):
        return empty_ledger(), version, False


def load(namespace: str, name: str) -> Dict[str, Any]:
    """Read the ledger ConfigMap, or start a fresh one if it does not exist.

    A missing ConfigMap is the first run on a chart that renders it, and a
    ConfigMap the Role cannot read is a misconfiguration -- both give an empty
    ledger rather than an exception, because a run that cannot read history is
    still a run that can find things.

    Also records the resourceVersion for `save` to write against. The first
    read in a process has nothing to compare a failure to, so an unreadable
    ConfigMap there records nothing and the write that follows is unconditional
    -- refusing to write because the read failed would turn a permissions
    problem into a lost run. A *later* read in the same process is different:
    `refresh_ledger` calls this again, deliberately, to catch a writer that
    moved the ConfigMap while the investigation ran, and a transient 403/404
    on that second call -- a blip, not a real absence -- must not erase the
    version the first call already recorded. Erasing it made `save` write
    unconditionally in exactly the case `refresh_ledger` exists to guard:
    a concurrent writer's entries discarded with no error, because the guard
    that would have caught the conflict was the thing the blip took out.
    So a version already on file survives a versionless read; only a key that
    was never recorded stays that way.

    A third case is not either of those, and used to be treated as both: a
    `ledger.json` that exists and will not parse. It came back as an empty
    ledger *with* the resourceVersion, so the run built its findings on nothing,
    the precondition matched, and `save` wrote the empty ledger over months of
    history -- irreversibly, since a ConfigMap keeps no revisions. It is
    recorded here instead and `save` refuses, which costs this run's findings
    and keeps everything else. The whole of the recovery is that the corrupt
    text is still in the ConfigMap for a human to look at.
    """
    client, api = _api_client()
    ledger, version, parsed = _read(api, client, namespace, name)
    if version:
        _OBSERVED_RESOURCE_VERSION[(namespace, name)] = version
    if parsed:
        _UNPARSEABLE.pop((namespace, name), None)
    else:
        _UNPARSEABLE[(namespace, name)] = True
    return ledger


def _newest_first(entries: Any) -> List[Dict[str, Any]]:
    """Timestamped rows, deduplicated and ordered oldest first."""
    rows = [e for e in (entries or []) if isinstance(e, dict)]
    return sorted(rows, key=lambda e: str(e.get("at") or ""))


def _union(base: Any, incoming: Any, key) -> List[Dict[str, Any]]:
    """Append-only rows from both sides, one row per `key`, oldest first.

    `incoming` wins a tie so that a row this run rewrote -- a promotion whose
    URL arrived late, say -- lands rather than being masked by the copy the
    other writer already had.
    """
    merged: Dict[Any, Dict[str, Any]] = {}
    for row in _newest_first(base):
        merged[key(row)] = row
    for row in _newest_first(incoming):
        merged[key(row)] = row
    return _newest_first(merged.values())


def merge(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """Fold this run's ledger into one another writer moved underneath it.

    Two callers, and both arrive with the same shape: `base` is what is in the
    ConfigMap now and `incoming` is what this run built from an older read of
    it. `save` gets here on a 409. `refresh_ledger` gets here before each
    filing turn, on purpose -- another writer's promotion or refusal has to be
    in the document before the gate is asked again, or two runs file the same
    finding at a maintainer. Almost everything the ledger holds is append-only
    and timestamped, which is what makes a merge well defined rather than a
    guess:

    - `runs`, `sightings` and `promotions` are unions keyed on their timestamps.
      Neither writer's rows are dropped, which is the whole point -- a lost
      promotion record releases a cooldown and files a duplicate pull request at
      a maintainer.
    - `refused` keeps the earlier of the two. Its timestamp is how long a
      finding has been waiting on a human, and `record_refusal` already refuses
      to move it forward.
    - `first_seen` keeps the earlier, for the same reason.
    - Everything else on a finding -- severity, title, summary, evidence -- is
      the agent's current description of it, so the newer writer wins. Which is
      newer is `last_seen`, not which argument it arrived in. `incoming` is this
      run's document, built from a read that predates whatever is in `base` now,
      so taking it as the newer side by position was backwards whenever the
      other writer had seen the finding more recently -- and severity is one of
      the scalars, so the losing side's grading decided the next run's gate.

    Rows only `base` has are carried through untouched. That does resurrect a
    finding this run's `prune` had just dropped, and on the `refresh_ledger`
    path the resurrected row lives until this run's own `save`, because the
    prune it undoes already happened. The alternative is worse: telling the two
    apart needs a record of what was pruned, while a resurrected row is pruned
    again by the next run on the same criterion, an hour later. It is not what
    keeps the ledger writable either -- `_ledger_json` bounds whatever `save`
    is handed, whether or not a merge grew it.
    """
    out = {
        "version": incoming.get("version") or base.get("version") or LEDGER_VERSION,
        "runs": _union(
            base.get("runs"),
            incoming.get("runs"),
            key=lambda r: (str(r.get("at") or ""), str(r.get("revision") or "")),
        ),
        "findings": {},
    }

    base_findings = base.get("findings") or {}
    incoming_findings = incoming.get("findings") or {}
    for fp in set(base_findings) | set(incoming_findings):
        old = base_findings.get(fp)
        new = incoming_findings.get(fp)
        if not isinstance(old, dict) and not isinstance(new, dict):
            # Neither side has a row worth carrying. Writing one anyway put
            # `{"<fingerprint>": null}` into the ledger, which every later
            # reader treats as an entry -- `summarise_for_prompt` calls `.get`
            # on it and takes the run down before the agent is ever prompted.
            continue
        if not isinstance(old, dict):
            out["findings"][fp] = new
            continue
        if not isinstance(new, dict):
            out["findings"][fp] = old
            continue

        out["findings"][fp] = merge_entries(old, new)

    return out


def merge_entries(old: Dict[str, Any], new: Dict[str, Any]) -> Dict[str, Any]:
    """One row from each of two ledgers, folded into one row.

    Extracted from `merge` because `_rekey` needs the same arithmetic for a
    different reason: there, `old` and `new` are two rows of the *same* ledger
    that a change to the fingerprint material has just made into one finding.
    Both callers want the same answer -- neither side's sightings or promotions
    dropped, the newer description kept -- so there is one implementation of it.
    """
    # Whichever side saw the finding last describes it. A row with no
    # `last_seen` sorts below one that has it, and a tie goes to `new` -- the
    # behaviour when the two are the same run, where either answer is the same
    # answer.
    if str(old.get("last_seen") or "") > str(new.get("last_seen") or ""):
        entry = dict(new)
        entry.update(old)
    else:
        entry = dict(old)
        entry.update(new)
    # `thinned` survives only if both writers agree. `record_finding` clears it
    # by popping the key, and a pop does not travel through `update`, so a row
    # one writer has just seen again would otherwise keep the other's mark --
    # and `_shed_to_fit` treats a marked row as the cheapest thing in the ledger
    # to delete.
    if not (old.get("thinned") is True and new.get("thinned") is True):
        entry.pop("thinned", None)
    entry["sightings"] = _union(
        old.get("sightings"), new.get("sightings"), key=lambda s: str(s.get("at") or "")
    )
    entry["promotions"] = _union(
        old.get("promotions"),
        new.get("promotions"),
        key=lambda p: (str(p.get("at") or ""), str(p.get("url") or "")),
    )
    seens = [s for s in (old.get("first_seen"), new.get("first_seen")) if s]
    if seens:
        entry["first_seen"] = min(seens)
    refusals = [r for r in (old.get("refused"), new.get("refused")) if isinstance(r, dict)]
    # Earliest wins, but only among refusals that carry a date. Sorting on
    # `str(at or "")` put an undated refusal first every time, because the empty
    # string precedes every timestamp -- so the one record that cannot say how
    # long a finding has been waiting on a human was the one that always
    # survived the merge.
    dated = [r for r in refusals if from_iso(str(r.get("at") or "")) is not None]
    if dated:
        entry["refused"] = min(dated, key=lambda r: str(r.get("at")))
    elif refusals:
        entry["refused"] = refusals[0]
    return entry


class LedgerWriteError(RuntimeError):
    """The ledger could not be written, with the reason a human needs.

    Its own type because the caller has to distinguish it from every other
    failure in the run: this one loses the occurrence counts the gate reads, so
    the next run under-counts and promotes nothing. A run that finds things and
    cannot record them is a failed run, whatever else went right.
    """


#: What replaces a description field shed to get the ledger under the cap.
SHED_MARKER = "[dropped: the ledger was over the %d-byte cap]" % LEDGER_MAX_BYTES


def _shed_to_fit(ledger: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """A copy of `ledger` under the cap, or None if it cannot be got there.

    Sheds in the order of what a lost field costs. The run history goes first:
    it is a log, nothing reads it but a human, and the newest few rows carry
    what a human wants. Then the agent-written prose, largest field first --
    those are the bytes, and the field is replaced by a marker rather than
    removed so a reader sees that the finding once carried a summary.

    What is never shed while a finding is live is what the gate reads:
    fingerprints, sightings, promotions, refusals. A row that loses its
    sightings loses its occurrence count, so the finding stops promoting; one
    that loses a promotion record stops holding its cooldown and is filed again.
    Shedding those to save bytes would trade a loud failure for a quiet wrong
    answer.

    The last tier is the exception, and it exists because `prune` now keeps
    refused rows indefinitely rather than deleting them at 30 days. A row
    `_tombstone` has already thinned is a finding nothing has seen for a month:
    no sightings inside the window, no promotion the cooldown still consults,
    nothing left but the hold. Dropping the oldest of those costs one finding
    being filed once if it ever comes back -- which is exactly what the ledger
    did before, unconditionally and on every refusal -- and it buys a write.
    Raising instead loses every finding of every later run until a human
    intervenes, so this is the cheaper failure by a wide margin.
    """
    out = copy.deepcopy(ledger)
    runs = out.get("runs")
    if isinstance(runs, list) and len(runs) > 5:
        out["runs"] = runs[-5:]
    if len(json.dumps(out, indent=1, sort_keys=True).encode("utf-8")) <= LEDGER_MAX_BYTES:
        return out

    prose = []
    for fp, entry in (out.get("findings") or {}).items():
        if not isinstance(entry, dict):
            continue
        for field in ("evidence",) + DESCRIPTION_FIELDS:
            size = _json_size(entry.get(field))
            if size is not None and size > len(SHED_MARKER):
                prose.append((size, fp, field))
    for _, fp, field in sorted(prose, reverse=True):
        out["findings"][fp][field] = SHED_MARKER
        if len(json.dumps(out, indent=1, sort_keys=True).encode("utf-8")) <= LEDGER_MAX_BYTES:
            return out

    tombstones = []
    for fp, entry in (out.get("findings") or {}).items():
        if not isinstance(entry, dict) or entry.get("thinned") is not True:
            continue
        refused = entry.get("refused")
        if not isinstance(refused, dict):
            continue
        # Oldest refusal first, and an unreadable date sorts oldest: a row whose
        # `at` will not parse is one nothing has maintained.
        at = from_iso(refused.get("at", ""))
        tombstones.append((at is not None, at or _dt.datetime.min.replace(tzinfo=_dt.timezone.utc), fp))
    for _, _, fp in sorted(tombstones):
        del out["findings"][fp]
        if len(json.dumps(out, indent=1, sort_keys=True).encode("utf-8")) <= LEDGER_MAX_BYTES:
            return out
    return None


def _ledger_json(ledger: Dict[str, Any]) -> str:
    """Serialise, shedding prose if that is what it takes to stay writable.

    A ConfigMap is capped at 1MiB across all keys. The ledger only grows -- new
    fingerprints arrive, `prune` drops findings older than the window -- so an
    install with a wide finding surface reaches the cap eventually, and the
    failure is a 413 on the write rather than anything visible in the run. Trip
    at 768KiB instead.

    Raising there was the whole of it, and the raise was a trap. `save` is the
    last thing a run does, so a ledger over the cap is over the cap for every
    run after it too: each one reads it, prunes it, finds the result still too
    large -- 30 days of retention is not shortenable from the chart -- and
    raises before writing the smaller document that would have fixed it. The
    error text said the opposite, that the next run's `prune` would leave room.
    So shed first and raise only if shedding is not enough, which puts the
    ledger back under the cap on the same run that noticed.
    """
    body = json.dumps(ledger, indent=1, sort_keys=True)
    size = len(body.encode("utf-8"))
    if size <= LEDGER_MAX_BYTES:
        return body
    shed = _shed_to_fit(ledger)
    if shed is not None:
        return json.dumps(shed, indent=1, sort_keys=True)
    raise LedgerWriteError(
        f"ledger is {size} bytes, over the "
        f"{LEDGER_MAX_BYTES}-byte cap ({LEDGER_MAX_BYTES // 1024}KiB of the "
        "API server's 1MiB ConfigMap limit), and dropping every summary and "
        "evidence array it carries still does not bring it under. What is left "
        f"is {len(ledger.get('findings') or {})} finding rows and their "
        "sightings and promotions, which are not droppable because the gate "
        "reads them. Look for unbounded distinct fingerprints -- a finding "
        "title carrying a timestamp, a pod name or a request id -- and expect "
        "to prune the ConfigMap by hand: retention is a fixed 30 days and is "
        "not settable from the chart."
    )


def save(namespace: str, name: str, ledger: Dict[str, Any]) -> None:
    """Write the ledger back into the ConfigMap the chart rendered.

    `patch` rather than `replace`: the chart owns the object's labels and a
    replace would drop them, which takes the ledger out of every
    `-l app.kubernetes.io/part-of=kube-agents` sweep the docs tell an operator
    to run.

    There is no create-if-missing fallback, and its absence is the point. The
    chart renders this ConfigMap whenever it renders the CronJob, so a 404 here
    means something deleted it or the runner is pointed at the wrong name --
    neither of which a silent create fixes, and both of which it hides. The
    Role grants `get`/`update`/`patch` on this one name and no `create` at all,
    so the runner could not do it anyway: an unscoped `create configmaps` is
    the one grant that would let a compromised investigation write a ConfigMap
    of its choosing into the namespace the agent runs in.

    Written against the `resourceVersion` `load` observed, so a writer that
    moved the object in between gets a 409 here instead of being overwritten.
    This used to be unconditional, and the reasoning for that was sound as far
    as it went: `load` runs at the top of the run and this at the bottom, so the
    precondition spans the whole of `activeDeadlineSeconds` -- four hours by
    default -- and a 409 over a window that wide would be common. What made it
    wrong was treating a 409 as fatal. It is not, now that `merge` exists: the
    conflict is caught below, this run's rows are folded into whatever is there
    now, and the write is retried. Nobody's findings are lost either way, which
    is what the unconditional write could not say.

    It could not say it because `concurrencyPolicy: Forbid` serialises the
    CronJob's own runs and nothing else. A Job created by hand is not owned by
    the CronJob, so `Forbid` does not see it -- and a live test ran one
    alongside two scheduled runs. Its closing write took the ledger back to its
    own view of it: the other two runs lost their rows, and with them the
    promotion records for two pull requests already open. A lost sighting only
    delays a promotion; a lost promotion record removes the only thing holding
    the cooldown, so the finding is filed again and the maintainer gets the
    duplicate the gate exists to prevent.

    `patch` rather than `replace` also matters to the precondition working at
    all: a strategic-merge patch carrying `metadata.resourceVersion` is rejected
    with a 409 when it does not match, which is the whole mechanism.

    Once a 409 has been seen, the precondition is never dropped again. A write
    with no `resourceVersion` is the unconditional write above, and the 409 is
    the object saying somebody else's document is there to be overwritten by it.
    So every way the re-read can fail to produce a version -- deleted, a Role
    with patch but not get, the API server not answering -- ends the run's write
    rather than retrying without one. What that costs is this run's findings,
    which recur within the hour; what it keeps is the other writer's promotion
    records, which do not.
    """
    client, api = _api_client()
    key = (namespace, name)
    if _UNPARSEABLE.get(key):
        raise _corrupt_error(namespace, name)

    for attempt in range(LEDGER_WRITE_ATTEMPTS):
        body: Dict[str, Any] = {"data": {LEDGER_KEY: _ledger_json(ledger)}}
        version = _OBSERVED_RESOURCE_VERSION.get(key)
        if version:
            body["metadata"] = {"resourceVersion": version}
        try:
            written = api.patch_namespaced_config_map(
                name=name, namespace=namespace, body=body, _request_timeout=API_TIMEOUT
            )
        except client.exceptions.ApiException as exc:
            if exc.status == 409 and attempt < LEDGER_WRITE_ATTEMPTS - 1:
                # Somebody wrote between `load` and here. Take their document,
                # fold this run's rows into it, and write against the version
                # they left behind.
                try:
                    remote, remote_version, parsed = _read(api, client, namespace, name)
                except Exception as read_exc:  # noqa: BLE001
                    # Raised from inside an `except` clause, so without this it
                    # leaves `save` as whatever the API client threw rather than
                    # the LedgerWriteError every caller checks for.
                    raise LedgerWriteError(
                        f"another writer moved ConfigMap {namespace}/{name} and reading it "
                        f"back to merge with them failed: {read_exc}. Nothing has been "
                        "written, and this run's findings are lost; they recur, and the "
                        "other writer's work is still there."
                    ) from exc
                if not remote_version:
                    # `_read` maps 403 and 404 to an empty ledger, which is the
                    # right answer for `load` -- a run that cannot read history
                    # is still a run that can find things -- and the wrong one
                    # here. A 409 says the object exists and somebody just wrote
                    # it. Merging into that empty placeholder and retrying with
                    # no precondition, which is what the missing version would
                    # do, writes this run's rows over theirs: the same loss the
                    # `parsed` branch below refuses, through a third door. A
                    # deleted ConfigMap ends here too, where the message says so
                    # rather than arriving as a second 404 from the patch.
                    raise LedgerWriteError(
                        f"another writer moved ConfigMap {namespace}/{name} and it could not "
                        "be read back -- it was deleted, or the Role grants patch but not "
                        "get. Refusing to write without a precondition, which would drop "
                        "whatever they wrote. This run's findings are lost and will recur."
                    ) from exc
                if not parsed:
                    # Merging into the placeholder empty ledger would write this
                    # run's rows over the other writer's document -- the same
                    # loss `load` refuses above, reached by the other door.
                    _UNPARSEABLE[key] = True
                    raise _corrupt_error(namespace, name) from exc
                # In place, not `ledger = merge(...)`. Rebinding the local left
                # the caller holding the pre-merge document, which cost nothing
                # while `save` ran once at the end of a run and everything the
                # moment it ran twice: the second call would write the caller's
                # copy -- still missing the rows this merge just folded in --
                # against a resourceVersion that now matches, so it would
                # succeed and drop the other writer's work. The filing loop
                # saves after every promotion, so that is the normal path now.
                merged = merge(remote, ledger)
                ledger.clear()
                ledger.update(merged)
                _OBSERVED_RESOURCE_VERSION[key] = remote_version
                continue
            raise _write_error(namespace, name, exc) from exc
        except Exception as exc:  # noqa: BLE001 -- a timeout is not an ApiException
            # Same reason `API_TIMEOUT` exists: a dropped egress path to the API
            # server produces no HTTP status to key the table on, and without
            # this clause it escapes as a bare urllib3 error rather than the
            # typed LedgerWriteError the caller checks for.
            raise LedgerWriteError(
                f"could not write the ledger: the API server did not answer within "
                f"{API_TIMEOUT[1]}s ({exc}). Check egress to the API server from the "
                "runner pod -- on GKE Dataplane V2 the NetworkPolicy must allow the "
                "address in the default/kubernetes Endpoints, not just the ClusterIP."
            ) from exc

        observed = getattr(getattr(written, "metadata", None), "resource_version", None)
        if observed:
            _OBSERVED_RESOURCE_VERSION[key] = observed
        return


def _write_error(namespace: str, name: str, exc) -> LedgerWriteError:
    """The message a human needs for each way the write can be refused."""
    detail = {
        404: (
            f"ConfigMap {namespace}/{name} does not exist. The chart renders "
            "it alongside the CronJob, so either it was deleted or "
            "selfImprovement.ledgerConfigMap does not match what was rendered."
        ),
        403: (
            f"not permitted to patch ConfigMap {namespace}/{name}. The Role "
            "grants get/update/patch on exactly one name -- check that "
            "SELFIMPROVE_LEDGER_CONFIGMAP matches the resourceNames in it."
        ),
        409: (
            f"ConfigMap {namespace}/{name} was modified by another writer "
            f"{LEDGER_WRITE_ATTEMPTS} times while this run tried to merge into "
            "it. Something is writing the ledger continuously -- look for a Job "
            "created by hand alongside the CronJob, which `concurrencyPolicy: "
            "Forbid` does not serialise."
        ),
        413: (
            f"ConfigMap {namespace}/{name} is too large for the API server. "
            "Findings are accumulating faster than the retention window "
            "prunes them."
        ),
    }.get(exc.status, f"HTTP {exc.status}: {exc.reason}")
    return LedgerWriteError(f"could not write the ledger: {detail}")


def _corrupt_error(namespace: str, name: str) -> LedgerWriteError:
    """The refusal to overwrite a ledger that would not parse, and its repair."""
    return LedgerWriteError(
        f"could not write the ledger: the {LEDGER_KEY} key in ConfigMap "
        f"{namespace}/{name} is not valid JSON, so this run read no history and "
        "writing would replace whatever is there with an empty ledger. Nothing "
        "has been written. Recover the accumulated findings by hand -- "
        f"`kubectl -n {namespace} get configmap {name} -o yaml` to read the "
        f"text, then repair it, or `kubectl -n {namespace} patch configmap "
        f"{name} --type merge -p '{{\"data\":{{\"{LEDGER_KEY}\":\"\"}}}}'` to "
        "start over -- and every run until then will stop here."
    )


def clone(ledger: Dict[str, Any]) -> Dict[str, Any]:
    return copy.deepcopy(ledger)
