"""The Trend page's document: scores over time on ``main``, from the evidence store.

``store.py`` reads the store into ``store.json`` (one record per case per
night, as the nightly appended it); this module turns those records into
what ``trend.html`` draws and ``render.py`` puts into ``brief.json`` as
``trend`` (SCHEMA.md, "brief.json"): per case and per domain, the
deterministic pass rate by night and the judged quality by night, with the
nights where the version key changed marked, so a step change has its
explanation next to it (#1493).

WHAT "SCORE" MEANS is written once, in ``docs/designs/eval-scorer.md``
("What a score is"), and this module follows it:

* **Pass rate is the gate's number.** ``passes / runs`` over the scored
  repetitions of a record; ``blocked`` and ``infra`` repetitions stay out of
  the rate. Beside each night's own rate the page draws the **trailing
  window**: the newest whole records at the same key pooled until they hold
  ``ADMISSION_MIN_RUNS`` runs, which is what computed admission reads
  (``BaselineStore.evidence_for``). Admission pools with no date limit and
  this page reads a bounded span, so the read reaches ``lead_days`` past
  the drawn window (store.py) and the nights in that lead-in are pooled
  but not drawn; a window that is still short while the listing shows
  older objects at that key beyond the read (``older`` in store.json:
  older than the span, or trimmed by the cap) is marked ``cut`` and the
  page says so instead of "not full", so the line here and the record's
  verdict never disagree silently.
* **Judged quality is advisory and never a single point.** A record carries
  a mean and its ``n`` per metric and not the repetitions' own values, so
  the spread across repetitions is not in the store yet (an addition to the
  record; the design names it). What the page can show honestly is drawn
  instead: for a case, the range of its nightly means over the trailing
  ``SPREAD_NIGHTS`` nights at the same key (a band; a lone night has none and
  is drawn as one marked point with its ``n``); for a domain, the
  ``n``-weighted mean across its cases with the range of their means.

ONLY NIGHTLY RECORDS. The store holds what ran on ``main``: only the nightly
appends to it (a pull request's run is graded against it and never writes),
so a trend read from the store is the nightly's by construction; the
presubmit's judged scores stay on the run page.

THE VERSION KEY. Each record carries the five-component key it was measured
at (``setup_id``, ``scoring_version``, ``judge_model``, ``fleet``,
``verifiers``). Records are compared in time order per case; where the key
differs from the record before, that night is a marker naming the
components that changed. Pooling (the trailing window, the spread) never
crosses a key: a baseline measured on other software is not evidence about
this one.

Only stdlib, like ``nightly.py``.
"""

from __future__ import annotations

import datetime
import itertools
import math

try:
    from eval_dashboard import nightly, tiers
except ImportError:  # run as a script from scripts/eval_dashboard/
    import nightly  # type: ignore[no-redef]
    import tiers  # type: ignore[no-redef]

# The report page render.py writes and the Cases page links to.
TREND_PAGE = "trend.html"
# The admission bar computed admission uses (bench/kube_agents_bench/
# baselines.py: DEFAULT_ADMISSION_RATE, DEFAULT_ADMISSION_MIN_RUNS). The
# page draws the bar and pools the window by these; the gate reads its own
# from the environment, so a tuned bar shows here only when these move too.
ADMISSION_RATE = 0.95
ADMISSION_MIN_RUNS = 20
# How many trailing nights a case's judged range spans (a week of nightlies:
# the same span that fills an admission window at three repetitions).
SPREAD_NIGHTS = 7
# The judged metric the page opens on: rung 6's default (EVAL_JUDGED_METRICS).
DEFAULT_METRIC = "OutcomeValidity"
DAY_MS = 24 * 3600 * 1000
KEY_COMPONENTS = ("setup_id", "scoring_version", "judge_model", "fleet", "verifiers")
DOMAIN_UNKNOWN = nightly.DOMAIN_UNKNOWN
UTC = datetime.timezone.utc


# --------------------------------------------------------------------------
# records


def key_id(key: dict) -> str:
    """The key as one readable string, the store's own directory order:
    ``<setup_id>/<judge_model>/<scoring_version>-f<fleet>-v<verifiers>``."""
    return (
        f"{key.get('setup_id') or 'unknown-setup'}/{key.get('judge_model') or 'unknown-judge'}/"
        f"{key.get('scoring_version') or 'unknown'}-f{key.get('fleet')}-v{key.get('verifiers')}"
    )


def key_document(key: dict) -> dict:
    return {name: key.get(name) for name in KEY_COMPONENTS}


def path_segment(text) -> str:
    """One directory segment as the store's writer spells it
    (``evidence_store._sanitize``, mirrored here rather than imported: the
    dashboard is stdlib only): anything outside letters, digits, ``-_.``
    becomes ``-``, so ``vendor/model:tag`` cannot add a path level."""
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in str(text))


def key_path(key: dict | None) -> str:
    """The version key as the directory path under the case, the way the
    writer files it (``evidence_store._key_segments``): setup, judge, then
    the versions segment, each sanitised; ``unkeyed`` for a record with no
    key. store.json's ``older`` is keyed by this path, since the listing
    sees paths and not records; ``key_id`` is the readable form the page
    shows, and the two differ as soon as a component holds a character the
    writer rewrites."""
    if not key:
        return "unkeyed"
    versions = f"{key.get('scoring_version') or 'unknown'}-f{key.get('fleet')}-v{key.get('verifiers')}"
    return "/".join(path_segment(part) for part in (key.get("setup_id") or "unknown-setup", key.get("judge_model") or "unknown-judge", versions))


def key_changes(before: dict, after: dict) -> list[str]:
    return [name for name in KEY_COMPONENTS if before.get(name) != after.get(name)]


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _mean(value) -> float | None:
    """A finite number, or None. ``json.loads`` accepts ``NaN`` and
    ``Infinity`` and ``json.dumps`` would write them back into the inlined
    page, where ``JSON.parse`` refuses them and the page cannot render; a
    judge's non-finite score is no score."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def judged_of(record: dict) -> dict[str, dict]:
    """``{metric: {mean, n}}`` from a record's ``judged`` block; a metric
    without a usable mean and a positive ``n`` is absent, never zero."""
    raw = record.get("judged")
    if not isinstance(raw, dict):
        return {}
    out = {}
    for metric, blob in raw.items():
        if not isinstance(blob, dict):
            continue
        mean, n = _mean(blob.get("mean")), _count(blob.get("n"))
        if mean is None or n <= 0:
            continue
        out[str(metric)] = {"mean": mean, "n": n}
    return out


def usable_records(store: dict | None) -> list[dict]:
    """The store's records that carry what the page reads, oldest first."""
    if not store or not isinstance(store.get("records"), list):
        return []
    out = []
    for record in store["records"]:
        if not isinstance(record, dict) or not isinstance(record.get("case"), str) or not isinstance(record.get("key"), dict):
            continue
        if nightly.parse_iso(record.get("recorded_at")) is None:
            continue
        out.append(record)
    out.sort(key=lambda r: (r["recorded_at"], r["case"], str(r.get("object") or "")))
    return out


# --------------------------------------------------------------------------
# nights: the store's records joined to the collector's nightly runs


def night_id(record: dict) -> str:
    """One nightly job's records share its build; a record without one (a
    laptop run, a name outside the layout) stands alone under its stamp."""
    build = record.get("build")
    return f"build:{build}" if isinstance(build, str) and build else f"at:{record['recorded_at']}"


def nightly_runs_by_build(data: dict) -> dict[str, dict]:
    return {
        str(run["build_id"]): run
        for run in tiers.nightly_runs([r for r in data.get("runs") or [] if isinstance(r, dict)])
        if isinstance(run.get("build_id"), str)
    }


def night_documents(records: list[dict], data: dict) -> list[dict]:
    """``nights[]``, oldest first: each night the store holds a record for,
    with the collector's start time and Spyglass link when its build is a
    nightly run on record. The page dates every night by ``at`` (the newest
    ``recorded_at`` of the night), the same stamp its points are placed by;
    ``started`` and ``log_url`` only serve the link to the report."""
    runs = nightly_runs_by_build(data)
    nights: dict[str, dict] = {}
    for record in records:
        nid = night_id(record)
        night = nights.get(nid)
        if night is None:
            build = record.get("build") if isinstance(record.get("build"), str) else None
            run = runs.get(build) if build else None
            started = nightly.parse_iso(run.get("started")) if run else None
            night = nights[nid] = {
                "id": nid,
                "at": record["recorded_at"],
                "build": build,
                "commit": record.get("commit") if isinstance(record.get("commit"), str) else None,
                "started": started.isoformat() if started else None,
                "log_url": nightly.build_url(run) if run else None,
                "cases": 0,
            }
        night["cases"] += 1
        night["at"] = max(night["at"], record["recorded_at"])
    return sorted(nights.values(), key=lambda n: n["at"])


# --------------------------------------------------------------------------
# per case


def at_ms(point: dict) -> float | None:
    parsed = nightly.parse_iso(point.get("at"))
    return parsed.timestamp() * 1000 if parsed else None


def trailing_window(points: list[dict], index: int, older: dict[str, int] | None = None) -> dict:
    """What admission would read at ``points[index]``: the newest whole
    records at its key pooled back until ``ADMISSION_MIN_RUNS`` runs
    (``BaselineStore.evidence_for``: whole lines, so a pool of threes
    overshoots to 21 rather than pretending to 20). ``full`` says whether
    the bar's run count was reached. ``cut`` says the pool ran out of
    records inside the read while the listing showed older objects at
    this key that the read left behind (``older``, ``{key: count}`` from
    store.json: older than the span, or trimmed by the cap), so the store
    holds records that admission pools and this page did not read. A
    short pool with nothing older at its key is genuinely short
    (``collecting``)."""
    key = points[index]["key"]
    runs = passes = lines = 0
    for point in reversed(points[: index + 1]):
        if point["key"] != key:
            continue
        runs += point["runs"]
        passes += point["passes"]
        lines += 1
        if runs >= ADMISSION_MIN_RUNS:
            break
    full = runs >= ADMISSION_MIN_RUNS
    cut = not full and bool(older) and _count(older.get(key)) > 0
    return {"runs": runs, "passes": passes, "lines": lines, "full": full, "cut": cut}


def trailing_spread(points: list[dict], index: int, metric: str) -> dict | None:
    """The range of the case's nightly means for ``metric`` over the last
    ``SPREAD_NIGHTS`` points at the same key, ending at ``points[index]``:
    ``{low, high, nights}``. ``nights`` is how many means the range spans;
    one is no spread, and the page draws that night as a lone point."""
    key = points[index]["key"]
    means = []
    for point in reversed(points[: index + 1]):
        if point["key"] != key:
            continue
        blob = point["judged"].get(metric)
        if blob:
            means.append(blob["mean"])
        if len(means) >= SPREAD_NIGHTS:
            break
    if not means:
        return None
    return {"low": min(means), "high": max(means), "nights": len(means)}


def case_points(records: list[dict], older: dict[str, int] | None = None) -> list[dict]:
    """One case's records as points, oldest first, each with its trailing
    window and, per metric, its trailing spread. ``older`` is what the
    listing left behind at each of the case's keys (``trailing_window``)."""
    points = []
    for record in records:
        points.append({
            "night": night_id(record),
            "at": record["recorded_at"],
            "build": record.get("build") if isinstance(record.get("build"), str) else None,
            "commit": record.get("commit") if isinstance(record.get("commit"), str) else None,
            "key": key_id(record["key"]),
            "runs": _count(record.get("runs")),
            "passes": _count(record.get("passes")),
            "blocked": _count(record.get("blocked")),
            "infra": _count(record.get("infra")),
            "judged": judged_of(record),
        })
    for index, point in enumerate(points):
        point["window"] = trailing_window(points, index, older)
        for metric, blob in point["judged"].items():
            blob["spread"] = trailing_spread(points, index, metric)
    return points


def case_key_changes(points: list[dict], keys: dict[str, dict]) -> list[dict]:
    """The nights whose key differs from the record before: ``{night, at,
    from, to, changed[]}`` with the component names that moved."""
    out = []
    for before, after in itertools.pairwise(points):
        if before["key"] == after["key"]:
            continue
        out.append({
            "night": after["night"],
            "at": after["at"],
            "from": before["key"],
            "to": after["key"],
            "changed": key_changes(keys[before["key"]], keys[after["key"]]),
        })
    return out


def record_state(points: list[dict]) -> dict | None:
    """What the store says about the case today, in the record's own
    vocabulary (``RECORD_*`` in baselines.py): the newest point's window
    against the bar. ``None`` with no point."""
    if not points:
        return None
    newest = points[-1]
    window = newest["window"]
    rate = window["passes"] / window["runs"] if window["runs"] else None
    if window["full"]:
        state = "would-admit" if rate is not None and rate >= ADMISSION_RATE else "would-demote"
    elif window.get("cut"):
        state = "cut"
    else:
        state = "collecting"
    return {
        "state": state,
        "key": newest["key"],
        "runs": window["runs"],
        "passes": window["passes"],
        "lines": window["lines"],
        "rate": rate,
        "bar": {"rate": ADMISSION_RATE, "min_runs": ADMISSION_MIN_RUNS},
        "as_of": newest["at"],
    }


# --------------------------------------------------------------------------
# per domain


def domain_points(cases: dict[str, dict]) -> list[dict]:
    """The domain's cases pooled per night, oldest first: runs and passes
    summed, and per metric the ``n``-weighted mean across the cases that
    night with the range of their means (``low``, ``high``, ``cases``).
    A night mixes keys only if its cases do; the page marks the key
    changes, it does not pool across them here because a domain's cases
    share one nightly job and therefore one key."""
    by_night: dict[str, dict] = {}
    for doc in cases.values():
        for point in doc["points"]:
            night = by_night.setdefault(point["night"], {"night": point["night"], "at": point["at"], "runs": 0, "passes": 0, "cases": 0, "keys": set(), "judged": {}})
            night["runs"] += point["runs"]
            night["passes"] += point["passes"]
            night["cases"] += 1
            night["keys"].add(point["key"])
            night["at"] = max(night["at"], point["at"])
            for metric, blob in point["judged"].items():
                acc = night["judged"].setdefault(metric, {"sum": 0.0, "n": 0, "means": []})
                acc["sum"] += blob["mean"] * blob["n"]
                acc["n"] += blob["n"]
                acc["means"].append(blob["mean"])
    out = []
    for night in sorted(by_night.values(), key=lambda n: n["at"]):
        judged = {
            metric: {"mean": acc["sum"] / acc["n"], "n": acc["n"], "low": min(acc["means"]), "high": max(acc["means"]), "cases": len(acc["means"])}
            for metric, acc in night["judged"].items() if acc["n"] > 0
        }
        out.append({"night": night["night"], "at": night["at"], "runs": night["runs"], "passes": night["passes"], "cases": night["cases"], "keys": sorted(night["keys"]), "judged": judged})
    return out


def domain_key_changes(cases: dict[str, dict]) -> list[dict]:
    """The cases' key changes, one per night and transition."""
    seen: dict[tuple, dict] = {}
    for doc in cases.values():
        for change in doc["key_changes"]:
            seen.setdefault((change["night"], change["from"], change["to"]), change)
    return sorted(seen.values(), key=lambda c: c["at"])


# --------------------------------------------------------------------------
# the document


def read_span(store: dict | None) -> tuple[float | None, int | None, int]:
    """``(drawn_since_ms, window_days, lead_days)`` from the store document:
    the page draws nights from ``read_at - window_days``; the read began
    ``lead_days`` earlier (store.py) and those records are pooled only. A
    store without a usable ``read_at`` or window bounds nothing:
    everything is drawn."""
    if not store:
        return None, None, 0
    read_at = nightly.parse_iso(store.get("read_at"))
    window_days = store.get("window_days") if isinstance(store.get("window_days"), int) and not isinstance(store.get("window_days"), bool) else None
    lead_days = store.get("lead_days") if isinstance(store.get("lead_days"), int) and not isinstance(store.get("lead_days"), bool) and store.get("lead_days") >= 0 else 0
    if read_at is None or window_days is None:
        return None, window_days, lead_days
    return read_at.timestamp() * 1000 - window_days * DAY_MS, window_days, lead_days


def partial_read(store: dict | None) -> dict | None:
    """store.json's ``partial`` (``{fetched, remaining}``: the read stopped
    at its deadline with objects left for the next tick), or None."""
    raw = store.get("partial") if store else None
    if not isinstance(raw, dict) or _count(raw.get("remaining")) <= 0:
        return None
    return {"fetched": _count(raw.get("fetched")), "remaining": _count(raw.get("remaining"))}


def older_by_case(store: dict | None) -> dict[str, dict[str, int]]:
    """store.json's ``older`` (``{case directory: {key path: n}}``, what the
    listing left behind, both as the writer spells them), with only usable
    counts."""
    raw = store.get("older") if store else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for case, keys in raw.items():
        if not isinstance(keys, dict):
            continue
        counts = {str(k): v for k, v in keys.items() if _count(v) > 0}
        if counts:
            out[str(case)] = counts
    return out


def trend_document(store: dict | None, data: dict) -> dict:
    """The ``trend`` block of brief.json. ``store`` is store.json as
    ``store.py`` wrote it, or None when the render had none (the page then
    says the store was not read this tick). Records in the read's lead-in
    (older than the drawn window) feed the trailing windows and spreads of
    the first drawn nights and appear nowhere else: not as points, nights,
    key changes or in ``records``."""
    every = usable_records(store)
    drawn_since_ms, window_days, lead_days = read_span(store)
    older_paths = older_by_case(store)
    in_window = lambda at: drawn_since_ms is None or (at is not None and at >= drawn_since_ms)
    records = [r for r in every if in_window(at_ms({"at": r["recorded_at"]}))]
    domain_of = nightly.domains(data)
    keys: dict[str, dict] = {}
    by_case: dict[str, list[dict]] = {}
    metrics: set[str] = set()
    paths: dict[str, str] = {}  # key id -> the directory path the writer files it under
    for record in every:
        keys.setdefault(key_id(record["key"]), key_document(record["key"]))
        paths.setdefault(key_id(record["key"]), key_path(record["key"]))
        by_case.setdefault(record["case"], []).append(record)
    for record in records:
        metrics.update(judged_of(record))
    cases = {}
    for name in sorted(by_case):
        # What the listing left behind at each of this case's keys, joined
        # through the paths the writer files them under.
        left = older_paths.get(path_segment(name)) or {}
        older = {kid: left[paths[kid]] for kid in {key_id(r["key"]) for r in by_case[name]} if paths[kid] in left}
        points = case_points(by_case[name], older)
        drawn = [p for p in points if in_window(at_ms(p))]
        if not drawn:
            continue  # recorded in the lead-in only: outside the page's window
        cases[name] = {
            "domain": domain_of.get(name, DOMAIN_UNKNOWN),
            "points": drawn,
            "key_changes": [c for c in case_key_changes(points, keys) if in_window(at_ms(c))],
            "record": record_state(points),
        }
    domains = {}
    for domain in sorted({doc["domain"] for doc in cases.values()}):
        members = {name: doc for name, doc in cases.items() if doc["domain"] == domain}
        domains[domain] = {"cases": sorted(members), "points": domain_points(members), "key_changes": domain_key_changes(members)}
    metric_list = sorted(metrics, key=lambda m: (m != DEFAULT_METRIC, m))
    return {
        "source": store.get("source") if store and isinstance(store.get("source"), str) else None,
        "read_at": store.get("read_at") if store and nightly.parse_iso(store.get("read_at")) is not None else None,
        "error": store.get("error") if store and isinstance(store.get("error"), str) else None,
        "window_days": window_days,
        "lead_days": lead_days,
        "max_objects": store.get("max_objects") if store and isinstance(store.get("max_objects"), int) else None,
        "truncated": {str(k): v for k, v in (store.get("truncated") or {}).items() if isinstance(v, int)} if store and isinstance(store.get("truncated"), dict) else {},
        "partial": partial_read(store),
        "warnings": [w for w in (store.get("warnings") or []) if isinstance(w, str)] if store else [],
        "records": len(records),
        "metrics": metric_list,
        "default_metric": DEFAULT_METRIC if DEFAULT_METRIC in metrics else (metric_list[0] if metric_list else None),
        "spread_nights": SPREAD_NIGHTS,
        "bar": {"rate": ADMISSION_RATE, "min_runs": ADMISSION_MIN_RUNS},
        "keys": keys,
        "nights": night_documents(records, data),
        "cases": cases,
        "domains": domains,
    }
