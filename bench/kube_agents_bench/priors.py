"""Where a case's pass rate on record comes from, for planning and comparison.

A three-repetition run says little on its own; against hundreds of recorded
repetitions of the same case it says something. Three sources supply that
record, and ``bench-run`` accepts any of them as ``--baseline``:

* ``dashboard`` (the default) -- the eval dashboard's ``data.json``, which
  every eval job feeds. It carries two records per case: the nightly's, which
  runs ``main`` and is the record a developer wants, under ``cases[].nightly``;
  and the pooled presubmit record of every pull-request build at the top
  level. The nightly is young and holds a handful of runs per case, so the
  reader takes it once it holds :data:`MIN_NIGHTLY_RUNS` runs (the gate's own
  admission window) and the presubmit record until then, and names which in
  :attr:`Prior.source` so the plan and the summary can say which record a
  number is against. The presubmit record is pull-request branches, most of
  them ``main`` plus one change, so it is a serviceable stand-in and an
  honest label matters. Read through ``gcloud storage cat`` and cached for an
  hour, because the file is republished every fifteen minutes and a laptop
  asks for it many times in an afternoon.
* a ``summary.json`` written by an earlier ``bench-run run`` -- the A in an
  A/B, typically ``main`` built and run on the same install as the branch.
* ``none`` -- no prior; the summary prints intervals and no p-values.

A local path or ``gs://`` URL is accepted in place of the ``dashboard`` name
and is read the same way; the file's shape decides which of the two formats
it is.

The dashboard's rates are graded rates (infrastructure losses excluded) and
its run counts count every repetition, so the pass count is recovered as
``round(rate * runs)`` and can be off by the handful of infrastructure rows.
At the record sizes involved that moves nothing a developer would act on.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from kube_agents_bench.baselines import DEFAULT_ADMISSION_MIN_RUNS

__all__ = [
    "BASELINE_DASHBOARD",
    "BASELINE_NONE",
    "DASHBOARD_DATA_URL",
    "MIN_NIGHTLY_RUNS",
    "Prior",
    "PriorsError",
    "TIER_NIGHTLY",
    "TIER_PRESUBMIT",
    "load_priors",
]

# The named sources. Anything else is a path or a gs:// URL.
BASELINE_DASHBOARD = "dashboard"
BASELINE_NONE = "none"

# The dashboard bucket's data file: the object every eval job republishes,
# read-only for anyone with viewer access to the dashboards bucket.
DASHBOARD_DATA_URL = "gs://kube-agents-dashboards/evals/data.json"

# The two records data.json carries per case, and the suffix a Prior's
# source carries to say which one it came from.
TIER_NIGHTLY = "nightly"
TIER_PRESUBMIT = "presubmit"
NIGHTLY_KEY = "nightly"

# Runs the nightly record must hold before it is preferred over the pooled
# presubmit record: the gate's admission window, the same constant.
MIN_NIGHTLY_RUNS = DEFAULT_ADMISSION_MIN_RUNS

# Cache for a fetched dashboard file (under XDG_CACHE_HOME or ~/.cache,
# resolved at read time so importing this module needs no home directory),
# and how long a copy stays fresh: an hour, against a dashboard republished
# every fifteen minutes.
CACHE_HOME_ENV = "XDG_CACHE_HOME"
CACHE_HOME_FALLBACK = ".cache"
CACHE_SUBDIR = "kube-agents-bench"
CACHE_TTL_S = 3600

# How long one `gcloud storage cat` may take before the prior is given up on.
FETCH_TIMEOUT_S = 60

# The gcloud binary the fetch shells out to, as the evidence store does.
GCLOUD = "gcloud"
GS_PREFIX = "gs://"

# The file a bench-run run set writes at its root (runner.py's SUMMARY_JSON;
# this module cannot import it).
SUMMARY_FILE = "summary.json"

# Outcomes a bench-run summary counts toward a rate: scoring.RepResult's
# pass and fail (runner.py names the same two; this module cannot import it).
SCORED_OUTCOMES = ("pass", "fail")


class PriorsError(RuntimeError):
    """A baseline source that cannot be read."""


@dataclass(frozen=True)
class Prior:
    """A case's record on the baseline: pass count, run count, duration."""

    case_id: str
    passes: int
    n: int
    median_seconds: float | None
    source: str

    @property
    def rate(self) -> float | None:
        return self.passes / self.n if self.n else None

    @property
    def fails(self) -> int:
        return self.n - self.passes


def _read_gs(url: str) -> str:
    if shutil.which(GCLOUD) is None:
        raise PriorsError(f"{GCLOUD} is not on PATH; pass --baseline none or a local file")
    try:
        proc = subprocess.run(
            [GCLOUD, "storage", "cat", url],
            capture_output=True,
            text=True,
            timeout=FETCH_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PriorsError(f"reading {url} timed out after {FETCH_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        raise PriorsError(f"reading {url} failed: {proc.stderr.strip()}")
    return proc.stdout


def _cache_path(url: str, cache_dir: Path) -> Path:
    return cache_dir / (url[len(GS_PREFIX):].replace("/", "__"))


def _default_cache_dir() -> Path:
    return Path(os.environ.get(CACHE_HOME_ENV, Path.home() / CACHE_HOME_FALLBACK)) / CACHE_SUBDIR


def _read_cached_gs(url: str, *, cache_dir: Path | None = None, ttl_s: float = CACHE_TTL_S, reader=_read_gs) -> str:
    cache_dir = cache_dir or _default_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = _cache_path(url, cache_dir)
    if cached.is_file() and time.time() - cached.stat().st_mtime < ttl_s:
        return cached.read_text(encoding="utf-8")
    text = reader(url)
    cached.write_text(text, encoding="utf-8")
    return text


def _tier_record(record: dict, name: str, source: str, med: float | None) -> Prior | None:
    runs = int(record.get("runs_on_record") or 0)
    rate = record.get("pass_rate")
    if runs <= 0 or rate is None:
        return None
    return Prior(case_id=name, passes=round(float(rate) * runs), n=runs, median_seconds=med, source=source)


def _from_dashboard(doc: dict, source: str) -> dict[str, Prior]:
    out: dict[str, Prior] = {}
    cases = doc.get("cases") or []
    if isinstance(cases, dict):
        cases = list(cases.values())
    for case in cases:
        name = str(case.get("name") or "")
        if not name:
            continue
        durations = case.get("durations") or {}
        med = float(durations["med"]) if durations.get("med") else None
        nightly = _tier_record(case.get(NIGHTLY_KEY) or {}, name, f"{source}#{TIER_NIGHTLY}", med)
        presubmit = _tier_record(case, name, f"{source}#{TIER_PRESUBMIT}", med)
        if nightly and nightly.n >= MIN_NIGHTLY_RUNS:
            out[name] = nightly
        elif presubmit:
            out[name] = presubmit
        elif nightly:
            out[name] = nightly
    return out


def _from_summary(doc: dict, source: str) -> dict[str, Prior]:
    out: dict[str, Prior] = {}
    for case in doc.get("cases") or []:
        name = str(case.get("case_id") or "")
        passes = int(case.get("passes") or 0)
        fails = int(case.get("fails") or 0)
        n = passes + fails
        if not name or n <= 0:
            continue
        seconds = [
            float(rep["seconds"])
            for rep in case.get("reps") or []
            if rep.get("seconds") is not None and rep.get("outcome") in SCORED_OUTCOMES
        ]
        med = sorted(seconds)[len(seconds) // 2] if seconds else None
        out[name] = Prior(case_id=name, passes=passes, n=n, median_seconds=med, source=source)
    return out


def _parse(text: str, source: str) -> dict[str, Prior]:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PriorsError(f"{source}: not JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise PriorsError(f"{source}: expected a JSON object")
    # A bench-run summary names its cases by `case_id`; the dashboard by `name`.
    cases = doc.get("cases")
    first = None
    if isinstance(cases, list) and cases:
        first = cases[0]
    if isinstance(first, dict) and "case_id" in first:
        return _from_summary(doc, source)
    return _from_dashboard(doc, source)


def load_priors(baseline: str) -> dict[str, Prior]:
    """Resolve ``--baseline`` to per-case priors. ``none`` is an empty dict."""
    if baseline == BASELINE_NONE:
        return {}
    if baseline == BASELINE_DASHBOARD:
        return _parse(_read_cached_gs(DASHBOARD_DATA_URL), DASHBOARD_DATA_URL)
    if baseline.startswith(GS_PREFIX):
        return _parse(_read_cached_gs(baseline), baseline)
    path = Path(baseline)
    if path.is_dir():
        path = path / SUMMARY_FILE
    if not path.is_file():
        raise PriorsError(f"baseline {baseline!r}: no such file")
    return _parse(path.read_text(encoding="utf-8"), str(path))
