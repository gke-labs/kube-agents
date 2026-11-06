"""Report eval inference cost, tokens, cache hits, model time, rate limits and reliability.

Reads the shadow proxy's JSON lines (replay_proxy.py; field names in
schema.py) and, optionally, the eval dashboard's data.json, and reports per job
kind (nightly, presubmit), per build, per case where the eval data names one,
and over the whole window. The same script produces the baseline frozen before
replay is switched on and every report after it, and `--compare` prints the
headline deltas between a saved report and a new one, so the two are measured
the same way (examples/inference-replay/eval-design.md, Rollout).

Inputs:

- LOG paths: segment files and job manifests, or directories searched for
  `*.jsonl` and `*.jsonl.open` segments and `job-<ms>.json` manifests. A
  file's job kind is the kind folder it was filed under at the collector's
  layout (filed_kind: `<kind>/<YYYY-MM-DD>/<build>/logs/<segment>.jsonl` or
  `<kind>/<YYYY-MM-DD>/<build>/job-<ms>.json`, with no other kind-named
  directory in its path), or `--kind`; never its lines' own tags, and a line
  whose tag disagrees is dropped. A file found under a directory off that
  layout is skipped and counted;
- `--eval-data data.json` (repeatable): the dashboard's runs, joined to the
  lines on the Prow build id (`job.build` == `runs[].build_id`);
- `--prices`: the dated price table (default: model-prices.json beside this
  script), used for lines without LiteLLM's own cost; `--price-as GROUP=MODEL`
  prices a logged model group (the LiteLLM alias, `model-default` on an eval
  install) as a model in the table when the line's litellm_model_name is not
  in it.

Every metric is defined once, in METRICS and CASE_METRICS below; the README's
table of them is generated from those (`make docs-generate`).

Usage:
  python3 baseline_report.py LOG [LOG ...] [--kind KIND] [--eval-data data.json]
      [--price-as model-default=MODEL] [--since DATE] [--until DATE] [--json]
  python3 baseline_report.py LOG [LOG ...] ... --compare BASELINE.json [--json]
  python3 baseline_report.py --report CURRENT.json --compare BASELINE.json [--json]
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, NamedTuple, Optional, Sequence, Set, Tuple

import analyze_shadow
import replay_proxy
import schema as F

REPORT_SCHEMA_VERSION = 1
NIGHTLY = analyze_shadow.NIGHTLY
PRESUBMIT = analyze_shadow.PRESUBMIT
# A scheduled run whose shadow logs are filed apart from the trusted nightly's
# because it did not upload as the baseline recorder (the GitLab nightly, for
# one). analyze_shadow names it once it knows the kind; until then, the same
# value here.
NIGHTLY_UNTRUSTED = getattr(analyze_shadow, "NIGHTLY_UNTRUSTED", "nightly-untrusted")
# Every kind this report counts, whether or not analyze_shadow.JOB_KINDS lists
# it yet.
JOB_KINDS = (NIGHTLY, NIGHTLY_UNTRUSTED, PRESUBMIT)
TOTAL_SCOPE = "total"
SCOPES = (TOTAL_SCOPE,) + JOB_KINDS
HTTP_OK = 200
HTTP_TOO_MANY_REQUESTS = 429
# The value of x-litellm-attempted-fallbacks when LiteLLM tried no fallback.
NO_FALLBACKS = replay_proxy.NO_FALLBACKS

DEFAULT_PRICES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model-prices.json")
TOKENS_PER_PRICE_UNIT = 1_000_000
PRICE_FIELDS = ("input", "cached_input", "output")
# Price table keys.
PRICES_AS_OF = "as_of"
PRICES_SOURCE = "source"
PRICES_MODELS = "models"
# False (or absent) until someone has checked the prices against the
# provider's price page on as_of.
PRICES_CONFIRMED = "confirmed"
PRICES_SHA256 = "sha256"
PRICES_PRICE_AS = "price_as"
LONG_CONTEXT = "long_context"
LONG_CONTEXT_THRESHOLD = "above_prompt_tokens"
UNPRICED_NO_MODEL = "(no model)"
PRICE_AS_SEPARATOR = "="
COST_SOURCE_LITELLM, COST_SOURCE_TABLE, COST_SOURCE_MIXED = "litellm", "price table", "both"
# LiteLLM names a deployment's model `<provider>/<model>`.
PROVIDER_SEPARATOR = "/"

SEGMENT_SUFFIXES = (".jsonl", ".jsonl.open")
MANIFEST_NAME = re.compile(r"^job-(\d+)\.json$")
# Where collect_shadow.py files under the bucket's BUCKET_ROOT: a segment at
# <kind>/<YYYY-MM-DD>/<build>/logs/<segment>.jsonl and the job manifest at
# <kind>/<YYYY-MM-DD>/<build>/job-<ms>.json. The build is BUILD_ID's shape
# (hack/ci-env.sh, REPLAY_SHADOW_BUILD_RE), the segment name the logs/ form of
# collect_shadow.SAFE_NAME, and the manifest collect_shadow.write_manifest's.
# The same values as analyze_shadow's (kube-agents#4982), for filed_kind.
BUCKET_ROOT = "replay-shadow"
LOGS_DIR = "logs"
DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BUILD_DIR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
SEGMENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.jsonl$")
FILED_DEPTH = 5
MANIFEST_DEPTH = 4
LAYOUT = f"<{'|'.join(JOB_KINDS)}>/<YYYY-MM-DD>/<build>/{LOGS_DIR}/<segment>.jsonl or <build>/job-<ms>.json"
# The listing of lines without a build id: they are no build, and stay out of
# `builds` and every per-build figure.
NO_BUILD = "(lines without a build id; not a build)"

MS_PER_SECOND = 1000.0
P50, P95 = 50, 95
PERCENT = 100.0
RATE_DECIMALS = 4
USD_DECIMALS = 6
USD_DISPLAY_DECIMALS = 2
SECONDS_DECIMALS = 1
PER_BUILD_DECIMALS = 1
DELTA_DECIMALS = 1
DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATE_FORMAT = "%Y-%m-%d"
# Epoch seconds: a plain run of digits, optionally with a fraction. Fewer than
# nine digits is before 1973 and more likely a date written without dashes
# (20261105 would be August 1970), so it is refused.
EPOCH_SECONDS = re.compile(r"^\d+(\.\d+)?$")
EPOCH_MIN_DIGITS = 9
# Upper bounds on what a line or an option may say, so an absurd value is a
# bad value rather than an overflow: the last second of year 9999 (the largest
# time datetime can show), a day of model time for one request (an eval job's
# deadline is 14 hours), a trillion for any token or other count, and a
# million dollars for one request's cost.
MAX_EPOCH_SECONDS = 253402300799
MAX_LATENCY_MS = 24 * 60 * 60 * 1000
MAX_COUNT = 10 ** 12
MAX_REQUEST_COST_USD = 1_000_000.0
DECIMAL_POINT = "."
# What a date or an ISO time starts with; anything else (nan, inf) is refused.
ISO_START = re.compile(r"^\d{4}-\d{2}-\d{2}")
ISO_TIMESPEC = "seconds"
# The status_counts label of a line that carried no usable status.
STATUS_NONE = "none"

# Eval data (scripts/eval_dashboard/SCHEMA.md): its keys, and a repetition's results.
EVAL_RUNS = "runs"
EVAL_BUILD_ID = "build_id"
EVAL_TIER = "tier"
EVAL_PR = "pr"
EVAL_TASKS = "tasks"
EVAL_TASK_NAME = "name"
EVAL_REPS = "reps"
EVAL_RESULT = "result"
EVAL_AGENT_S = "agent_s"
EVAL_TOKENS = "tokens"
EVAL_TOKENS_TOTAL = "total"
# True when the harness's counts are short (a chain link or a worker unread).
EVAL_TOKENS_PARTIAL = "partial"
EVAL_PASS, EVAL_FAIL, EVAL_INFRA = "pass", "fail", "infra"
EVAL_RESULTS = (EVAL_PASS, EVAL_FAIL, EVAL_INFRA)
EVAL_TIER_DEFAULT = PRESUBMIT
EVAL_TIER_NIGHTLY = "nightly"
# The forge lanes' tiers (scripts/eval_dashboard/tiers.py FORGE_TIERS): one
# tier covers a lane's pull-request builds (with a `pr`) and its nightlies
# (`pr` null), so the run's `pr` says which job kind it is.
EVAL_FORGE_TIERS = ("gitlab", "ghes", "gitlab-ce")

# Why a line or file was not counted, for the data-quality notes. not_json and
# other_event come from analyze_shadow.iter_request_lines.
SKIP_UNTAGGED = analyze_shadow.SKIP_UNTAGGED
SKIP_BAD_TS = "bad_ts"
SKIP_DUPLICATE = "duplicate_request_id"
SKIP_OUTSIDE_WINDOW = "outside_window"
# The line's job.kind tag names a kind other than the folder it was filed under.
SKIP_KIND_MISMATCH = "kind_mismatch"

# Report keys read back by render() and compare().
REPORT_SCHEMA = "report_schema"
REPORT_WINDOW = "window"
REPORT_PRICES = "prices"
REPORT_SCOPES = "scopes"
REPORT_BUILDS = "builds"
REPORT_CASES = "cases"
REPORT_DATA_QUALITY = "data_quality"
BUILD_METRICS = "metrics"
COMPARE_BASELINE_PRICES = "baseline_prices"
COMPARE_CURRENT_PRICES = "current_prices"
COMPARE_NOTES = "notes"
COMPARE_BASELINE_WINDOW = "baseline_window"
COMPARE_CURRENT_WINDOW = "current_window"
WINDOW_SINCE = "since"
WINDOW_UNTIL = "until"
WINDOW_FIRST_TS = "first_request_ts"
WINDOW_LAST_TS = "last_request_ts"
# A scope's breakdowns beside its metrics.
STATUS_COUNTS = "status_counts"
UNPRICED_BY_MODEL = "unpriced"

# Text layout: column widths and how much of the price table's hash to show.
METRIC_COLUMN_WIDTH = 32
SCOPE_COLUMN_WIDTH = 18
COMPARE_COLUMN_WIDTH = 16
PERCENT_COLUMN_WIDTH = 10
KIND_COLUMN_WIDTH = 9
HASH_DISPLAY_CHARS = 12
PRICES_CONFIRMED_LABEL = "confirmed"
PRICES_UNCONFIRMED_LABEL = "not confirmed"

# Metric units.
COUNT, TOKENS, FRACTION, USD, SECONDS, MS = "count", "tokens", "fraction", "usd", "seconds", "ms"


@dataclass(frozen=True)
class Metric:
    name: str
    unit: str
    definition: str
    headline: bool = False


# Every metric of a scope (the window, a job kind, or one build). Requests are
# counted from every request line in scope; tokens and cost from the lines
# LiteLLM answered with 200, since a refused request bills nothing; model time
# from every line with a latency, since a 429 or a 500 still cost the agent
# that wait. Eval metrics are null until a build in scope joins an eval run.
METRICS: Tuple[Metric, ...] = (
    Metric("builds", COUNT,
           "Distinct (job kind, build) pairs with at least one request line. Lines without a build id count in "
           "every sum but are no build, so they stay out of this and every per-build figure.", True),
    Metric("requests", COUNT,
           "Request lines, any status, including lines whose analysis failed or was dropped (each was a model "
           "request).", True),
    Metric("requests_per_build", COUNT, "requests from lines with a build id / builds.", True),
    Metric("ok_requests", COUNT, "Request lines with status 200."),
    Metric("prompt_tokens", TOKENS,
           "Sum of prompt_tokens over status-200 lines, estimated ones included (tokens_estimated: the response "
           "carried no usage and the proxy estimated from the request size).", True),
    Metric("cached_prompt_tokens", TOKENS, "Sum of cached_tokens over status-200 lines (missing counts as 0)."),
    Metric("uncached_prompt_tokens", TOKENS,
           "Sum over status-200 lines of max(prompt_tokens - cached_tokens, 0).", True),
    Metric("completion_tokens", TOKENS,
           "Sum of completion_tokens over status-200 lines; reasoning tokens are part of it.", True),
    Metric("reasoning_tokens", TOKENS,
           "Sum of reasoning_tokens (usage.completion_tokens_details.reasoning_tokens) over status-200 lines.", True),
    Metric("reasoning_token_share", FRACTION,
           "reasoning_tokens / completion_tokens, both over the status-200 lines that log reasoning_tokens (a line "
           "written before the field existed would read as no reasoning)."),
    Metric("cache_write_tokens", TOKENS,
           "Sum of cache_write_tokens (usage.cache_creation_input_tokens, Anthropic-style cache writes) over "
           "status-200 lines."),
    Metric("prompt_tokens_per_build", TOKENS, "prompt_tokens from lines with a build id / builds.", True),
    Metric("cache_hit_rate", FRACTION,
           "cached / prompt tokens, token-weighted, over status-200 lines whose usage was reported (not "
           "estimated): an estimated line has no cached count and would read as a miss.", True),
    Metric("estimated_prompt_token_share", FRACTION,
           "Prompt tokens on status-200 lines with tokens_estimated, over prompt_tokens."),
    Metric("cost_usd", USD,
           "Sum over status-200 lines of LiteLLM's own cost (litellm_cost_usd) where the line has it, else the "
           "price-table estimate (see table_cost_usd); cost_from_table_requests says how many lines took the "
           "table. Lines with neither are left out, and so are lines with estimated tokens and no LiteLLM cost: "
           "a partial figure when unpriced_requests or estimated_unpriced_requests > 0.", True),
    Metric("cost_per_build_usd", USD, "cost_usd from lines with a build id / builds.", True),
    Metric("litellm_cost_usd", USD, "Sum of litellm_cost_usd over the status-200 lines that carry it."),
    Metric("litellm_costed_requests", COUNT, "Status-200 lines that carry litellm_cost_usd."),
    Metric("table_cost_usd", USD,
           "Sum over status-200 lines with reported (not estimated) tokens that the price table prices (with or "
           "without a LiteLLM cost) of (uncached x "
           "input + cached x cached_input + completion x output) per 1M tokens, at the long-context price when "
           "the line's prompt_tokens exceed the table's above_prompt_tokens. Cache writes are not priced."),
    Metric("table_priced_requests", COUNT, "Status-200 lines with reported tokens that the price table prices."),
    Metric("cost_from_table_requests", COUNT,
           "Status-200 lines whose cost_usd came from the price table (no LiteLLM cost)."),
    Metric("table_to_litellm_cost_ratio", FRACTION,
           "The table estimate over LiteLLM's cost, both over the status-200 lines that have both: the cross-check "
           "of the price table (1.0 is agreement)."),
    Metric("unpriced_requests", COUNT,
           "Status-200 lines with reported tokens but neither a LiteLLM cost nor a price-table price."),
    Metric("estimated_token_requests", COUNT, "Status-200 lines with tokens_estimated."),
    Metric("estimated_unpriced_requests", COUNT,
           "Status-200 lines with tokens_estimated and no LiteLLM cost: an estimate of the prompt size and no "
           "completion count cannot be priced, so they are left out of cost_usd and table_cost_usd."),
    Metric("model_time_s", SECONDS,
           "Sum of latency_ms over lines that carry one, any status, in seconds. Concurrent requests add up, so "
           "this can exceed wall clock.", True),
    Metric("model_time_per_build_s", SECONDS, "model_time_s from lines with a build id / builds.", True),
    Metric("latency_p50_ms", MS, "Median latency_ms (nearest rank) over lines that carry one.", True),
    Metric("latency_p95_ms", MS, "95th percentile latency_ms (nearest rank) over lines that carry one.", True),
    Metric("non_200_requests", COUNT, "Lines whose status is not 200 (status_counts breaks them down).", True),
    Metric("rate_limited_requests", COUNT, "Lines with status 429.", True),
    Metric("rate_limited_rate", FRACTION, "rate_limited_requests / requests.", True),
    Metric("retried_requests", COUNT,
           "Lines, any status, whose litellm_attempted_retries (x-litellm-attempted-retries) is above 0: retries "
           "LiteLLM made inside one request, apart from the client's own retries, which are lines of their own.",
           True),
    Metric("litellm_retries", COUNT, "Sum of litellm_attempted_retries over lines that carry it.", True),
    Metric("fallback_attempted_requests", COUNT,
           "Lines whose x-litellm-attempted-fallbacks header was present and not 0.", True),
    Metric("upstream_incomplete_requests", COUNT, "Lines with upstream_incomplete true (the answer broke off)."),
    Metric("eval_builds_joined", COUNT, "Builds in scope whose build id is a run in the eval data."),
    Metric("agent_time_s", SECONDS, "Sum of agent_s over the repetitions of joined builds that carry it."),
    Metric("model_time_share_of_agent_time", FRACTION,
           "model_time_s over agent_time_s, both over the joined builds that have repetitions with agent_s. Model "
           "time outside a repetition (background agents, retries the harness never sees) counts too, so it is a "
           "ratio, not a share bounded by 1.", True),
    Metric("harness_tokens_total", TOKENS,
           "Sum of tokens.total over the repetitions of joined builds that carry it (the harness's own count; "
           "a cross-check on prompt_tokens + completion_tokens), leaving out repetitions whose tokens.partial is "
           "true."),
    Metric("harness_partial_reps", COUNT,
           "Repetitions of joined builds left out of harness_tokens_total because their tokens.partial is true."),
    Metric("graded_reps", COUNT, "Repetitions of joined builds graded pass or fail."),
    Metric("passed_reps", COUNT, "Repetitions graded pass."),
    Metric("infra_reps", COUNT, "Repetitions lost to infrastructure (infra), outside pass_rate."),
    Metric("pass_rate", FRACTION, "passed_reps / graded_reps.", True),
    Metric("cases", COUNT, "Distinct case names among the joined builds' tasks."),
    Metric("flaky_cases", COUNT,
           "Cases with at least one joined build whose graded repetitions include both a pass and a fail.", True),
)
METRIC_NAMES = tuple(m.name for m in METRICS)
METRIC_UNITS = {m.name: m.unit for m in METRICS}
HEADLINE_METRICS = tuple(m.name for m in METRICS if m.headline)

# Per case and job kind, over the joined builds.
CASE_METRICS: Tuple[Metric, ...] = (
    Metric("case_builds", COUNT, "Joined builds that ran the case."),
    Metric("case_graded_reps", COUNT, "Its repetitions graded pass or fail."),
    Metric("case_passed_reps", COUNT, "Its repetitions graded pass."),
    Metric("case_infra_reps", COUNT, "Its repetitions lost to infrastructure."),
    Metric("case_pass_rate", FRACTION, "case_passed_reps / case_graded_reps."),
    Metric("case_flaky_builds", COUNT, "Builds whose graded repetitions of the case include a pass and a fail."),
    Metric("case_agent_s_p50", SECONDS, "Median agent_s over its repetitions that carry it."),
)
CASE_METRIC_NAMES = tuple(m.name for m in CASE_METRICS)


@dataclass
class Line:
    """One request line, reduced to what the report counts."""

    kind: str
    build: str
    project: str
    pr: str
    ts: float
    status: Optional[int]
    latency_ms: Optional[int]
    prompt_tokens: Optional[int]
    cached_tokens: Optional[int]
    completion_tokens: Optional[int]
    tokens_estimated: bool
    model_group: Optional[str]
    model: Optional[str]
    fallback_attempted: bool
    upstream_incomplete: bool
    error: Optional[str]
    # Fields a line written before they existed lacks: None.
    reasoning_tokens: Optional[int] = None
    cache_write_tokens: Optional[int] = None
    model_name: Optional[str] = None
    attempted_retries: Optional[int] = None
    litellm_cost_usd: Optional[float] = None

    @property
    def key(self) -> Tuple[str, str]:
        return (self.kind, self.build)

    @property
    def ok(self) -> bool:
        return self.status == HTTP_OK


class Manifest(NamedTuple):
    """A job manifest: which build ran with the proxy, and when its collector started (from the file name)."""

    kind: str
    build: str
    started: Optional[float]


@dataclass
class Rep:
    case: str
    result: str
    agent_s: Optional[float]
    tokens_total: Optional[int]
    tokens_partial: bool = False


@dataclass
class EvalRun:
    build_id: str
    tier: str
    reps: List[Rep] = field(default_factory=list)
    pr: Optional[str] = None

    def matches_kind(self, kind: str) -> bool:
        """Whether the dashboard's tier for this run agrees with the proxy's job kind: a presubmit is a
        presubmit run or a forge-lane run with a pull request; a nightly (trusted or not) is a nightly run
        or a forge-lane run without one."""
        forge = self.tier in EVAL_FORGE_TIERS
        if kind == PRESUBMIT:
            return self.tier == PRESUBMIT or (forge and bool(self.pr))
        return self.tier == EVAL_TIER_NIGHTLY or (forge and not self.pr)


@dataclass
class Quality:
    """What the report could not count, and why."""

    files: int = 0
    # Files found under a directory argument but not at the bucket's layout.
    off_layout_files: int = 0
    manifests: int = 0
    bad_manifests: int = 0
    manifest_kind_mismatches: int = 0
    skipped: Dict[str, int] = field(default_factory=dict)
    # A field present with a value of the wrong type or sign, by field name.
    bad_values: Dict[str, int] = field(default_factory=dict)
    analysis_errors: int = 0
    analysis_dropped: int = 0
    eval_runs_read: int = 0
    eval_duplicate_runs: int = 0
    eval_unknown_results: int = 0
    eval_tier_mismatches: int = 0

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1

    def bad(self, name: str) -> None:
        self.bad_values[name] = self.bad_values.get(name, 0) + 1


class Prices:
    """The dated price table: per model, USD per 1M input, cached input and output tokens."""

    def __init__(self, table: Dict[str, Any], digest: str, price_as: Dict[str, str]):
        models = table.get(PRICES_MODELS)
        if not isinstance(table.get(PRICES_AS_OF), str) or not isinstance(models, dict):
            raise ValueError("the price table needs an `as_of` date and a `models` object")
        for name, entry in models.items():
            tiers = [entry] + ([entry[LONG_CONTEXT]] if isinstance(entry, dict) and LONG_CONTEXT in entry else [])
            for tier in tiers:
                if not isinstance(tier, dict) or not all(_is_price(tier.get(p)) for p in PRICE_FIELDS):
                    raise ValueError(f"price table entry {name!r} needs numeric {', '.join(PRICE_FIELDS)}")
            if len(tiers) > 1 and _as_count(tiers[1].get(LONG_CONTEXT_THRESHOLD)) is None:
                raise ValueError(f"price table entry {name!r}: {LONG_CONTEXT} needs {LONG_CONTEXT_THRESHOLD}")
        unknown = sorted(target for target in price_as.values() if target not in models)
        if unknown:
            raise ValueError(f"--price-as names models the price table does not have: {', '.join(unknown)}")
        self.as_of: str = table[PRICES_AS_OF]
        self.source: str = str(table.get(PRICES_SOURCE, ""))
        confirmed = table.get(PRICES_CONFIRMED, False)
        if not isinstance(confirmed, bool):
            raise ValueError(f"the price table's `{PRICES_CONFIRMED}` must be true or false")
        self.confirmed: bool = confirmed
        self.models: Dict[str, Dict[str, Any]] = models
        self.digest = digest
        self.price_as = dict(price_as)

    @classmethod
    def load(cls, path: str, price_as: Dict[str, str]) -> "Prices":
        with open(path, "rb") as handle:
            raw = handle.read()
        return cls(json.loads(raw), hashlib.sha256(raw).hexdigest(), price_as)

    def model_for(self, line: Line) -> Tuple[Optional[str], str]:
        """(the table's model, or None when unpriced; the label the line is reported under).

        The deployment's model (litellm_model_name, with and then without its
        provider prefix) is looked up before the alias and the request's model;
        --price-as is consulted only when none of them is in the table.
        """
        names = [name for name in (line.model_name, strip_provider(line.model_name), line.model_group, line.model)
                 if name]
        for name in names:
            if name in self.models:
                return name, name
        for name in names:
            if name in self.price_as:
                return self.price_as[name], name
        return None, names[1 if line.model_name else 0] if names else UNPRICED_NO_MODEL

    def cost(self, model: str, line: Line) -> float:
        entry = self.models[model]
        prompt = line.prompt_tokens or 0
        long_context = entry.get(LONG_CONTEXT)
        if long_context and prompt > long_context[LONG_CONTEXT_THRESHOLD]:
            entry = long_context
        cached = min(line.cached_tokens or 0, prompt)
        return (
            (prompt - cached) * entry["input"] + cached * entry["cached_input"]
            + (line.completion_tokens or 0) * entry["output"]
        ) / TOKENS_PER_PRICE_UNIT

    def describe(self) -> Dict[str, Any]:
        return {PRICES_AS_OF: self.as_of, PRICES_CONFIRMED: self.confirmed, PRICES_SOURCE: self.source,
                PRICES_SHA256: self.digest, PRICES_MODELS: sorted(self.models),
                PRICES_PRICE_AS: dict(sorted(self.price_as.items()))}


def strip_provider(model_name: Optional[str]) -> Optional[str]:
    """`vertex_ai/gemini-x` -> `gemini-x`: LiteLLM's model name without its provider prefix."""
    if not model_name or PROVIDER_SEPARATOR not in model_name:
        return model_name
    return model_name.split(PROVIDER_SEPARATOR, 1)[1]


def _is_price(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _as_count(value: Any, maximum: int = MAX_COUNT) -> Optional[int]:
    """A whole number from zero to maximum, else None (a bool is not a count)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        return None
    return value


def _as_seconds(value: Any, maximum: Optional[float] = None) -> Optional[float]:
    """A finite number from zero to maximum (no upper bound when None), else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    if maximum is not None and value > maximum:
        return None
    return float(value)


def _field(entry: Dict[str, Any], name: str, quality: Quality, maximum: int = MAX_COUNT) -> Optional[int]:
    """A count field: None when absent or null, and counted as bad when present with another type or out of
    range."""
    value = entry.get(name)
    if value is None:
        return None
    count = _as_count(value, maximum)
    if count is None:
        quality.bad(name)
    return count


def expand_paths(paths: Iterable[str]) -> Tuple[List[str], List[str]]:
    """(log files, job manifests). A directory is searched for segments and manifests; a file is a log
    unless it is named like a manifest."""
    logs: List[str] = []
    manifests: List[str] = []
    for path in paths:
        if os.path.isdir(path):
            for root, dirs, names in os.walk(path):
                dirs.sort()
                for name in sorted(names):
                    full = os.path.join(root, name)
                    if MANIFEST_NAME.match(name):
                        manifests.append(full)
                    elif name.endswith(SEGMENT_SUFFIXES):
                        logs.append(full)
        elif MANIFEST_NAME.match(os.path.basename(path)):
            manifests.append(path)
        else:
            logs.append(path)
    return logs, manifests


def parse_line(entry: Dict[str, Any], quality: Quality, kind: str) -> Optional[Line]:
    """`kind` is the folder's (or --kind); a line whose own tag disagrees is dropped and counted."""
    job = entry.get(F.JOB)
    if not isinstance(job, dict) or job.get(F.JOB_KIND) not in JOB_KINDS:
        quality.skip(SKIP_UNTAGGED)
        return None
    if job[F.JOB_KIND] != kind:
        quality.skip(SKIP_KIND_MISMATCH)
        return None
    ts = _as_seconds(entry.get(F.TS), MAX_EPOCH_SECONDS)
    if ts is None:
        # A line without a usable time cannot be windowed or ordered: skipped, and
        # its value counted bad when it had one.
        if entry.get(F.TS) is not None:
            quality.bad(F.TS)
        quality.skip(SKIP_BAD_TS)
        return None
    error = entry.get(F.ERROR)
    if error == F.ERROR_ANALYSIS_DROPPED:
        quality.analysis_dropped += 1
    elif error:
        quality.analysis_errors += 1
    fallbacks = entry.get(F.LITELLM_ATTEMPTED_FALLBACKS)
    group = entry.get(F.LITELLM_MODEL_GROUP)
    model = entry.get(F.MODEL)
    model_name = entry.get(F.LITELLM_MODEL_NAME)
    cost = entry.get(F.LITELLM_COST_USD)
    litellm_cost_usd = _as_seconds(cost, MAX_REQUEST_COST_USD)
    if cost is not None and litellm_cost_usd is None:
        quality.bad(F.LITELLM_COST_USD)
    return Line(
        kind=kind,
        build=str(job.get(F.JOB_BUILD) or ""),
        project=str(job.get(F.JOB_PROJECT) or ""),
        pr=str(job.get(F.JOB_PR) or ""),
        ts=ts,
        status=_field(entry, F.STATUS, quality),
        latency_ms=_field(entry, F.LATENCY_MS, quality, MAX_LATENCY_MS),
        prompt_tokens=_field(entry, F.PROMPT_TOKENS, quality),
        cached_tokens=_field(entry, F.CACHED_TOKENS, quality),
        completion_tokens=_field(entry, F.COMPLETION_TOKENS, quality),
        tokens_estimated=entry.get(F.TOKENS_ESTIMATED) is True,
        model_group=group if isinstance(group, str) and group else None,
        model=model if isinstance(model, str) and model else None,
        fallback_attempted=fallbacks is not None and str(fallbacks) != NO_FALLBACKS,
        upstream_incomplete=entry.get(F.UPSTREAM_INCOMPLETE) is True,
        error=str(error) if error else None,
        reasoning_tokens=_field(entry, F.REASONING_TOKENS, quality),
        cache_write_tokens=_field(entry, F.CACHE_WRITE_TOKENS, quality),
        model_name=model_name if isinstance(model_name, str) and model_name else None,
        attempted_retries=_field(entry, F.LITELLM_ATTEMPTED_RETRIES, quality),
        litellm_cost_usd=litellm_cost_usd,
    )


def filed_kind(path: str) -> Optional[str]:
    """The kind a segment or job manifest was filed under, when it sits at the collector's layout, else None.

    The kind directory must be the only component of the whole absolute path
    named for a kind, since everything below a kind prefix is the uploader's
    choice; the build directory may not be named for a kind or the bucket root.

    Mirrors analyze_shadow.filed_kind (kube-agents#4982), copied with its
    constants; replace it with that import once both are on main.
    """
    parts = os.path.normpath(os.path.abspath(path)).split(os.sep)
    name = parts[-1]
    if SEGMENT_NAME.match(name):
        depth = FILED_DEPTH
        if len(parts) < depth or parts[-2] != LOGS_DIR:
            return None
    elif MANIFEST_NAME.match(name):
        depth = MANIFEST_DEPTH
        if len(parts) < depth:
            return None
    else:
        return None
    kind, day, build = parts[-depth:][:3]
    if kind not in JOB_KINDS or not DAY_DIR.match(day) or not BUILD_DIR.match(build) or build in (*JOB_KINDS, BUCKET_ROOT):
        return None
    if any(part in JOB_KINDS for part in parts[:-depth]):
        return None
    return kind


def filed_inputs(
    paths: Iterable[str], kind: Optional[str], quality: Quality
) -> Tuple[List[Tuple[str, str]], List[Tuple[str, str]]]:
    """(log files, job manifests), each with its job kind: `kind` when given, else filed_kind's, never the
    lines' own tags, which the proxy built from the commit under test writes.

    A file found under a directory but off the layout is skipped and counted
    in off_layout_files. Raises ValueError for a path that does not exist, a
    file named outright that is off the layout, or a directory whose log files
    and manifests are all off it: each a mistake in the arguments rather than a
    planted file.
    """
    logs: List[Tuple[str, str]] = []
    manifests: List[Tuple[str, str]] = []
    for path in paths:
        if not os.path.exists(path):
            raise ValueError(f"{path} does not exist")
        if not os.path.isdir(path):
            file_kind = kind or filed_kind(path)
            if file_kind is None:
                raise ValueError(f"{path} is not at the bucket's layout ({LAYOUT}, with no other directory in its"
                                 " path named for a kind); pass --kind to name its kind")
            (manifests if MANIFEST_NAME.match(os.path.basename(path)) else logs).append((path, file_kind))
            continue
        found_logs, found_manifests = expand_paths([path])
        found = 0
        for names, out in ((found_logs, logs), (found_manifests, manifests)):
            for name in names:
                file_kind = kind or filed_kind(name)
                if file_kind is None:
                    quality.off_layout_files += 1
                    continue
                out.append((name, file_kind))
                found += 1
        if (found_logs or found_manifests) and not found:
            raise ValueError(f"{path} holds no log file or manifest at the bucket's layout ({LAYOUT}, with no other"
                             " directory in its path named for a kind); pass a kind folder or its parent, or --kind")
    return logs, manifests


def load_lines(
    paths: Iterable[str], quality: Quality, kind: Optional[str] = None
) -> Tuple[List[Line], List[Manifest]]:
    """The request lines in time order, and each readable manifest, each of the kind filed_inputs() gives."""
    log_kinds, manifest_kinds = filed_inputs(paths, kind, quality)
    quality.files = len(log_kinds)
    quality.manifests = len(manifest_kinds)
    lines: List[Line] = []
    seen: set = set()
    for path, file_kind in log_kinds:
        for line in _file_lines(path, file_kind, quality, seen):
            lines.append(line)
    lines.sort(key=lambda l: l.ts)
    manifests: List[Manifest] = []
    for path, file_kind in manifest_kinds:
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError):
            quality.bad_manifests += 1
            continue
        if not isinstance(doc, dict) or doc.get(F.JOB_KIND) not in JOB_KINDS:
            quality.bad_manifests += 1
            continue
        if doc[F.JOB_KIND] != file_kind:
            quality.manifest_kind_mismatches += 1
            continue
        name = MANIFEST_NAME.match(os.path.basename(path))
        started = int(name.group(1)) / MS_PER_SECOND if name else None
        manifests.append(Manifest(file_kind, str(doc.get(F.JOB_BUILD) or ""), started))
    return lines, manifests


def _file_lines(path: str, kind: str, quality: Quality, seen: Set[str]) -> Iterator[Line]:
    """One log file's request lines, deduplicated by request_id across files."""
    for entry in analyze_shadow.iter_request_lines([path], quality.skipped):
        request_id = entry.get(F.REQUEST_ID)
        if isinstance(request_id, str) and request_id:
            # The bucket holds each segment once (the collector uploads an open
            # segment under its closed name, without overwriting), but local
            # copies can overlap. Error and dropped lines carry no request_id
            # and are never deduplicated.
            if request_id in seen:
                quality.skip(SKIP_DUPLICATE)
                continue
            seen.add(request_id)
        line = parse_line(entry, quality, kind)
        if line is not None:
            yield line


def parse_time(value: str, end_of_day: bool = False) -> float:
    """Epoch seconds from epoch seconds, an ISO date (UTC midnight; the next midnight with end_of_day)
    or an ISO datetime (UTC unless it names an offset)."""
    if EPOCH_SECONDS.match(value):
        if len(value.split(DECIMAL_POINT, 1)[0]) < EPOCH_MIN_DIGITS:
            raise ValueError(f"{value!r}: write a date as YYYY-MM-DD; epoch seconds need at least"
                             f" {EPOCH_MIN_DIGITS} digits")
        seconds = float(value)
        if not math.isfinite(seconds) or seconds > MAX_EPOCH_SECONDS:
            raise ValueError(f"{value!r}: epoch seconds must be finite and at most {MAX_EPOCH_SECONDS}"
                             " (the end of year 9999)")
        return seconds
    if not ISO_START.match(value):
        raise ValueError(f"{value!r}: expected epoch seconds, YYYY-MM-DD or an ISO 8601 time")
    if DATE_ONLY.match(value):
        day = datetime.datetime.strptime(value, DATE_FORMAT).replace(tzinfo=datetime.timezone.utc)
        if end_of_day:
            day += datetime.timedelta(days=1)
        return day.timestamp()
    moment = datetime.datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.timestamp()


def _outside(ts: float, since: Optional[float], until: Optional[float]) -> bool:
    return (since is not None and ts < since) or (until is not None and ts >= until)


def in_window(
    lines: List[Line], since: Optional[float], until: Optional[float], quality: Quality
) -> Tuple[List[Line], Set[Tuple[str, str]]]:
    """Keep whole builds whose first request falls in [since, until), so no build is cut in two, and lines
    without a build id by their own time. Returns the kept lines and the (kind, build) keys left out."""
    first: Dict[Tuple[str, str], float] = {}
    for line in lines:
        first.setdefault(line.key, line.ts)
    kept = []
    excluded: Set[Tuple[str, str]] = set()
    for line in lines:
        start = first[line.key] if line.build else line.ts
        if _outside(start, since, until):
            quality.skip(SKIP_OUTSIDE_WINDOW)
            if line.build:
                excluded.add(line.key)
            continue
        kept.append(line)
    return kept, excluded


def manifests_in_window(
    manifests: List[Manifest], lines: List[Line], excluded: Set[Tuple[str, str]],
    since: Optional[float], until: Optional[float],
) -> List[Manifest]:
    """Drop the manifests of builds the window left out, and of builds with no lines whose collector started
    outside it."""
    logged = {line.key for line in lines}
    return [
        manifest for manifest in manifests
        if (manifest.kind, manifest.build) not in excluded
        and ((manifest.kind, manifest.build) in logged or manifest.started is None
             or not _outside(manifest.started, since, until))
    ]


def load_eval(paths: Iterable[str], quality: Quality) -> Dict[str, EvalRun]:
    """The dashboard data.json runs by build id; the first file naming a build wins."""
    runs: Dict[str, EvalRun] = {}
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
        raw_runs = doc.get(EVAL_RUNS) if isinstance(doc, dict) else None
        if not isinstance(raw_runs, list):
            raise ValueError(f"{path}: not a dashboard data.json (no `runs` list)")
        for raw in raw_runs:
            if not isinstance(raw, dict) or not raw.get(EVAL_BUILD_ID):
                continue
            build_id = str(raw[EVAL_BUILD_ID])
            quality.eval_runs_read += 1
            if build_id in runs:
                quality.eval_duplicate_runs += 1
                continue
            pr = raw.get(EVAL_PR)
            run = EvalRun(build_id, str(raw.get(EVAL_TIER) or EVAL_TIER_DEFAULT),
                          pr=str(pr) if pr not in (None, "") else None)
            for task in raw.get(EVAL_TASKS) or []:
                if not isinstance(task, dict) or not task.get(EVAL_TASK_NAME):
                    continue
                reps = task.get(EVAL_REPS)
                has_reps = isinstance(reps, list)
                # A build graded before repetitions were logged counts its
                # task verdict as one repetition, with no timing.
                for rep in reps if has_reps else [task]:
                    if not isinstance(rep, dict):
                        continue
                    result = rep.get(EVAL_RESULT)
                    if result not in EVAL_RESULTS:
                        quality.eval_unknown_results += 1
                        continue
                    tokens = rep.get(EVAL_TOKENS) if isinstance(rep.get(EVAL_TOKENS), dict) else {}
                    agent_s = _as_seconds(rep.get(EVAL_AGENT_S)) if has_reps else None
                    run.reps.append(Rep(str(task[EVAL_TASK_NAME]), result, agent_s,
                                        _as_count(tokens.get(EVAL_TOKENS_TOTAL)),
                                        tokens.get(EVAL_TOKENS_PARTIAL) is True))
            runs[build_id] = run
    return runs


def percentile(values: Sequence[float], pct: int) -> Optional[float]:
    """Nearest rank: the smallest value with at least pct% of values at or below it."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(math.ceil(pct / PERCENT * len(ordered)) - 1, 0)]


def ratio(part: float, whole: float, decimals: int = RATE_DECIMALS) -> Optional[float]:
    return round(part / whole, decimals) if whole else None


def aggregate(lines: List[Line], runs: Dict[str, EvalRun], prices: Prices) -> Dict[str, Any]:
    """Every metric in METRICS for one scope, plus status_counts and unpriced (by model label)."""
    # Lines without a build id count in every sum but are no build.
    builds = {line.key for line in lines if line.build}
    built = [line for line in lines if line.build]
    ok = [line for line in lines if line.ok]
    prompt = sum(line.prompt_tokens or 0 for line in ok)
    cached = sum(min(line.cached_tokens or 0, line.prompt_tokens or 0) for line in ok)
    measured = [line for line in ok if not line.tokens_estimated and line.prompt_tokens is not None]
    measured_prompt = sum(line.prompt_tokens or 0 for line in measured)
    measured_cached = sum(min(line.cached_tokens or 0, line.prompt_tokens or 0) for line in measured)
    cost = built_cost = litellm_cost = table_cost = both_litellm = both_table = 0.0
    litellm_costed = table_priced = from_table = estimated_unpriced = 0
    unpriced: Dict[str, int] = {}
    for line in ok:
        model, label = prices.model_for(line)
        # Estimated tokens are the request's size and no completion count: never priced.
        estimate = prices.cost(model, line) if model is not None and not line.tokens_estimated else None
        if estimate is not None:
            table_cost += estimate
            table_priced += 1
        line_cost = line.litellm_cost_usd if line.litellm_cost_usd is not None else estimate
        if line_cost is not None and line.build:
            built_cost += line_cost
        if line.litellm_cost_usd is not None:
            cost += line.litellm_cost_usd
            litellm_cost += line.litellm_cost_usd
            litellm_costed += 1
            if estimate is not None:
                both_litellm += line.litellm_cost_usd
                both_table += estimate
        elif estimate is not None:
            cost += estimate
            from_table += 1
        elif line.tokens_estimated:
            estimated_unpriced += 1
        else:
            unpriced[label] = unpriced.get(label, 0) + 1
    with_reasoning = [line for line in ok if line.reasoning_tokens is not None]
    latencies = [line.latency_ms for line in lines if line.latency_ms is not None]
    model_s = sum(latencies) / MS_PER_SECOND
    # Per-build figures divide what lines with a build id add up to by the builds.
    built_prompt = sum(line.prompt_tokens or 0 for line in built if line.ok)
    built_model_s = sum(line.latency_ms or 0 for line in built) / MS_PER_SECOND
    statuses: Dict[str, int] = {}
    for line in lines:
        if not line.ok:
            label = str(line.status) if line.status is not None else STATUS_NONE
            statuses[label] = statuses.get(label, 0) + 1

    joined = sorted({build for _, build in builds if build in runs})
    reps = [rep for build in joined for rep in runs[build].reps]
    timed_builds = {build for build in joined if any(rep.agent_s is not None for rep in runs[build].reps)}
    agent_s = sum(rep.agent_s for rep in reps if rep.agent_s is not None)
    timed_model_s = sum(
        line.latency_ms for line in lines if line.build in timed_builds and line.latency_ms is not None
    ) / MS_PER_SECOND
    graded = [rep for rep in reps if rep.result in (EVAL_PASS, EVAL_FAIL)]
    passed = sum(1 for rep in graded if rep.result == EVAL_PASS)
    flaky = set()
    for build in joined:
        by_case: Dict[str, set] = {}
        for rep in runs[build].reps:
            if rep.result in (EVAL_PASS, EVAL_FAIL):
                by_case.setdefault(rep.case, set()).add(rep.result)
        flaky.update(case for case, results in by_case.items() if len(results) > 1)
    has_eval = bool(joined)
    harness_tokens = [rep.tokens_total for rep in reps if rep.tokens_total is not None and not rep.tokens_partial]

    values: Dict[str, Any] = {
        "builds": len(builds),
        "requests": len(lines),
        "requests_per_build": ratio(len(built), len(builds), PER_BUILD_DECIMALS),
        "ok_requests": len(ok),
        "prompt_tokens": prompt,
        "cached_prompt_tokens": cached,
        "uncached_prompt_tokens": prompt - cached,
        "completion_tokens": sum(line.completion_tokens or 0 for line in ok),
        "reasoning_tokens": sum(line.reasoning_tokens or 0 for line in ok),
        "reasoning_token_share": ratio(sum(line.reasoning_tokens or 0 for line in with_reasoning),
                                       sum(line.completion_tokens or 0 for line in with_reasoning)),
        "cache_write_tokens": sum(line.cache_write_tokens or 0 for line in ok),
        "prompt_tokens_per_build": ratio(built_prompt, len(builds), PER_BUILD_DECIMALS),
        "cache_hit_rate": ratio(measured_cached, measured_prompt),
        "estimated_prompt_token_share": ratio(sum(line.prompt_tokens or 0 for line in ok if line.tokens_estimated),
                                              prompt),
        "cost_usd": round(cost, USD_DECIMALS),
        "cost_per_build_usd": ratio(built_cost, len(builds), USD_DECIMALS),
        "litellm_cost_usd": round(litellm_cost, USD_DECIMALS),
        "litellm_costed_requests": litellm_costed,
        "table_cost_usd": round(table_cost, USD_DECIMALS),
        "table_priced_requests": table_priced,
        "cost_from_table_requests": from_table,
        "table_to_litellm_cost_ratio": ratio(both_table, both_litellm),
        "unpriced_requests": sum(unpriced.values()),
        "estimated_token_requests": sum(1 for line in ok if line.tokens_estimated),
        "estimated_unpriced_requests": estimated_unpriced,
        "model_time_s": round(model_s, SECONDS_DECIMALS),
        "model_time_per_build_s": ratio(built_model_s, len(builds), PER_BUILD_DECIMALS),
        "latency_p50_ms": percentile(latencies, P50),
        "latency_p95_ms": percentile(latencies, P95),
        "non_200_requests": sum(statuses.values()),
        "rate_limited_requests": sum(1 for line in lines if line.status == HTTP_TOO_MANY_REQUESTS),
        "rate_limited_rate": None,
        "retried_requests": sum(1 for line in lines if (line.attempted_retries or 0) > 0),
        "litellm_retries": sum(line.attempted_retries or 0 for line in lines),
        "fallback_attempted_requests": sum(1 for line in lines if line.fallback_attempted),
        "upstream_incomplete_requests": sum(1 for line in lines if line.upstream_incomplete),
        "eval_builds_joined": len(joined),
        "agent_time_s": round(agent_s, SECONDS_DECIMALS) if timed_builds else None,
        "model_time_share_of_agent_time": ratio(timed_model_s, agent_s) if timed_builds else None,
        "harness_tokens_total": sum(harness_tokens) if harness_tokens else None,
        "harness_partial_reps": sum(1 for rep in reps if rep.tokens_partial) if has_eval else None,
        "graded_reps": len(graded) if has_eval else None,
        "passed_reps": passed if has_eval else None,
        "infra_reps": sum(1 for rep in reps if rep.result == EVAL_INFRA) if has_eval else None,
        "pass_rate": ratio(passed, len(graded)) if has_eval else None,
        REPORT_CASES: len({rep.case for rep in reps}) if has_eval else None,
        "flaky_cases": len(flaky) if has_eval else None,
    }
    values["rate_limited_rate"] = ratio(values["rate_limited_requests"], len(lines))
    assert tuple(values) == METRIC_NAMES, "aggregate() and METRICS disagree"
    values[STATUS_COUNTS] = dict(sorted(statuses.items()))
    values[UNPRICED_BY_MODEL] = dict(sorted(unpriced.items()))
    return values


def case_table(builds: Iterable[str], runs: Dict[str, EvalRun]) -> Dict[str, Dict[str, Any]]:
    """CASE_METRICS per case over the given joined builds."""
    out: Dict[str, Dict[str, Any]] = {}
    per_case: Dict[str, List[Tuple[str, Rep]]] = {}
    for build in sorted(builds):
        for rep in runs[build].reps:
            per_case.setdefault(rep.case, []).append((build, rep))
    for case, items in sorted(per_case.items()):
        graded = [rep for _, rep in items if rep.result in (EVAL_PASS, EVAL_FAIL)]
        passed = sum(1 for rep in graded if rep.result == EVAL_PASS)
        results_by_build: Dict[str, set] = {}
        for build, rep in items:
            if rep.result in (EVAL_PASS, EVAL_FAIL):
                results_by_build.setdefault(build, set()).add(rep.result)
        values = {
            "case_builds": len({build for build, _ in items}),
            "case_graded_reps": len(graded),
            "case_passed_reps": passed,
            "case_infra_reps": sum(1 for _, rep in items if rep.result == EVAL_INFRA),
            "case_pass_rate": ratio(passed, len(graded)),
            "case_flaky_builds": sum(1 for results in results_by_build.values() if len(results) > 1),
            "case_agent_s_p50": percentile([rep.agent_s for _, rep in items if rep.agent_s is not None], P50),
        }
        assert tuple(values) == CASE_METRIC_NAMES, "case_table() and CASE_METRICS disagree"
        out[case] = values
    return out


def build_report(
    lines: List[Line], manifests: List[Manifest], runs: Dict[str, EvalRun], prices: Prices,
    quality: Quality, since: Optional[float] = None, until: Optional[float] = None,
) -> Dict[str, Any]:
    by_build: Dict[Tuple[str, str], List[Line]] = {}
    for line in lines:
        by_build.setdefault(line.key, []).append(line)
    for (kind, build), build_lines in by_build.items():
        run = runs.get(build)
        if run is None:
            continue
        if not run.matches_kind(kind):
            quality.eval_tier_mismatches += 1
    builds_out = []
    for (kind, build), build_lines in sorted(by_build.items(), key=lambda kv: kv[1][0].ts):
        first = build_lines[0]
        builds_out.append({
            "kind": kind, "build": build or NO_BUILD, "project": first.project, "pr": first.pr,
            WINDOW_FIRST_TS: first.ts, WINDOW_LAST_TS: build_lines[-1].ts,
            BUILD_METRICS: aggregate(build_lines, runs, prices),
        })
    scopes = {TOTAL_SCOPE: aggregate(lines, runs, prices)}
    cases: Dict[str, Dict[str, Any]] = {}
    for kind in JOB_KINDS:
        kind_lines = [line for line in lines if line.kind == kind]
        scopes[kind] = aggregate(kind_lines, runs, prices)
        cases[kind] = case_table({line.build for line in kind_lines if line.build in runs}, runs)
    logged = set(by_build)
    ok_lines = [line for line in lines if line.ok]
    estimated = [line for line in ok_lines if line.tokens_estimated]
    return {
        REPORT_SCHEMA: REPORT_SCHEMA_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec=ISO_TIMESPEC),
        REPORT_WINDOW: {
            WINDOW_SINCE: since, WINDOW_UNTIL: until,
            WINDOW_FIRST_TS: lines[0].ts if lines else None,
            WINDOW_LAST_TS: lines[-1].ts if lines else None,
        },
        REPORT_PRICES: prices.describe(),
        REPORT_SCOPES: scopes,
        REPORT_BUILDS: builds_out,
        REPORT_CASES: cases,
        REPORT_DATA_QUALITY: {
            "log_files": quality.files,
            "off_layout_files": quality.off_layout_files,
            "manifests": quality.manifests,
            "bad_manifests": quality.bad_manifests,
            "manifest_kind_mismatches": quality.manifest_kind_mismatches,
            "manifest_builds_without_lines": sorted(
                f"{kind}/{build}" for kind, build in {(m.kind, m.build) for m in manifests} - logged
            ),
            "skipped_lines": dict(sorted(quality.skipped.items())),
            "bad_field_values": dict(sorted(quality.bad_values.items())),
            "analysis_error_lines": quality.analysis_errors,
            "analysis_dropped_lines": quality.analysis_dropped,
            "lines_without_build": sum(1 for line in lines if not line.build),
            "estimated_token_lines": len(estimated),
            "lines_without_latency": sum(1 for line in lines if line.latency_ms is None),
            "eval_runs_read": quality.eval_runs_read,
            "eval_duplicate_runs": quality.eval_duplicate_runs,
            "eval_unknown_results": quality.eval_unknown_results,
            "eval_tier_mismatches": quality.eval_tier_mismatches,
            "builds_without_eval_run": sum(1 for _, build in logged if build and build not in runs) if runs else None,
        },
    }


def _fmt(name: str, value: Any) -> str:
    if value is None:
        return "-"
    unit = METRIC_UNITS.get(name)
    if unit == FRACTION:
        return f"{value * PERCENT:.{DELTA_DECIMALS}f}%"
    if unit == USD:
        return f"${value:,.{USD_DISPLAY_DECIMALS}f}"
    if isinstance(value, float):
        return f"{value:,.1f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _iso(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(timespec=ISO_TIMESPEC)


def _prices_label(prices: Dict[str, Any]) -> str:
    """as_of, whether the prices were confirmed on it, and their source (absent in older reports)."""
    confirmed = PRICES_CONFIRMED_LABEL if prices.get(PRICES_CONFIRMED) else PRICES_UNCONFIRMED_LABEL
    return f"as of {prices.get(PRICES_AS_OF)} ({confirmed}; source: {prices.get(PRICES_SOURCE) or '-'})"


def _prices_summary(prices: Dict[str, Any]) -> Dict[str, Any]:
    return {key: prices.get(key) for key in (PRICES_AS_OF, PRICES_CONFIRMED, PRICES_SOURCE)}


def render(report: Dict[str, Any]) -> str:
    window = report[REPORT_WINDOW]
    prices = report[REPORT_PRICES]
    lines = [
        f"window: {_iso(window[WINDOW_FIRST_TS])} to {_iso(window[WINDOW_LAST_TS])}"
        f" (filter since {_iso(window[WINDOW_SINCE])}, until {_iso(window[WINDOW_UNTIL])})",
        f"prices: {_prices_label(prices)}, sha256 {prices[PRICES_SHA256][:HASH_DISPLAY_CHARS]}, models {', '.join(prices['models'])};"
        f" price-as {prices[PRICES_PRICE_AS] or 'none'}",
        "",
        f"{'metric':<{METRIC_COLUMN_WIDTH}}" + "".join(f"{scope:>{SCOPE_COLUMN_WIDTH}}" for scope in SCOPES),
    ]
    for name in METRIC_NAMES:
        lines.append(f"{name:<{METRIC_COLUMN_WIDTH}}" + "".join(f"{_fmt(name, report[REPORT_SCOPES][s][name]):>{SCOPE_COLUMN_WIDTH}}" for s in SCOPES))
    for scope in SCOPES:
        values = report[REPORT_SCOPES][scope]
        if values[STATUS_COUNTS] or values[UNPRICED_BY_MODEL]:
            lines.append(f"{scope}: non-200 statuses {values[STATUS_COUNTS]};"
                         f" unpriced by model {values[UNPRICED_BY_MODEL]}")
    lines += ["", "builds (kind build project pr: requests, prompt tokens, cache hit, cost, model time, 429s, pass rate):"]
    for build in report[REPORT_BUILDS]:
        m = build[BUILD_METRICS]
        lines.append(
            f"  {build['kind']:<{KIND_COLUMN_WIDTH}} {build['build']} {build['project'] or '-'} {build['pr'] or '-'}:"
            f" {m['requests']}, {_fmt('prompt_tokens', m['prompt_tokens'])},"
            f" {_fmt('cache_hit_rate', m['cache_hit_rate'])}, {_fmt('cost_usd', m['cost_usd'])},"
            f" {_fmt('model_time_s', m['model_time_s'])} s, {m['rate_limited_requests']},"
            f" {_fmt('pass_rate', m['pass_rate'])}"
        )
    for kind in JOB_KINDS:
        if report[REPORT_CASES].get(kind):
            lines += ["", f"{kind} cases (builds, graded reps, pass rate, flaky builds, infra reps, agent_s p50):"]
            for case, c in report[REPORT_CASES][kind].items():
                lines.append(
                    f"  {case}: {c['case_builds']}, {c['case_graded_reps']},"
                    f" {_fmt('pass_rate', c['case_pass_rate'])}, {c['case_flaky_builds']}, {c['case_infra_reps']},"
                    f" {_fmt('agent_time_s', c['case_agent_s_p50'])}"
                )
    lines += ["", "data quality:"]
    lines += [f"  {name}: {value}" for name, value in report[REPORT_DATA_QUALITY].items()]
    return "\n".join(lines)


def cost_source(values: Dict[str, Any]) -> Optional[str]:
    """Where a scope's cost_usd came from: LiteLLM, the price table, both, or nowhere (None)."""
    litellm = bool(values.get("litellm_costed_requests"))
    table = bool(values.get("cost_from_table_requests"))
    if litellm and table:
        return COST_SOURCE_MIXED
    if litellm:
        return COST_SOURCE_LITELLM
    return COST_SOURCE_TABLE if table else None


def compare(baseline: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Any]:
    """Headline deltas per scope: current - baseline, and that as a percent of the baseline."""
    notes = []
    if baseline.get(REPORT_SCHEMA) != current.get(REPORT_SCHEMA):
        notes.append(f"report schemas differ ({baseline.get(REPORT_SCHEMA)} vs {current.get(REPORT_SCHEMA)}):"
                     " metric definitions may have changed")
    if baseline[REPORT_PRICES][PRICES_SHA256] != current[REPORT_PRICES][PRICES_SHA256] or \
            baseline[REPORT_PRICES][PRICES_PRICE_AS] != current[REPORT_PRICES][PRICES_PRICE_AS]:
        notes.append("price tables or --price-as differ: cost deltas mix price changes with usage changes")
    for side, report in (("baseline", baseline), ("current", current)):
        for scope in SCOPES:
            values = report[REPORT_SCOPES][scope]
            if values.get("unpriced_requests"):
                notes.append(f"{side} {scope}: cost is partial ({values['unpriced_requests']} unpriced requests)")
            if values.get("estimated_token_requests"):
                notes.append(f"{side} {scope}: {values['estimated_token_requests']} requests have estimated tokens"
                             f" ({values.get('estimated_unpriced_requests') or 0} of them left out of cost_usd)")
            if cost_source(values) == COST_SOURCE_MIXED:
                notes.append(f"{side} {scope}: cost_usd takes LiteLLM's cost for {values['litellm_costed_requests']}"
                             f" requests and the price table for {values['cost_from_table_requests']}")
    for scope in SCOPES:
        sources = (cost_source(baseline[REPORT_SCOPES][scope]), cost_source(current[REPORT_SCOPES][scope]))
        if None not in sources and sources[0] != sources[1]:
            notes.append(f"{scope}: cost_usd comes from {sources[0]} in the baseline and {sources[1]} now")
    scopes: Dict[str, Dict[str, Any]] = {}
    for scope in SCOPES:
        rows = {}
        for name in HEADLINE_METRICS:
            base = baseline[REPORT_SCOPES][scope].get(name)
            new = current[REPORT_SCOPES][scope].get(name)
            delta = round(new - base, RATE_DECIMALS) if base is not None and new is not None else None
            pct = round(PERCENT * delta / base, DELTA_DECIMALS) if delta is not None and base else None
            rows[name] = {"baseline": base, "current": new, "delta": delta, "delta_percent": pct}
        scopes[scope] = rows
    return {
        COMPARE_BASELINE_WINDOW: baseline[REPORT_WINDOW], COMPARE_CURRENT_WINDOW: current[REPORT_WINDOW],
        COMPARE_BASELINE_PRICES: _prices_summary(baseline[REPORT_PRICES]),
        COMPARE_CURRENT_PRICES: _prices_summary(current[REPORT_PRICES]),
        COMPARE_NOTES: notes, REPORT_SCOPES: scopes,
    }


def render_compare(result: Dict[str, Any]) -> str:
    lines = [
        f"baseline: {_iso(result[COMPARE_BASELINE_WINDOW][WINDOW_FIRST_TS])} to"
        f" {_iso(result[COMPARE_BASELINE_WINDOW][WINDOW_LAST_TS])}, prices {_prices_label(result[COMPARE_BASELINE_PRICES])}",
        f"current:  {_iso(result[COMPARE_CURRENT_WINDOW][WINDOW_FIRST_TS])} to"
        f" {_iso(result[COMPARE_CURRENT_WINDOW][WINDOW_LAST_TS])}, prices {_prices_label(result[COMPARE_CURRENT_PRICES])}",
    ]
    lines += [f"note: {note}" for note in result[COMPARE_NOTES]]
    for scope, rows in result[REPORT_SCOPES].items():
        lines += ["", f"{scope}:", f"  {'metric':<{METRIC_COLUMN_WIDTH}}{'baseline':>{COMPARE_COLUMN_WIDTH}}{'current':>{COMPARE_COLUMN_WIDTH}}{'delta':>{COMPARE_COLUMN_WIDTH}}{'delta %':>{PERCENT_COLUMN_WIDTH}}"]
        for name, row in rows.items():
            delta = row["delta"]
            if delta is not None and METRIC_UNITS[name] == FRACTION:
                shown = f"{delta * PERCENT:+.{DELTA_DECIMALS}f} pp"
            elif delta is not None and METRIC_UNITS[name] == USD:
                shown = f"{delta:+,.{USD_DISPLAY_DECIMALS}f}"
            elif delta is not None:
                shown = f"{delta:+,.{DELTA_DECIMALS}f}" if isinstance(delta, float) else f"{delta:+,}"
            else:
                shown = "-"
            pct = f"{row['delta_percent']:+.{DELTA_DECIMALS}f}%" if row["delta_percent"] is not None else "-"
            lines.append(f"  {name:<{METRIC_COLUMN_WIDTH}}{_fmt(name, row['baseline']):>{COMPARE_COLUMN_WIDTH}}{_fmt(name, row['current']):>{COMPARE_COLUMN_WIDTH}}"
                         f"{shown:>{COMPARE_COLUMN_WIDTH}}{pct:>{PERCENT_COLUMN_WIDTH}}")
    return "\n".join(lines)


def parse_price_as(values: Sequence[str]) -> Dict[str, str]:
    out = {}
    for value in values:
        name, sep, model = value.partition(PRICE_AS_SEPARATOR)
        if not sep or not name or not model:
            raise ValueError(f"--price-as {value!r}: expected GROUP=MODEL")
        out[name] = model
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("logs", nargs="*", help="JSONL files or directories of them (and job-<ms>.json manifests)")
    parser.add_argument("--eval-data", action="append", default=[], help="the eval dashboard's data.json")
    parser.add_argument("--prices", default=DEFAULT_PRICES_PATH, help="the price table (JSON)")
    parser.add_argument("--price-as", action="append", default=[], metavar="GROUP=MODEL",
                        help="price a logged model group or model as a model in the price table")
    parser.add_argument("--kind", choices=JOB_KINDS,
                        help="the job kind of every input, instead of the replay-shadow/<kind>/ folder each is under")
    parser.add_argument("--since", help="first build start to include: epoch seconds, YYYY-MM-DD or ISO datetime")
    parser.add_argument("--until", help="builds starting at or after this are left out (a date means its end)")
    parser.add_argument("--compare", metavar="BASELINE.json", help="print headline deltas against this report")
    parser.add_argument("--report", metavar="CURRENT.json", help="with --compare: a saved report instead of logs")
    parser.add_argument("--json", action="store_true", help="print JSON")
    args = parser.parse_args(argv)
    if args.report and (args.logs or not args.compare):
        parser.error("--report takes no logs and needs --compare")
    if not args.report and not args.logs:
        parser.error("give log files or directories, or --report with --compare")
    try:
        if args.report:
            with open(args.report, encoding="utf-8") as handle:
                report = json.load(handle)
        else:
            prices = Prices.load(args.prices, parse_price_as(args.price_as))
            quality = Quality()
            since = parse_time(args.since) if args.since else None
            until = parse_time(args.until, end_of_day=True) if args.until else None
            lines, manifests = load_lines(args.logs, quality, args.kind)
            lines, excluded = in_window(lines, since, until, quality)
            manifests = manifests_in_window(manifests, lines, excluded, since, until)
            runs = load_eval(args.eval_data, quality)
            report = build_report(lines, manifests, runs, prices, quality, since, until)
        if args.compare:
            with open(args.compare, encoding="utf-8") as handle:
                result = compare(json.load(handle), report)
            print(json.dumps(result, indent=2, sort_keys=True) if args.json else render_compare(result))
        else:
            print(json.dumps(report, indent=2, sort_keys=True) if args.json else render(report))
    except (OSError, ValueError, KeyError, OverflowError) as exc:
        print(f"baseline_report: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
