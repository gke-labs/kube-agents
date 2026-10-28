"""Harness limits: the scale measurements, their fits and the two sizing answers.

The measurements are ``bench/scale/harness-limits.json``: one record per run, in
the schema that ``bench/scale/README.md`` documents. The metrics, thresholds,
safety margin and machine types are those of
``docs/designs/agent-harness-bottlenecks.md``. Nothing here needs Streamlit or
a connected install; ``views/harness_limits.py`` renders what this module
computes.

A run's ``totals`` are sums over its test clusters. A factor counted per
cluster (nodes, objects) sits on its axis at its total divided by the run's
cluster count, the unit of its level and of the page's input. Churn sits at
the rate the fixture achieved. Any other factor the totals do not count sits
at the run's planned value, ``values``.

A chart fits one straight line per operation, factor and metric, over the base
runs and that factor's runs. A prediction for a fleet starts from the largest
base value of any operation and adds, per factor, the largest increase that any
operation's line predicts from the factor's base to the fleet. The factors are
independent in this sum: it does not model how two factors interact.

The node answer follows Decision 3: one node pool for every pod. The machine
type is the smallest that holds the largest pod's recommended requests, and the
node count holds the requests of every pod, replicas included.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = REPO_ROOT / "bench" / "scale" / "harness-limits.json"
SAMPLE_PATH = REPO_ROOT / "bench" / "scale" / "harness-limits.sample.json"
SCHEMA_VERSION = 1
# The factor of the runs at the base point, where every factor's line starts.
BASE_FACTOR = "base"
# The mode the Phase 1 plan measures; selected when the commit has it.
DEFAULT_MODE = "next"
COMMIT_LABEL_LENGTH = 12
# "YYYY-MM-DD", the date part of a record's ISO 8601 start time.
DATE_LENGTH = 10
# Between the parts of the commit selector's label.
LABEL_SEPARATOR = " · "

# Factor names, as bench/scale/phase1.yaml spells them.
CLUSTERS = "clusters"
NODES = "nodes"
OBJECTS = "objects"
CHURN = "churn_per_minute"
# A count, not a rate: the Pods that fail their image pull at the same time.
FAILING_PODS = "failing_pods"
CONCURRENT = "concurrent"
LISTING_PODS = "listing_pods"
# Not fleet inputs. The proxy load's workers are a load the runner sets (bench/scale/phase2.yaml);
# a combined level is a point that moves several factors at once, not a place on an axis.
PROXY_CONCURRENCY = "proxy_concurrency"
COMBINED = "combined"
# The kinds the design counts as objects. Their summed totals, per cluster, are
# the objects factor's measured value, so the cluster's own objects count too.
OBJECT_KINDS = ("namespaces", "deployments", "services", "hpas", "pods", "configmaps")
# The plan's base point (bench/scale/phase1.yaml `base:`, the design's "Base
# numbers"), per cluster for nodes and objects. Only data without `values`
# (each factor's planned value, which older records lack) reads it: a base
# record's place on the axis of a factor that its totals do not count.
PLAN_BASE_LEVELS = MappingProxyType(
    {
        CLUSTERS: 2.0,
        NODES: 2.0,
        OBJECTS: 100.0,
        CHURN: 10.0,
        FAILING_PODS: 1.0,
        CONCURRENT: 1.0,
        LISTING_PODS: 100.0,
    }
)

# Metric names, as bench/scale/README.md lists them.
MEMORY_PEAK = "memory_peak_bytes"
CPU_PEAK = "cpu_peak_cores"
CPU_SECONDS = "cpu_seconds"
# A CPU peak this close to the container's own CPU limit is the limit, not the need: the container
# was throttled, and what it would have used is unknown. Such a point stays out of the CPU line.
CPU_CAPPED_SHARE = 0.9
RESTARTS = "restarts"
OOMKILLS = "oomkills"
PVC_USED = "pvc_used_bytes"
PVC_CAPACITY = "pvc_capacity_bytes"
PROFILE_MAX = "profile_bytes_max"
PROFILES = "profiles"
PROXY_WAIT_P50 = "proxy_wait_p50_seconds"
PROXY_WAIT_MAX = "proxy_wait_max_seconds"
PROXY_REFUSALS = "proxy_refusals"
PROXY_COMMANDS = "proxy_commands"
PROXY_COMMAND_SECONDS = "proxy_command_seconds"
PROXY_LOAD_COMPLETED = "proxy_load_completed"
PROXY_LOAD_ERRORS = "proxy_load_errors"
PROXY_LOAD_RATE = "proxy_load_per_second"
PROXY_LOAD_P50 = "proxy_load_p50_seconds"
PROXY_LOAD_P95 = "proxy_load_p95_seconds"
PROXY_LOAD_MAX = "proxy_load_max_seconds"
WATCHER_SEEN = "watcher_events_seen"
WATCHER_SEEN_RATE = "watcher_events_seen_per_minute"
WATCHER_CREATES = "watcher_session_creates"
WATCHER_INJECTED = "watcher_events_injected"
WATCHER_SUPPRESSED = "watcher_quota_suppressed"
TRIAGE_LOST = "triage_lost"
RUN_SLOT_REFUSALS = "run_slot_refusals"
FAILED_PROMPTS = "failed_prompts"
UNFINISHED_PROMPTS = "unfinished_prompts"
RESPONSE = "response_seconds"
TOKENS_INPUT = "tokens_input"
TOKENS_OUTPUT = "tokens_output"
TOKENS_CACHED = "tokens_cached"
RUN_TOKENS_INPUT = "run_tokens_input"
RUN_TOKENS_OUTPUT = "run_tokens_output"
RUN_TOKENS_CACHED = "run_tokens_cached"
CHURN_ACHIEVED = "churn_achieved_per_minute"
# A run with a failed prompt is left out of these fits: the runner times only
# the prompts that completed, so the run's value omits the one that failed.
PROMPT_FAILURE_FITS = (RESPONSE,)
# How the runner writes a failed prompt into a record's errors: a line with
# this prefix, or the harness's transport-failure marker in the line.
PROMPT_FAILED_PREFIX = "prompt failed"
INFRA_FAILURE_MARKER = "KUBE_AGENTS_INFRA_FAILURE"
# Decision 3 sizes the node by the largest pod. The gateway pod holds these
# containers; every other container, an unlisted name included, is a pod of
# its own.
GATEWAY_POD = "platform-agent-gateway"
POD_CONTAINERS = MappingProxyType(
    {GATEWAY_POD: ("platform-agent", "agent-api-auth", "hermes-bridge", "fluent-bit", "platform-agent-dashboard")}
)
# The pods that carry a container when a record does not count them.
DEFAULT_REPLICAS = 1
# The keys of a record's `limits` entry, and the metric each one bounds.
LIMIT_MEMORY = "memory_bytes"
LIMIT_CPU = "cpu_cores"
CONTAINER_LIMIT_KEYS = MappingProxyType({MEMORY_PEAK: LIMIT_MEMORY, CPU_PEAK: LIMIT_CPU})

MIB = 1024**2
GIB = 1024**3
MILLICORES_PER_CORE = 1000
# Displayed values from this magnitude up show no decimals.
WHOLE_NUMBER_FROM = 100

# A fleet value within 1% of a measured one counts as measured: totals move by
# a few objects between runs, and the page's inputs round.
RANGE_TOLERANCE = 0.01
# The float noise a least-squares sum can leave on a value that should be exact.
EPSILON = 1e-9
# A line through two distinct values: Phase 1's design, labelled as such.
TWO_POINTS = 2
# Growth below this share of a threshold counts as none: input rounding, not a larger fleet.
NEGLIGIBLE_GROWTH = 1e-3
# A threshold that this fleet would reach only past 10 times its growth is shown as such.
REACHED_DISPLAY_CAP = 10.0
BEYOND_CAP_TEXT = "beyond 10× this growth"
BASE_LEVEL = "base"
# Containers that run to completion (the bus provision Job, the agent pod's init containers).
SHORT_LIVED_CONTAINERS = frozenset({"provision", "prepare-image-trees", "sandbox-credential-cleanup", "sandbox-ssh-key"})
# The dashed prediction runs to twice the largest measured value, or to the
# fleet when the fleet is larger.
CHART_EXTENSION = 2.0

# Decision 2: a recommended limit is 30% above the predicted peak.
SAFETY_MARGIN = 0.30

PASS = "pass"
WARN = "warn"
FAIL = "fail"
NO_DATA = "no data"
NO_LIMIT = "no limit set"
STATUS_LABELS = MappingProxyType(
    {
        PASS: "✓ pass",
        WARN: "▲ warn",
        FAIL: "× fail",
        NO_DATA: "– no data",
        NO_LIMIT: "– no limit set",
    }
)
TWO_POINT_LABEL = "two-point estimate"
LEAST_SQUARES_LABEL = "least-squares fit"
NO_FIT_LABEL = "no fit"
PREDICTED_LABEL = "predicted, not measured"
INTERPOLATED_LABEL = "within the measured range"
MEASURED_LABEL = "measured at the base point"
BASE_VALUE_LABEL = "base value"
NO_VALUE = "—"
YES_LABEL = "✓ yes"
NO_LABEL = "no"
LIMIT_LABEL = "{container} limit"
PVC_CAPACITY_LABEL = "PVC capacity {capacity}"
# The columns of the editable limits table.
LIMIT_CONTAINER_COLUMN = "Container"
LIMIT_MEMORY_COLUMN = "Memory limit (MiB)"
LIMIT_CPU_COLUMN = "CPU limit (cores)"

# The PlatformAgent field that sets each harness container's resources. The
# order is also the order of the containers' chart colours.
CONTAINER_FIELDS = MappingProxyType(
    {
        "platform-agent": "spec.deployment.resources",
        "agent-api-auth": "spec.deployment.agentAPIAuth.resources",
        "envoy-credential-proxy": "spec.deployment.credentialProxy.resources",
        "manager": "Helm value operator.resources (the controller-manager)",
        "litellm-container": "Helm value litellm.resources",
    }
)
HARNESS_CONTAINERS = tuple(CONTAINER_FIELDS)
NO_FIELD = "no PlatformAgent field"

PROXY_QUEUE_NOTE = (
    "The broker admits at most 8 commands at once. More memory in "
    "spec.deployment.credentialProxy.resources helps only below 2Gi; at 2Gi "
    "and above, no field moves the cap."
)
RESPONSE_NOTE = "Model latency and the queues for run slots and kanban workers set it."
# The thresholds that container resources do not move, and what does.
UNFIXABLE = MappingProxyType(
    {
        PROXY_WAIT_P50: PROXY_QUEUE_NOTE,
        PROXY_WAIT_MAX: PROXY_QUEUE_NOTE,
        PROXY_REFUSALS: PROXY_QUEUE_NOTE,
        RUN_SLOT_REFUSALS: (
            "The agent runs at most 10 turns at once. No PlatformAgent field changes it."
        ),
        TRIAGE_LOST: (
            "A triage start makes up to 6 answered attempts within a 300 s budget, "
            "with a 25 s session-create timeout. No setting changes it."
        ),
        FAILED_PROMPTS: (
            "The agent API gave no answer, an empty answer, a transport failure, or an "
            "answer with no model call behind it. The errors column of the raw records "
            "names the cause."
        ),
        RESPONSE: RESPONSE_NOTE,
    }
)


class HarnessDataError(ValueError):
    """The data file cannot be read, or does not follow the documented schema."""


@dataclass(frozen=True)
class FactorSpec:
    """One stress point: the fleet input that sets it and the axis it is fitted on."""

    input_label: str
    # The level, the axis and the input count per cluster, so a measured total
    # is divided by the run's cluster count.
    per_cluster: bool = False
    # Totals keys summed into the measured value when no key names the factor.
    total_kinds: tuple[str, ...] = ()
    help: str = ""


FACTORS = MappingProxyType(
    {
        CLUSTERS: FactorSpec("Clusters"),
        NODES: FactorSpec("Nodes per cluster", per_cluster=True),
        OBJECTS: FactorSpec(
            "Objects per cluster",
            per_cluster=True,
            total_kinds=OBJECT_KINDS,
            help=(
                "Namespaces, Deployments, Services, HPAs, Pods and ConfigMaps in "
                "one cluster, the cluster's own objects included."
            ),
        ),
        CHURN: FactorSpec(
            "Pods created per minute",
            help="Pods created and deleted per minute, across the fleet.",
        ),
        FAILING_PODS: FactorSpec(
            "Pods failing at once",
            help=(
                "Pods failing at once: the Pods of one workload that fail their image "
                "pull at the same time. Each one writes Warning events."
            ),
        ),
        CONCURRENT: FactorSpec(
            "Concurrent requests",
            help="User questions that run while scheduled audits and triages run.",
        ),
        LISTING_PODS: FactorSpec(
            "Pods in the largest listing",
            help="Pods in the one namespace that the largest single agent listing reads.",
        ),
    }
)
# Factors that are a load the runner applies, not a property of the fleet: charted against their
# own runs, never a fleet input, so a prediction does not move with them.
LOAD_FACTORS = MappingProxyType({PROXY_CONCURRENCY: "Credential proxy load workers"})


@dataclass(frozen=True)
class MetricSpec:
    label: str
    unit: str = ""
    scale: float = 1.0


METRICS = MappingProxyType(
    {
        MEMORY_PEAK: MetricSpec("Memory peak", "MiB", MIB),
        CPU_PEAK: MetricSpec("CPU peak", "cores"),
        CPU_SECONDS: MetricSpec("CPU used over the window", "core-s"),
        RESTARTS: MetricSpec("Restarts"),
        OOMKILLS: MetricSpec("OOMKills"),
        PVC_USED: MetricSpec("Data PVC used", "GiB", GIB),
        PVC_CAPACITY: MetricSpec("Data PVC capacity", "GiB", GIB),
        PROFILE_MAX: MetricSpec("Largest profile", "MiB", MIB),
        PROFILES: MetricSpec("Profiles on the data PVC"),
        PROXY_WAIT_P50: MetricSpec("Credential proxy wait, median", "s"),
        PROXY_WAIT_MAX: MetricSpec("Credential proxy wait, maximum", "s"),
        PROXY_REFUSALS: MetricSpec("Credential proxy refusals"),
        PROXY_COMMANDS: MetricSpec("Brokered commands"),
        PROXY_COMMAND_SECONDS: MetricSpec("Brokered command time, summed", "s"),
        PROXY_LOAD_COMPLETED: MetricSpec("Proxy load: commands completed"),
        PROXY_LOAD_ERRORS: MetricSpec("Proxy load: commands failed"),
        PROXY_LOAD_RATE: MetricSpec("Proxy load: commands per second"),
        PROXY_LOAD_P50: MetricSpec("Proxy load: command time, median", "s"),
        PROXY_LOAD_P95: MetricSpec("Proxy load: command time, 95th percentile", "s"),
        PROXY_LOAD_MAX: MetricSpec("Proxy load: command time, maximum", "s"),
        WATCHER_SEEN: MetricSpec("Watcher events seen"),
        WATCHER_SEEN_RATE: MetricSpec("Watcher events seen per minute"),
        WATCHER_CREATES: MetricSpec("Watcher session creates"),
        WATCHER_INJECTED: MetricSpec("Watcher events injected"),
        WATCHER_SUPPRESSED: MetricSpec("Watcher events suppressed by quota"),
        TRIAGE_LOST: MetricSpec("Triage starts lost"),
        RUN_SLOT_REFUSALS: MetricSpec("Run-slot refusals"),
        FAILED_PROMPTS: MetricSpec("Failed prompts"),
        UNFINISHED_PROMPTS: MetricSpec("Prompts answered without the operation"),
        RESPONSE: MetricSpec("Response time", "s"),
        TOKENS_INPUT: MetricSpec("Input tokens, every caller (whole prompt)"),
        TOKENS_OUTPUT: MetricSpec("Output tokens, every caller"),
        TOKENS_CACHED: MetricSpec("Cached tokens, every caller"),
        RUN_TOKENS_INPUT: MetricSpec("Input tokens, the run's prompts (non-cached part)"),
        RUN_TOKENS_OUTPUT: MetricSpec("Output tokens, the run's prompts"),
        RUN_TOKENS_CACHED: MetricSpec("Cached tokens, the run's prompts"),
        CHURN_ACHIEVED: MetricSpec("Pods created per minute, achieved"),
    }
)


@dataclass(frozen=True)
class Line:
    """A threshold. A value above it warns, or fails when the line is critical."""

    value: float
    label: str
    critical: bool = True


# The design's Metrics table: the credential proxy's broker refuses a command
# that waits 60 s for admission; the response-time gates are "<30s OK, >60s
# investigate"; OOMKills, restarts, lost work and failed prompts fail at any
# count. A metric without a line here is charted without a threshold.
PROXY_WAIT_LINES = (Line(60.0, "60 s: broker refuses"),)
RESPONSE_LINES = (
    Line(30.0, "30 s: OK", critical=False),
    Line(60.0, "60 s: investigate"),
)
ABOVE_ZERO = (Line(0.0, "above 0"),)
FIXED_LINES = MappingProxyType(
    {
        PROXY_WAIT_P50: PROXY_WAIT_LINES,
        PROXY_WAIT_MAX: PROXY_WAIT_LINES,
        RESPONSE: RESPONSE_LINES,
        RESTARTS: ABOVE_ZERO,
        OOMKILLS: ABOVE_ZERO,
        PROXY_REFUSALS: ABOVE_ZERO,
        RUN_SLOT_REFUSALS: ABOVE_ZERO,
        TRIAGE_LOST: ABOVE_ZERO,
        FAILED_PROMPTS: ABOVE_ZERO,
    }
)
# A metric whose threshold is another metric of the same run.
CAPACITY_METRICS = MappingProxyType({PVC_USED: PVC_CAPACITY})
# The response-time gates are for typical queries: the plan's fan-out question
# and the metadata question asked during it. The other operations are long
# reports, such as a fleet audit; their response time is charted without a gate.
QUERY_OPERATIONS = ("fan_out",)
QUERY_METRICS = (RESPONSE,)

# How far a capacity search looks: up to this multiple of the input's base value.
CAPACITY_CEILING = 1000.0
# Halvings of the search interval; 30 resolve a ceiling of thousands to a thousandth.
CAPACITY_STEPS = 30
# Fleet-wide factors: a fleet with more clusters of the same shape has proportionally more of them.
FLEET_WIDE_FACTORS = (CHURN, FAILING_PODS)
# The credential proxy's container, and the operation that loads it with no model call.
PROXY_CONTAINER = "envoy-credential-proxy"
SHELL_CONTAINER = "shell"
PROXY_LOAD_OPERATION = "proxy_load"
SECONDS_PER_MINUTE = 60.0
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class MachineType:
    name: str
    vcpu: int
    memory_gib: float
    allocatable_memory_gib: float
    allocatable_cpu: float
    reason: str

    @property
    def allocatable_memory_bytes(self) -> float:
        return self.allocatable_memory_gib * GIB


# Decision 3. Allocatable memory is the design's value (GKE's reservation
# formula and the 100Mi eviction threshold). Allocatable CPU applies GKE's CPU
# reservation: 6% of the first core, 1% of the second, 0.5% of the next two
# and 0.25% of each core above four.
MACHINE_TYPES = (
    MachineType("e2-standard-2", 2, 8, 6.1, 1.93, "Smallest size on the list; the lower bound"),
    MachineType("e2-standard-4", 4, 16, 13.3, 3.92, "The installer's Standard-mode default"),
    MachineType("e2-standard-8", 8, 32, 28.3, 7.91, "Next step up, same family"),
    MachineType("n2-standard-16", 16, 64, 58.4, 15.89, "Large fleet, CPU-bound fan-outs"),
    MachineType("n2-highmem-16", 16, 128, 118.6, 15.89, "Large fleet, memory-bound caches"),
)


@dataclass(frozen=True)
class Measurement:
    name: str
    value: float
    container: str | None = None


@dataclass(frozen=True)
class Record:
    """One run, as bench/scale/README.md documents it."""

    key: str
    commit: str
    mode: str
    phase: str
    factor: str
    level: float
    operation: str
    run: int
    started: str
    ended: str
    metrics: tuple[Measurement, ...]
    # The fields below are optional in the file: a record written before the
    # runner recorded them still loads.
    image: str = ""
    # Every factor's planned value at the run's point.
    values: Mapping[str, float] = field(default_factory=dict)
    totals: Mapping[str, float] = field(default_factory=dict)
    limits: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    requests: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    # Per container, the running pods that carried it.
    replicas: Mapping[str, float] = field(default_factory=dict)
    provider_429s: int = 0
    excluded_from: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    _readings: dict[tuple[str, str | None], float] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        readings: dict[tuple[str, str | None], float] = {}
        for measurement in self.metrics:
            readings.setdefault((measurement.name, measurement.container), measurement.value)
        object.__setattr__(self, "_readings", readings)

    @property
    def is_base(self) -> bool:
        return self.factor == BASE_FACTOR

    @property
    def prompt_failed(self) -> bool:
        return any(
            error.startswith(PROMPT_FAILED_PREFIX) or INFRA_FAILURE_MARKER in error
            for error in self.errors
        )

    def excluded(self, metric: str) -> bool:
        """Whether the run is left out of the metric's fit and base value."""
        return metric in self.excluded_from or (
            metric in PROMPT_FAILURE_FITS and self.prompt_failed
        )

    def value(self, metric: str, container: str | None = None) -> float | None:
        return self._readings.get((metric, container))


@dataclass(frozen=True)
class Dataset:
    records: tuple[Record, ...]
    path: Path
    is_sample: bool


@dataclass(frozen=True)
class Point:
    x: float
    y: float
    key: str
    run: int
    is_base: bool
    excluded: bool
    # The planned point the run belongs to ("base", or the factor's level). Runs at one planned
    # point measure slightly different totals, so the x values alone over-count the levels.
    level: str = ""


@dataclass(frozen=True)
class Fit:
    """A least-squares line; through two points it is the line through them."""

    slope: float
    intercept: float
    levels: int
    x_min: float
    x_max: float

    def at(self, x: float) -> float:
        return self.intercept + self.slope * x

    @property
    def two_point(self) -> bool:
        return self.levels == TWO_POINTS

    @property
    def label(self) -> str:
        return TWO_POINT_LABEL if self.two_point else LEAST_SQUARES_LABEL


@dataclass(frozen=True)
class Contribution:
    """One factor's predicted increase, from the operation that predicts the most."""

    factor: str
    operation: str
    base_x: float | None
    fleet_x: float | None
    fit: Fit | None
    delta: float

    @property
    def grows(self) -> bool:
        if self.fleet_x is None:
            return False
        return self.base_x is None or not _close(self.fleet_x, self.base_x)

    @property
    def outside(self) -> bool:
        return (
            self.fit is not None
            and self.fleet_x is not None
            and _outside(self.fleet_x, self.fit)
        )


@dataclass(frozen=True)
class Prediction:
    metric: str
    container: str | None
    base: float
    base_operation: str
    contributions: tuple[Contribution, ...]

    @property
    def value(self) -> float:
        return self.base + sum(item.delta for item in self.contributions)

    @property
    def growth(self) -> float:
        return self.value - self.base

    @property
    def outside(self) -> tuple[Contribution, ...]:
        return tuple(item for item in self.contributions if item.grows and item.outside)

    @property
    def two_point(self) -> bool:
        return any(
            item.grows and item.fit is not None and item.fit.two_point
            for item in self.contributions
        )

    @property
    def missing_fits(self) -> tuple[str, ...]:
        return tuple(
            item.factor for item in self.contributions if item.grows and item.fit is None
        )


@dataclass(frozen=True)
class Check:
    metric: str
    container: str | None
    lines: tuple[Line, ...]
    prediction: Prediction | None
    status: str
    crossed: Line | None
    # The share of the fleet's growth over the base at which the prediction
    # crosses the lowest critical line; None when it never does.
    reached: float | None

    @property
    def label(self) -> str:
        name = metric_spec(self.metric).label
        return f"{name} · {self.container}" if self.container else name

    @property
    def name(self) -> str:
        """The metric, and the operations its threshold reads when it is gated."""
        gated = gated_operations(self.metric)
        label = metric_spec(self.metric).label
        return f"{label} ({', '.join(gated)})" if gated else label


@dataclass(frozen=True)
class Recommendation:
    container: str
    memory: Prediction | None
    cpu: Prediction | None

    @property
    def field(self) -> str:
        return CONTAINER_FIELDS.get(self.container, NO_FIELD)

    @property
    def memory_bytes(self) -> float | None:
        return _recommended(self.memory)

    @property
    def cpu_cores(self) -> float | None:
        return _recommended(self.cpu)


@dataclass(frozen=True)
class PodRequest:
    """One pod's recommended requests: the sum of its containers' recommended limits."""

    name: str
    containers: tuple[str, ...]
    replicas: int
    memory_bytes: float
    cpu_cores: float


@dataclass(frozen=True)
class NodePlan:
    """Decision 3: one node pool holds every pod."""

    pods: tuple[PodRequest, ...]

    @property
    def largest_memory_pod(self) -> PodRequest | None:
        return max(self.pods, key=lambda pod: pod.memory_bytes, default=None)

    @property
    def largest_cpu_pod(self) -> PodRequest | None:
        return max(self.pods, key=lambda pod: pod.cpu_cores, default=None)

    @property
    def largest_memory_bytes(self) -> float:
        """The largest memory request of one pod, which one node must hold."""
        return self.largest_memory_pod.memory_bytes if self.largest_memory_pod else 0.0

    @property
    def largest_cpu_cores(self) -> float:
        return self.largest_cpu_pod.cpu_cores if self.largest_cpu_pod else 0.0

    @property
    def total_memory_bytes(self) -> float:
        return sum(pod.memory_bytes * pod.replicas for pod in self.pods)

    @property
    def total_cpu_cores(self) -> float:
        return sum(pod.cpu_cores * pod.replicas for pod in self.pods)

    @property
    def machine(self) -> MachineType | None:
        """The smallest machine type that holds the largest pod; None when none does."""
        return smallest_machine(self.largest_memory_bytes, self.largest_cpu_cores)

    @property
    def nodes(self) -> int | None:
        machine = self.machine
        if machine is None:
            return None
        return nodes_needed(machine, self.total_memory_bytes, self.total_cpu_cores)


@dataclass(frozen=True)
class ChartSeries:
    name: str
    container: str | None
    points: tuple[Point, ...]
    fit: Fit | None
    limit: float | None


@dataclass(frozen=True)
class MetricChart:
    metric: str
    factor: str
    operation: str
    title: str
    x_title: str
    y_title: str
    unit: str
    scale: float
    series: tuple[ChartSeries, ...]
    lines: tuple[Line, ...]
    x_end: float
    fleet_x: float | None


# --- loading ----------------------------------------------------------------


def display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def load_dataset(data_path: Path | None = None, sample_path: Path | None = None) -> Dataset:
    """The measured data, or the sample when no measured file exists yet."""
    data_path = data_path or DATA_PATH
    if data_path.exists():
        return Dataset(_read(data_path), data_path, is_sample=False)
    sample_path = sample_path or SAMPLE_PATH
    return Dataset(_read(sample_path), sample_path, is_sample=True)


def _read(path: Path) -> tuple[Record, ...]:
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise HarnessDataError(f"{display_path(path)}: {exc}") from exc
    return parse_records(raw, display_path(path))


def parse_records(raw: Any, source: str = "data") -> tuple[Record, ...]:
    if not isinstance(raw, dict):
        raise HarnessDataError(f"{source}: the top level is not an object")
    if raw.get("schema") != SCHEMA_VERSION:
        raise HarnessDataError(
            f"{source}: schema {raw.get('schema')!r}, expected {SCHEMA_VERSION}"
        )
    items = raw.get("records")
    if not isinstance(items, list):
        raise HarnessDataError(f"{source}: records is not a list")
    return tuple(
        _record(item, f"{source}: record {index + 1}") for index, item in enumerate(items)
    )


def _record(item: Any, where: str) -> Record:
    if not isinstance(item, dict):
        raise HarnessDataError(f"{where}: not an object")
    if isinstance(item.get("key"), str):
        where = f"{where} ({item['key']})"
    metrics = item.get("metrics")
    if not isinstance(metrics, list):
        raise HarnessDataError(f"{where}: metrics is not a list")
    resources = {}
    for kind in ("limits", "requests"):
        raw = item.get(kind, {})
        if not isinstance(raw, dict):
            raise HarnessDataError(f"{where}: {kind} is not an object")
        resources[kind] = MappingProxyType(
            {
                str(container): MappingProxyType(_numbers(values, f"{where}: {kind}.{container}"))
                for container, values in raw.items()
            }
        )
    measurements = (
        _measurement(entry, f"{where}: metric {index + 1}")
        for index, entry in enumerate(metrics)
    )
    image = item.get("image", "")
    if not isinstance(image, str):
        raise HarnessDataError(f"{where}: image is not a string")
    return Record(
        key=_text(item, "key", where),
        commit=_text(item, "commit", where),
        mode=_text(item, "mode", where),
        phase=_text(item, "phase", where),
        factor=_text(item, "factor", where),
        level=_number(item.get("level"), f"{where}: level"),
        operation=_text(item, "operation", where),
        run=int(_number(item.get("run"), f"{where}: run")),
        started=_text(item, "started", where),
        ended=_text(item, "ended", where),
        metrics=tuple(entry for entry in measurements if entry is not None),
        image=image,
        values=MappingProxyType(_numbers(item.get("values", {}), f"{where}: values")),
        totals=MappingProxyType(_numbers(item.get("totals", {}), f"{where}: totals")),
        limits=resources["limits"],
        requests=resources["requests"],
        replicas=MappingProxyType(_numbers(item.get("replicas", {}), f"{where}: replicas")),
        provider_429s=int(_number(item.get("provider_429s", 0), f"{where}: provider_429s")),
        excluded_from=_texts(item, "excluded_from", where),
        errors=_texts(item, "errors", where),
    )


def _measurement(entry: Any, where: str) -> Measurement | None:
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        raise HarnessDataError(f"{where}: not an object with a name")
    value = _number(entry.get("value"), f"{where} ({entry['name']}): value")
    if not math.isfinite(value):
        # A NaN or infinite reading carries no value a line can fit.
        return None
    container = entry.get("container")
    if container is not None and not isinstance(container, str):
        raise HarnessDataError(f"{where} ({entry['name']}): container is not a name")
    return Measurement(entry["name"], value, container or None)


def _text(item: dict, name: str, where: str) -> str:
    value = item.get(name)
    if not isinstance(value, str):
        raise HarnessDataError(f"{where}: {name} is missing or not a string")
    return value


def _texts(item: dict, name: str, where: str) -> tuple[str, ...]:
    """An optional list of strings; an absent one is empty."""
    values = item.get(name, [])
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise HarnessDataError(f"{where}: {name} is not a list of strings")
    return tuple(values)


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HarnessDataError(f"{where} is missing or not a number")
    return float(value)


def _numbers(values: Any, where: str) -> dict[str, float]:
    if not isinstance(values, dict):
        raise HarnessDataError(f"{where} is not an object")
    return {str(name): _number(value, f"{where}.{name}") for name, value in values.items()}


# --- selection --------------------------------------------------------------


def commits(records: Iterable[Record]) -> list[str]:
    """Every commit in the data, the one with the latest run first."""
    latest: dict[str, str] = {}
    for record in records:
        latest[record.commit] = max(latest.get(record.commit, record.started), record.started)
    return sorted(latest, key=lambda commit: (latest[commit], commit), reverse=True)


def commit_label(records: Iterable[Record], commit: str) -> str:
    """The commit, its runs' images where they recorded one, its phases and its latest date."""
    selected = [record for record in records if record.commit == commit]
    images = ", ".join(sorted({record.image for record in selected if record.image}))
    phases = ", ".join(sorted({record.phase for record in selected}))
    latest = max(record.started for record in selected)
    parts = (commit[:COMMIT_LABEL_LENGTH], images, phases, latest[:DATE_LENGTH])
    return LABEL_SEPARATOR.join(part for part in parts if part)


def modes(records: Iterable[Record], commit: str) -> list[str]:
    return sorted({record.mode for record in records if record.commit == commit})


def default_mode_index(available: Sequence[str]) -> int:
    return available.index(DEFAULT_MODE) if DEFAULT_MODE in available else 0


def select(records: Iterable[Record], commit: str, mode: str) -> tuple[Record, ...]:
    return tuple(
        record for record in records if record.commit == commit and record.mode == mode
    )


def operations(records: Iterable[Record]) -> list[str]:
    """Every operation, in the order the runs first reach it."""
    return list(dict.fromkeys(record.operation for record in records))


def factors(records: Iterable[Record], operation: str | None = None) -> list[str]:
    """Every factor with runs of its own: the planned ones first, in plan order.

    Combined points are not a factor's axis; ``combined_rows`` reports them.
    """
    found = {
        record.factor
        for record in records
        if not record.is_base
        and record.factor != COMBINED
        and (operation is None or record.operation == operation)
    }
    return [name for name in FACTORS if name in found] + sorted(found - set(FACTORS))


def unmapped_factors(records: Iterable[Record]) -> list[str]:
    """Factors that have no fleet input, so predictions hold them at their base."""
    return [name for name in factors(records) if name not in FACTORS and name not in LOAD_FACTORS]


def factor_label(factor: str) -> str:
    """A factor's name as the page shows it."""
    if factor in FACTORS:
        return FACTORS[factor].input_label
    return LOAD_FACTORS.get(factor, factor)


def containers(records: Iterable[Record]) -> list[str]:
    """Every container with a measurement: harness containers first, in field order."""
    found = {
        measurement.container
        for record in records
        for measurement in record.metrics
        if measurement.container
    }
    return [name for name in HARNESS_CONTAINERS if name in found] + sorted(
        found - set(HARNESS_CONTAINERS)
    )


def container_colors(
    available: Sequence[str], palette: Sequence[str], fallback: str
) -> dict[str | None, str]:
    """A fixed colour per container, so a filter never repaints the others.

    A run-level series takes the first colour. Containers past the palette's
    length share the neutral fallback instead of a cycled or generated hue.
    """
    colors: dict[str | None, str] = {None: palette[0]}
    for index, name in enumerate(available):
        colors[name] = palette[index] if index < len(palette) else fallback
    return colors


def metric_spec(metric: str) -> MetricSpec:
    return METRICS.get(metric, MetricSpec(metric))


def metric_names(records: Iterable[Record]) -> list[str]:
    found = {measurement.name for record in records for measurement in record.metrics}
    return [name for name in METRICS if name in found] + sorted(found - set(METRICS))


def latest_limits(records: Iterable[Record]) -> dict[str, dict[str, float]]:
    """Per container, the limits of the latest run that read them."""
    limits: dict[str, dict[str, float]] = {}
    for record in sorted(records, key=lambda item: (item.started, item.key)):
        for container, values in record.limits.items():
            limits[container] = dict(values)
    return limits


def latest_requests(records: Iterable[Record]) -> dict[str, dict[str, float]]:
    """Per container, the requests of the latest run that read them."""
    requests: dict[str, dict[str, float]] = {}
    for record in sorted(records, key=lambda item: (item.started, item.key)):
        for container, values in record.requests.items():
            requests[container] = dict(values)
    return requests


def limit_table(
    limits: Mapping[str, Mapping[str, float]], selected: Sequence[str]
) -> dict[str, list]:
    """The limits as an editable table, in MiB and cores; 0 is no limit."""
    return {
        LIMIT_CONTAINER_COLUMN: list(selected),
        LIMIT_MEMORY_COLUMN: [
            limits.get(name, {}).get(LIMIT_MEMORY, 0.0) / MIB for name in selected
        ],
        LIMIT_CPU_COLUMN: [limits.get(name, {}).get(LIMIT_CPU, 0.0) for name in selected],
    }


def limits_from_table(table: Mapping[str, Sequence[Any]]) -> dict[str, dict[str, float]]:
    """The edited table back in bytes and cores; an empty cell is no limit."""
    rows = zip(
        table[LIMIT_CONTAINER_COLUMN], table[LIMIT_MEMORY_COLUMN], table[LIMIT_CPU_COLUMN]
    )
    return {
        name: {LIMIT_MEMORY: _cell(memory_mib) * MIB, LIMIT_CPU: _cell(cores)}
        for name, memory_mib, cores in rows
    }


def _cell(value: Any) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return 0.0


# --- fitting ----------------------------------------------------------------


def factor_value(record: Record, factor: str) -> float | None:
    """Where a run sits on a factor's axis.

    A factor that the totals count sits at its measured value, per cluster
    when the factor is counted per cluster. Any other factor sits at the run's
    planned value. A record without planned values falls back to its level,
    or, for a base record, to the plan's base point.
    """
    # Churn sits at the rate the fixture achieved, which can fall short of the plan.
    if factor == CHURN:
        achieved = record.value(CHURN_ACHIEVED)
        if achieved is not None:
            return achieved
    spec = FACTORS.get(factor)
    kinds = (factor,) if factor in record.totals else spec.total_kinds if spec else ()
    if kinds and all(kind in record.totals for kind in kinds):
        total = sum(record.totals[kind] for kind in kinds)
        if spec is None or not spec.per_cluster:
            return total
        clusters = record.totals.get(CLUSTERS)
        if clusters:
            return total / clusters
    if factor in record.values:
        return record.values[factor]
    if record.factor == factor:
        return record.level
    if record.is_base and not record.values:
        return PLAN_BASE_LEVELS.get(factor)
    return None


def series_points(
    records: Iterable[Record],
    operation: str,
    factor: str,
    metric: str,
    container: str | None = None,
) -> tuple[Point, ...]:
    """The base runs and the factor's runs of one operation, on the factor's axis."""
    points = []
    for record in records:
        if record.operation != operation or not (record.is_base or record.factor == factor):
            continue
        y = record.value(metric, container)
        x = factor_value(record, factor)
        if x is None or y is None:
            continue
        level = BASE_LEVEL if record.is_base else f"{record.level:g}"
        excluded = record.excluded(metric) or capped(record, metric, container)
        points.append(Point(x, y, record.key, record.run, record.is_base, excluded, level))
    return tuple(sorted(points, key=lambda point: (point.x, point.key)))


def capped(record: Record, metric: str, container: str | None) -> bool:
    """Whether a run's CPU peak is its container's CPU limit rather than its need."""
    if metric != CPU_PEAK or container is None:
        return False
    limit = record.limits.get(container, {}).get(LIMIT_CPU)
    value = record.value(metric, container)
    return bool(limit) and value is not None and value >= CPU_CAPPED_SHARE * limit


def fit_line(points: Iterable[Point]) -> Fit | None:
    """Least squares over the points a record does not exclude; None below two levels."""
    used = [point for point in points if not point.excluded]
    # Distinct planned points, not distinct x values: three runs at one point are one level.
    levels = {point.level or point.x for point in used}
    xs = [point.x for point in used]
    if len(levels) < TWO_POINTS or len(set(xs)) < TWO_POINTS:
        return None
    count = len(used)
    mean_x = sum(xs) / count
    mean_y = sum(point.y for point in used) / count
    spread = sum((x - mean_x) ** 2 for x in xs)
    covariance = sum((point.x - mean_x) * (point.y - mean_y) for point in used)
    slope = covariance / spread
    return Fit(slope, mean_y - slope * mean_x, len(levels), min(xs), max(xs))


def base_records(records: Iterable[Record], operation: str) -> list[Record]:
    """The base runs of an operation.

    An operation with no base run of its own, such as one measured at one fleet
    size under raised limits, has no base and does not anchor a prediction. Only
    data with no base run at all falls back to each factor's lowest level.
    """
    records = list(records)
    runs = [record for record in records if record.operation == operation]
    base = [record for record in runs if record.is_base]
    if base or any(record.is_base for record in records):
        return base
    lowest: dict[str, float] = {}
    for record in runs:
        lowest[record.factor] = min(lowest.get(record.factor, record.level), record.level)
    return [record for record in runs if record.level == lowest[record.factor]]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _close(first: float, second: float) -> bool:
    return abs(first - second) <= RANGE_TOLERANCE * max(abs(first), abs(second)) + EPSILON


def _outside(x: float, fit: Fit) -> bool:
    return (x > fit.x_max and not _close(x, fit.x_max)) or (
        x < fit.x_min and not _close(x, fit.x_min)
    )


def _base_value(
    records: Sequence[Record], operation: str, metric: str, container: str | None
) -> float | None:
    values = [
        value
        for record in base_records(records, operation)
        if not record.excluded(metric)
        and not capped(record, metric, container)
        and (value := record.value(metric, container)) is not None
    ]
    return _mean(values) if values else None


def _base_x(records: Sequence[Record], operation: str, factor: str) -> float | None:
    values = [
        value
        for record in base_records(records, operation)
        if (record.is_base or record.factor == factor)
        and (value := factor_value(record, factor)) is not None
    ]
    return _mean(values) if values else None


# --- prediction -------------------------------------------------------------


def fleet_value(factor: str, fleet: Mapping[str, float]) -> float | None:
    """The fleet's position on a factor's axis, or None when no input sets it."""
    return fleet.get(factor) if factor in FACTORS else None


def base_fleet(records: Sequence[Record]) -> dict[str, float]:
    """The fleet inputs that put every factor at the base point of the data.

    Without base runs or runs of its own, a factor takes the planned value of
    the other factors' lowest runs, which hold it at the base; data without
    planned values takes the plan's base point.
    """
    base = [
        record for operation in operations(records) for record in base_records(records, operation)
    ]
    fleet = {}
    for factor in FACTORS:
        # Churn's default is the planned base rate: the achieved rate differs per operation's
        # runs, and averaging it across operations would move every prediction off its base.
        planned = [record.values[factor] for record in base if factor in record.values]
        if factor == CHURN and planned:
            fleet[factor] = _mean(planned)
            continue
        values = [
            value
            for record in base
            if (record.is_base or record.factor == factor)
            and (value := factor_value(record, factor)) is not None
        ] or planned
        fleet[factor] = _mean(values) if values else PLAN_BASE_LEVELS[factor]
    return fleet


def grown_factors(records: Sequence[Record], fleet: Mapping[str, float]) -> list[str]:
    """The measured factors at which the fleet differs from the base point."""
    base = base_fleet(records)
    return [
        factor
        for factor in factors(records)
        if factor in FACTORS and not _close(fleet.get(factor, base[factor]), base[factor])
    ]


def _contribution(
    records: Sequence[Record],
    operation: str,
    factor: str,
    metric: str,
    container: str | None,
    fleet: Mapping[str, float],
) -> Contribution | None:
    points = series_points(records, operation, factor, metric, container)
    if all(point.is_base for point in points):
        return None
    fit = fit_line(points)
    base_x = _base_x(records, operation, factor)
    if base_x is None and fit is not None:
        base_x = fit.x_min
    fleet_x = fleet_value(factor, fleet)
    delta = 0.0
    if fit is not None and fleet_x is not None and base_x is not None:
        delta = fit.slope * (fleet_x - base_x)
    return Contribution(factor, operation, base_x, fleet_x, fit, delta)


def predict(
    records: Sequence[Record],
    metric: str,
    container: str | None,
    fleet: Mapping[str, float],
    only: Sequence[str] | None = None,
) -> Prediction | None:
    """The base value plus each factor's increase, or None when no base run has the metric.

    ``only`` limits the prediction to those operations.
    """
    names = [name for name in operations(records) if only is None or name in only]
    bases = {
        operation: value
        for operation in names
        if (value := _base_value(records, operation, metric, container)) is not None
    }
    if not bases:
        return None
    base_operation = max(bases, key=lambda operation: bases[operation])
    contributions = []
    for factor in factors(records):
        if factor in LOAD_FACTORS:
            continue
        candidates = [
            item
            for operation in names
            if (item := _contribution(records, operation, factor, metric, container, fleet))
            is not None
        ]
        if candidates:
            # A fitted line beats none; then the largest increase, the safe side.
            contributions.append(
                max(candidates, key=lambda item: (item.fit is not None, item.delta))
            )
    return Prediction(
        metric, container, bases[base_operation], base_operation, tuple(contributions)
    )


def basis_flags(prediction: Prediction) -> tuple[str, ...]:
    """How far to trust a prediction, in the labels the charts use."""
    fitted = [item for item in prediction.contributions if item.grows and item.fit]
    flags = []
    if prediction.outside:
        flags.append(
            f"{PREDICTED_LABEL}: "
            + ", ".join(_range_note(item) for item in prediction.outside)
        )
    elif fitted:
        flags.append(INTERPOLATED_LABEL)
    elif not prediction.missing_fits:
        flags.append(MEASURED_LABEL)
    if prediction.two_point:
        flags.append(TWO_POINT_LABEL)
    if prediction.missing_fits:
        flags.append(f"{NO_FIT_LABEL} for {', '.join(prediction.missing_fits)}")
    return tuple(flags)


def basis(prediction: Prediction | None) -> str:
    return "; ".join(basis_flags(prediction)) if prediction else NO_VALUE


def _range_note(item: Contribution) -> str:
    """The input past the measured range, against the end it passes."""
    fit, fleet_x = item.fit, item.fleet_x
    if fit is None or fleet_x is None:
        return item.factor
    if fleet_x > fit.x_max:
        return f"{item.factor} {_axis_text(fleet_x)} > {_axis_text(fit.x_max)}"
    return f"{item.factor} {_axis_text(fleet_x)} < {_axis_text(fit.x_min)}"


# --- checks -----------------------------------------------------------------


def evaluate(value: float, lines: Sequence[Line]) -> tuple[str, Line | None]:
    """Fail above a critical line, warn above another; the highest line crossed."""
    crossed = [
        line for line in sorted(lines, key=lambda item: item.value) if value > line.value + EPSILON
    ]
    if not crossed:
        return PASS, None
    status = FAIL if any(line.critical for line in crossed) else WARN
    return status, crossed[-1]


def reached_at(prediction: Prediction, threshold: float) -> float | None:
    """The share of the growth from the base at which the prediction passes the threshold.

    Growth too small to matter (a fleet that differs from the base only by the
    rounding of its inputs) reaches nothing, rather than a share in the millions.
    """
    if prediction.base > threshold + EPSILON:
        return 0.0
    if prediction.growth <= max(EPSILON, abs(threshold) * NEGLIGIBLE_GROWTH):
        return None
    return max(0.0, (threshold - prediction.base) / prediction.growth)


def _check(
    metric: str, container: str | None, lines: tuple[Line, ...], prediction: Prediction | None
) -> Check:
    if prediction is None:
        return Check(metric, container, lines, None, NO_DATA, None, None)
    if not lines:
        return Check(metric, container, lines, prediction, NO_LIMIT, None, None)
    status, crossed = evaluate(prediction.value, lines)
    critical = [line.value for line in lines if line.critical]
    reached = reached_at(prediction, min(critical)) if critical else None
    return Check(metric, container, lines, prediction, status, crossed, reached)


def resource_checks(
    records: Sequence[Record],
    fleet: Mapping[str, float],
    selected: Sequence[str],
    limits: Mapping[str, Mapping[str, float]],
) -> list[Check]:
    """Each container's predicted memory and CPU peak against its limit.

    A resource that no run measured for a container has no check: a container
    that reports only restarts, such as an init container, has no data for one.
    """
    measured = {
        (measurement.name, measurement.container)
        for record in records
        for measurement in record.metrics
    }
    checks = []
    for container in selected:
        for metric, key in CONTAINER_LIMIT_KEYS.items():
            if (metric, container) not in measured:
                continue
            limit = limits.get(container, {}).get(key)
            lines = (
                (Line(limit, LIMIT_LABEL.format(container=container)),)
                if limit and limit > 0
                else ()
            )
            checks.append(_check(metric, container, lines, predict(records, metric, container, fleet)))
    return checks


def capacity_lines(records: Iterable[Record], metric: str) -> tuple[Line, ...]:
    """The threshold a run measures itself, such as its PVC capacity."""
    source = CAPACITY_METRICS.get(metric)
    if source is None:
        return ()
    readings = [
        (record.started, value)
        for record in records
        if (value := record.value(source)) is not None
    ]
    if not readings:
        return ()
    capacity = max(readings)[1]
    return (Line(capacity, PVC_CAPACITY_LABEL.format(capacity=format_value(metric, capacity))),)


def gated_operations(metric: str) -> tuple[str, ...] | None:
    """The operations a metric's threshold applies to; None for all of them."""
    return QUERY_OPERATIONS if metric in QUERY_METRICS else None


def threshold_lines(
    records: Iterable[Record], metric: str, operation: str | None = None
) -> tuple[Line, ...]:
    """A metric's thresholds, in one operation's chart or, without one, for its check."""
    gated = gated_operations(metric)
    if gated is not None and operation is not None and operation not in gated:
        return ()
    return FIXED_LINES.get(metric, ()) or capacity_lines(records, metric)


def threshold_checks(
    records: Sequence[Record], fleet: Mapping[str, float], selected: Sequence[str]
) -> list[Check]:
    """Every metric with a fixed or measured threshold, per selected container where it has one."""
    checks = []
    for metric in metric_names(records):
        lines = threshold_lines(records, metric)
        if not lines:
            continue
        gated = gated_operations(metric)
        owners = {
            measurement.container
            for record in records
            if gated is None or record.operation in gated
            for measurement in record.metrics
            if measurement.name == metric
        }
        for owner in [None, *selected]:
            if owner in owners:
                prediction = predict(records, metric, owner, fleet, only=gated)
                checks.append(_check(metric, owner, lines, prediction))
    return checks


def past_at_base(check: Check) -> bool:
    """Whether the base point itself is past the check's threshold.

    ``reached == 0`` cannot tell this apart: a count that is exactly at its
    threshold at the base (0 against "above 0") also reaches it at once when
    the fleet grows, yet passes at the base.
    """
    if check.prediction is None or not check.lines:
        return False
    return evaluate(check.prediction.base, check.lines)[0] != PASS


def first_limit(checks: Iterable[Check]) -> Check | None:
    """The check the fleet reaches first as it grows from the base point."""
    reached = [check for check in checks if check.reached is not None]
    if not reached:
        return None
    return min(reached, key=lambda check: (check.reached, check.status != FAIL, check.label))


def headroom(check: Check) -> float | None:
    if check.prediction is None or not check.lines:
        return None
    return check.lines[0].value - check.prediction.value


# --- capacity ---------------------------------------------------------------


@dataclass(frozen=True)
class Capacity:
    """Where one check first fails as an input grows from the base point."""

    check: Check
    # The input's value at the first failure; None when the check passes up to the ceiling.
    value: float | None
    # True when the base point itself fails the check.
    at_base: bool = False


def all_checks(
    records: Sequence[Record],
    fleet: Mapping[str, float],
    containers: Sequence[str],
    limits: Mapping[str, Mapping[str, float]],
) -> list[Check]:
    return resource_checks(records, fleet, containers, limits) + threshold_checks(
        records, fleet, containers
    )


def scaled_fleet(base: Mapping[str, float], clusters: float) -> dict[str, float]:
    """The base fleet with ``clusters`` clusters of the same shape.

    Per-cluster inputs (nodes, objects) and the request-side inputs stay; the
    fleet-wide inputs (pod churn, Pods failing at once) grow with the clusters.
    """
    ratio = clusters / base[CLUSTERS]
    fleet = dict(base)
    fleet[CLUSTERS] = clusters
    for factor in FLEET_WIDE_FACTORS:
        fleet[factor] = base[factor] * ratio
    return fleet


def capacities(
    records: Sequence[Record],
    containers: Sequence[str],
    limits: Mapping[str, Mapping[str, float]],
    fleet_at: Callable[[float], Mapping[str, float]],
    start: float,
    ceiling: float,
) -> list[Capacity]:
    """For every check, the smallest input in [start, ceiling] at which it fails.

    ``fleet_at`` maps the input to a whole fleet. The search assumes a check
    that fails stays failed as the input grows, which holds for the straight
    lines the predictions are made of when they rise.
    """
    bottom = {
        (check.metric, check.container): check
        for check in all_checks(records, fleet_at(start), containers, limits)
    }
    results = []
    for top in all_checks(records, fleet_at(ceiling), containers, limits):
        first = bottom[(top.metric, top.container)]
        if past_at_base(first):
            results.append(Capacity(first, start, at_base=True))
            continue
        if top.status != FAIL:
            results.append(Capacity(top, None))
            continue
        only = gated_operations(top.metric)

        def check_at(x: float, top: Check = top, only: tuple[str, ...] | None = only) -> Check:
            prediction = predict(records, top.metric, top.container, fleet_at(x), only=only)
            return _check(top.metric, top.container, top.lines, prediction)

        low, high = start, ceiling
        for _ in range(CAPACITY_STEPS):
            middle = (low + high) / 2
            if check_at(middle).status == FAIL:
                high = middle
            else:
                low = middle
        results.append(Capacity(check_at(high), high))
    return sorted(
        results,
        key=lambda item: (not item.at_base, item.value is None, item.value or 0.0, item.check.label),
    )


def cluster_capacities(
    records: Sequence[Record],
    containers: Sequence[str],
    limits: Mapping[str, Mapping[str, float]],
    base: Mapping[str, float] | None = None,
) -> list[Capacity]:
    """Each check's breaking point in clusters, for a fleet of clusters shaped like the base."""
    base = dict(base or base_fleet(records))
    return capacities(
        records,
        containers,
        limits,
        lambda clusters: scaled_fleet(base, clusters),
        base[CLUSTERS],
        base[CLUSTERS] * CAPACITY_CEILING,
    )


def factor_capacities(
    records: Sequence[Record],
    containers: Sequence[str],
    limits: Mapping[str, Mapping[str, float]],
    factor: str,
    base: Mapping[str, float] | None = None,
) -> list[Capacity]:
    """Each check's breaking point in one factor, with every other factor at the base."""
    base = dict(base or base_fleet(records))
    return capacities(
        records,
        containers,
        limits,
        lambda value: {**base, factor: value},
        base[factor],
        max(base[factor], 1.0) * CAPACITY_CEILING,
    )


@dataclass(frozen=True)
class Onset:
    """Where a count that has to stay at zero was first measured above it, along one factor."""

    metric: str
    container: str | None
    factor: str
    # The largest measured point below the onset, where every run counted zero.
    last_zero: float | None
    # The smallest measured point where a run counted above zero, and how many of its runs did.
    first_above: float
    runs_above: int
    runs: int

    @property
    def label(self) -> str:
        name = metric_spec(self.metric).label
        return f"{name} · {self.container}" if self.container else name


def count_onsets(records: Sequence[Record], factor: str) -> list[Onset]:
    """The measured onset of every count whose threshold is "above 0", along ``factor``.

    A straight line through counts that stay at zero up to a queue's capacity
    and then jump crosses zero at once, so these are reported as measured:
    the last point at zero and the first above it, with no line between them.
    """
    counts = [metric for metric, lines in FIXED_LINES.items() if lines == ABOVE_ZERO]
    onsets = []
    for metric in counts:
        owners = sorted(
            {m.container for record in records for m in record.metrics if m.name == metric},
            key=lambda owner: owner or "",
        )
        for owner in owners:
            groups: dict[str, list[tuple[float, float]]] = {}
            for record in records:
                if not (record.is_base or record.factor == factor):
                    continue
                y = record.value(metric, owner)
                x = factor_value(record, factor)
                if x is not None and y is not None:
                    groups.setdefault(BASE_LEVEL if record.is_base else f"{record.level:g}", []).append((x, y))
            ordered = sorted(groups.values(), key=lambda runs: _mean([x for x, _ in runs]))
            above = next((index for index, runs in enumerate(ordered) if any(y > 0 for _, y in runs)), None)
            if above is None:
                continue
            runs = ordered[above]
            onsets.append(
                Onset(
                    metric,
                    owner,
                    factor,
                    _mean([x for x, _ in ordered[above - 1]]) if above else None,
                    _mean([x for x, _ in runs]),
                    sum(1 for _, y in runs if y > 0),
                    len(runs),
                )
            )
    return onsets


# --- the credential proxy's capacity --------------------------------------


@dataclass(frozen=True)
class ProxyLevel:
    """The proxy load runs at one worker count, averaged."""

    workers: float
    runs: int
    per_second: float
    p50_seconds: float | None
    p95_seconds: float | None
    errors: float
    # The largest CPU peaks over the runs: the broker's, and the sandbox shell's, where the
    # client of every brokered command runs.
    cpu_cores: float | None
    shell_cpu_cores: float | None = None
    # The broker's largest median admission wait: near zero while its slots are free.
    wait_p50_seconds: float | None = None


@dataclass(frozen=True)
class ProxyDemand:
    """The brokered commands one operation sent at one point, averaged over its runs."""

    operation: str
    factor: str
    level: float
    clusters: float
    runs: int
    commands_per_minute: float
    # Seconds a command ran for, on average.
    seconds_per_command: float | None

    @property
    def base_shaped(self) -> bool:
        """Whether the point differs from the base in its cluster count alone."""
        return self.factor in (BASE_FACTOR, CLUSTERS)

    @property
    def per_cluster_per_minute(self) -> float | None:
        """The demand of one cluster, for a point whose clusters are shaped like the base's."""
        return self.commands_per_minute / self.clusters if self.clusters and self.base_shaped else None


def _optional_mean(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return _mean(present) if present else None


def _minutes(record: Record) -> float:
    started = datetime.strptime(record.started, TIMESTAMP_FORMAT)
    ended = datetime.strptime(record.ended, TIMESTAMP_FORMAT)
    return max((ended - started).total_seconds(), 1.0) / SECONDS_PER_MINUTE


@dataclass(frozen=True)
class CpuRun:
    """The CPU of one container in an operation's runs at one cluster count and one CPU limit:
    the highest peak, whether the limit capped any run, and the mean average."""

    operation: str
    clusters: float
    limit: float | None
    runs: int
    peak: float | None
    capped: bool
    # CPU-seconds over the window divided by the window: the work, which keeps growing after the
    # peak stops at the limit.
    average: float | None


def cpu_runs(records: Iterable[Record], container: str) -> list[CpuRun]:
    """``container``'s CPU, grouped by operation, cluster count and CPU limit."""
    groups: dict[tuple[str, float, float], list[Record]] = {}
    for record in records:
        clusters = record.totals.get(CLUSTERS)
        if record.value(CPU_PEAK, container) is None and record.value(CPU_SECONDS, container) is None:
            continue
        if not clusters:
            continue
        limit = record.limits.get(container, {}).get(LIMIT_CPU) or 0.0
        groups.setdefault((record.operation, clusters, limit), []).append(record)
    rows = []
    for (operation, clusters, limit), runs in sorted(groups.items()):
        peaks = [value for run in runs if (value := run.value(CPU_PEAK, container)) is not None]
        averages = [
            seconds / (_minutes(run) * SECONDS_PER_MINUTE)
            for run in runs
            if (seconds := run.value(CPU_SECONDS, container)) is not None
        ]
        rows.append(
            CpuRun(
                operation,
                clusters,
                limit or None,
                len(runs),
                max(peaks) if peaks else None,
                any(capped(run, CPU_PEAK, container) for run in runs),
                _mean(averages) if averages else None,
            )
        )
    return rows


def proxy_levels(records: Iterable[Record]) -> list[ProxyLevel]:
    """The proxy load runs, one row per worker count, in rising order."""
    groups: dict[float, list[Record]] = {}
    for record in records:
        workers = factor_value(record, PROXY_CONCURRENCY)
        if record.value(PROXY_LOAD_RATE) is not None and workers is not None:
            groups.setdefault(workers, []).append(record)
    levels = []
    for workers, runs in sorted(groups.items()):
        peaks = [value for run in runs if (value := run.value(CPU_PEAK, PROXY_CONTAINER)) is not None]
        shell = [value for run in runs if (value := run.value(CPU_PEAK, SHELL_CONTAINER)) is not None]
        waits = [value for run in runs if (value := run.value(PROXY_WAIT_P50)) is not None]
        levels.append(
            ProxyLevel(
                workers,
                len(runs),
                _mean([run.value(PROXY_LOAD_RATE) for run in runs]),
                _optional_mean([run.value(PROXY_LOAD_P50) for run in runs]),
                _optional_mean([run.value(PROXY_LOAD_P95) for run in runs]),
                sum(run.value(PROXY_LOAD_ERRORS) or 0.0 for run in runs),
                max(peaks) if peaks else None,
                max(shell) if shell else None,
                max(waits) if waits else None,
            )
        )
    return levels


def proxy_demand(records: Iterable[Record]) -> list[ProxyDemand]:
    """Brokered commands per minute by operation and point, the proxy load runs aside.

    Points are kept apart: ten concurrent questions on two clusters are no
    measure of what one cluster asks of the broker.
    """
    groups: dict[tuple[str, str, float], list[Record]] = {}
    for record in records:
        if record.operation == PROXY_LOAD_OPERATION or record.value(PROXY_COMMANDS) is None:
            continue
        if not record.totals.get(CLUSTERS):
            continue
        groups.setdefault((record.operation, record.factor, record.level), []).append(record)
    demand = []
    for (operation, factor, level), runs in sorted(groups.items()):
        clusters = _mean([run.totals[CLUSTERS] for run in runs])
        commands = sum(run.value(PROXY_COMMANDS) or 0.0 for run in runs)
        timed = [value for run in runs if (value := run.value(PROXY_COMMAND_SECONDS)) is not None]
        minutes = sum(_minutes(run) for run in runs)
        demand.append(
            ProxyDemand(
                operation,
                factor,
                level,
                clusters,
                len(runs),
                commands / minutes,
                sum(timed) / commands if timed and commands else None,
            )
        )
    return demand


# --- recommendations --------------------------------------------------------


def with_margin(value: float) -> float:
    return value * (1 + SAFETY_MARGIN)


def _recommended(prediction: Prediction | None) -> float | None:
    if prediction is None or prediction.value <= 0:
        return None
    return with_margin(prediction.value)


def recommendations(
    records: Sequence[Record], fleet: Mapping[str, float], selected: Sequence[str]
) -> list[Recommendation]:
    return [
        Recommendation(
            container,
            predict(records, MEMORY_PEAK, container, fleet),
            predict(records, CPU_PEAK, container, fleet),
        )
        for container in selected
    ]


def latest_replicas(records: Iterable[Record]) -> dict[str, float]:
    """Per container, the replica count of the latest run that counted it."""
    replicas: dict[str, float] = {}
    for record in sorted(records, key=lambda item: (item.started, item.key)):
        replicas.update(record.replicas)
    return replicas


def pod_of(container: str) -> str:
    """The pod a container runs in: the gateway pod, or a pod of its own."""
    return next(
        (pod for pod, members in POD_CONTAINERS.items() if container in members), container
    )


def node_plan(items: Iterable[Recommendation], replicas: Mapping[str, float]) -> NodePlan:
    """The recommendations grouped into pods, each with its replica count.

    A container with no recommended memory or CPU, such as one that reports
    only restarts, requests nothing and is left out.
    """
    grouped: dict[str, list[Recommendation]] = {}
    for item in items:
        if item.memory_bytes is None and item.cpu_cores is None:
            continue
        # A Job's or an init container's peak is real but holds no node once it finishes.
        if item.container in SHORT_LIVED_CONTAINERS:
            continue
        grouped.setdefault(pod_of(item.container), []).append(item)
    return NodePlan(
        tuple(
            PodRequest(
                name,
                tuple(item.container for item in members),
                max(int(replicas.get(item.container, DEFAULT_REPLICAS)) for item in members),
                sum(item.memory_bytes or 0.0 for item in members),
                sum(item.cpu_cores or 0.0 for item in members),
            )
            for name, members in grouped.items()
        )
    )


def fits(machine: MachineType, memory_bytes: float, cpu_cores: float) -> bool:
    return (
        machine.allocatable_memory_bytes >= memory_bytes and machine.allocatable_cpu >= cpu_cores
    )


def smallest_machine(memory_bytes: float, cpu_cores: float) -> MachineType | None:
    """The first machine type whose allocatable memory and CPU hold one pod's requests."""
    return next(
        (machine for machine in MACHINE_TYPES if fits(machine, memory_bytes, cpu_cores)), None
    )


def nodes_needed(machine: MachineType, memory_bytes: float, cpu_cores: float) -> int:
    """The nodes of one machine type that the requests need, per resource; the larger count."""
    by_memory = math.ceil(memory_bytes / machine.allocatable_memory_bytes - EPSILON)
    by_cpu = math.ceil(cpu_cores / machine.allocatable_cpu - EPSILON)
    return max(1, by_memory, by_cpu)


def memory_quantity(value_bytes: float) -> str:
    return f"{math.ceil(value_bytes / MIB - EPSILON)}Mi"


def cpu_quantity(cores: float) -> str:
    return f"{math.ceil(cores * MILLICORES_PER_CORE - EPSILON)}m"


def storage_quantity(value_bytes: float) -> str:
    return f"{math.ceil(value_bytes / GIB - EPSILON)}Gi"


# --- formatting and table rows ----------------------------------------------


def _number_text(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= WHOLE_NUMBER_FROM:
        return f"{value:,.0f}"
    if magnitude >= 1:
        return f"{value:,.1f}"
    return f"{value:.2f}"


def _axis_text(value: float) -> str:
    """A factor's value: a count shows no decimals, an average one."""
    if abs(value - round(value)) <= EPSILON:
        return f"{round(value):,}"
    return f"{value:,.1f}"


def format_value(metric: str, value: float | None) -> str:
    if value is None:
        return NO_VALUE
    spec = metric_spec(metric)
    text = _number_text(value / spec.scale)
    return f"{text} {spec.unit}" if spec.unit else text


def percent(share: float | None) -> str:
    if share is None:
        return NO_VALUE
    if share > REACHED_DISPLAY_CAP:
        return BEYOND_CAP_TEXT
    return f"{share:.0%}"


def resource_rows(checks: Iterable[Check]) -> list[dict[str, str]]:
    rows = []
    for check in checks:
        room = headroom(check)
        room_text = NO_VALUE
        if room is not None:
            room_text = f"{format_value(check.metric, room)} ({room / check.lines[0].value:.0%})"
        rows.append(
            {
                "Container": check.container or NO_VALUE,
                "Resource": metric_spec(check.metric).label,
                "Predicted peak": format_value(
                    check.metric, check.prediction.value if check.prediction else None
                ),
                "Limit": format_value(
                    check.metric, check.lines[0].value if check.lines else None
                ),
                "Headroom": room_text,
                "Status": STATUS_LABELS[check.status],
                "Reached at": percent(check.reached),
                "Basis": basis(check.prediction),
            }
        )
    return rows


def threshold_rows(checks: Iterable[Check]) -> list[dict[str, str]]:
    return [
        {
            "Check": check.name,
            "Container": check.container or NO_VALUE,
            "Predicted": format_value(
                check.metric, check.prediction.value if check.prediction else None
            ),
            "Threshold": "; ".join(line.label for line in check.lines),
            "Status": STATUS_LABELS[check.status],
            "Reached at": percent(check.reached),
            "Basis": basis(check.prediction),
        }
        for check in checks
    ]


def contribution_rows(checks: Iterable[Check]) -> list[dict[str, str]]:
    """How each prediction is built: its base value and every factor that grows."""
    rows = []
    for check in checks:
        prediction = check.prediction
        if prediction is None:
            continue
        rows.append(
            {
                "Check": check.label,
                "Factor": BASE_VALUE_LABEL,
                "Operation": prediction.base_operation,
                "Base": NO_VALUE,
                "This fleet": NO_VALUE,
                "Measured": NO_VALUE,
                "Value": format_value(check.metric, prediction.base),
                "Fit": NO_VALUE,
            }
        )
        for item in prediction.contributions:
            if not item.grows:
                continue
            rows.append(
                {
                    "Check": check.label,
                    "Factor": FACTORS[item.factor].input_label
                    if item.factor in FACTORS
                    else item.factor,
                    "Operation": item.operation,
                    "Base": _optional_number(item.base_x),
                    "This fleet": _optional_number(item.fleet_x),
                    "Measured": (
                        f"{_axis_text(item.fit.x_min)}–{_axis_text(item.fit.x_max)}"
                        if item.fit
                        else NO_VALUE
                    ),
                    "Value": f"+{format_value(check.metric, item.delta)}"
                    if item.delta >= 0
                    else format_value(check.metric, item.delta),
                    "Fit": item.fit.label if item.fit else NO_FIT_LABEL,
                }
            )
    return rows


def _optional_number(value: float | None) -> str:
    return NO_VALUE if value is None else _axis_text(value)


def recommendation_rows(items: Iterable[Recommendation]) -> list[dict[str, str]]:
    rows = []
    for item in items:
        flags = dict.fromkeys(
            flag
            for prediction in (item.memory, item.cpu)
            if prediction is not None
            for flag in basis_flags(prediction)
        )
        rows.append(
            {
                "Container": item.container,
                "Field": item.field,
                "Predicted memory peak": format_value(
                    MEMORY_PEAK, item.memory.value if item.memory else None
                ),
                "Memory limit": memory_quantity(item.memory_bytes)
                if item.memory_bytes
                else NO_VALUE,
                "Predicted CPU peak": format_value(
                    CPU_PEAK, item.cpu.value if item.cpu else None
                ),
                "CPU limit": cpu_quantity(item.cpu_cores) if item.cpu_cores else NO_VALUE,
                "Basis": "; ".join(flags) or NO_VALUE,
            }
        )
    return rows


def _request_text(memory_bytes: float, cpu_cores: float) -> tuple[str, str]:
    return (
        memory_quantity(memory_bytes) if memory_bytes > 0 else NO_VALUE,
        cpu_quantity(cpu_cores) if cpu_cores > 0 else NO_VALUE,
    )


def pod_rows(plan: NodePlan) -> list[dict[str, str]]:
    """Each pod's recommended requests, the inputs of the node answer."""
    rows = []
    for pod in plan.pods:
        memory, cpu = _request_text(pod.memory_bytes, pod.cpu_cores)
        rows.append(
            {
                "Pod": pod.name,
                "Containers": ", ".join(pod.containers),
                "Replicas": str(pod.replicas),
                "Memory request": memory,
                "CPU request": cpu,
            }
        )
    return rows


def machine_rows(plan: NodePlan) -> list[dict[str, str]]:
    rows = []
    for machine in MACHINE_TYPES:
        holds = fits(machine, plan.largest_memory_bytes, plan.largest_cpu_cores)
        nodes = nodes_needed(machine, plan.total_memory_bytes, plan.total_cpu_cores)
        rows.append(
            {
                "Machine type": machine.name,
                "vCPU": str(machine.vcpu),
                "Memory": f"{machine.memory_gib:g} GiB",
                "Allocatable memory": f"{machine.allocatable_memory_gib:g} GiB",
                "Allocatable CPU": f"{machine.allocatable_cpu:g}",
                "Holds the largest pod": YES_LABEL if holds else NO_LABEL,
                "Nodes for every pod": str(nodes) if holds else NO_VALUE,
                "Why it is on the list": machine.reason,
            }
        )
    return rows


def unfixable_rows(checks: Iterable[Check]) -> list[dict[str, str]]:
    return [
        {
            "Check": check.name,
            "Predicted": format_value(
                check.metric, check.prediction.value if check.prediction else None
            ),
            "Status": STATUS_LABELS[check.status],
            "What sets it": UNFIXABLE[check.metric],
        }
        for check in checks
        if check.metric in UNFIXABLE
    ]


def combined_rows(records: Sequence[Record], containers: Iterable[str]) -> list[dict[str, str]]:
    """Each combined point's measured peaks against the page's prediction for its fleet.

    A prediction adds the factors one by one, and a combined point moves several
    at once, so a measured peak above the prediction is an interaction the sum
    misses. The prediction is made from every run except the combined ones.
    """
    single = [record for record in records if record.factor != COMBINED]
    base_values = next((record.values for record in single if record.is_base and record.values), {})
    rows = []
    for level in sorted({record.level for record in records if record.factor == COMBINED}):
        runs = [record for record in records if record.factor == COMBINED and record.level == level]
        fleet = base_fleet(single)
        moved = [
            factor
            for factor in FACTORS
            if factor in runs[0].values and runs[0].values[factor] != base_values.get(factor)
        ]
        for factor in moved:
            values = [value for record in runs if (value := factor_value(record, factor)) is not None]
            if values:
                fleet[factor] = _mean(values)
        point = ", ".join(f"{FACTORS[factor].input_label} {_number_text(fleet[factor])}" for factor in moved)
        for metric in (MEMORY_PEAK, CPU_PEAK):
            for container in containers:
                measured = [
                    value for record in runs if (value := record.value(metric, container)) is not None
                ]
                if not measured:
                    continue
                # The operations the point ran, so the prediction and the peak measure the same work.
                prediction = predict(single, metric, container, fleet, only=sorted({r.operation for r in runs}))
                predicted = prediction.value if prediction is not None else None
                rows.append(
                    {
                        "Point": point,
                        "Container": container,
                        "Metric": METRICS[metric].label,
                        "Measured (largest run)": format_value(metric, max(measured)),
                        "Predicted": format_value(metric, predicted),
                        "Measured ÷ predicted": (
                            f"{max(measured) / predicted:.2f}" if predicted else NO_VALUE
                        ),
                    }
                )
    return rows


def record_rows(records: Iterable[Record]) -> list[dict[str, Any]]:
    return [
        {
            "Key": record.key,
            "Commit": record.commit[:COMMIT_LABEL_LENGTH],
            "Mode": record.mode,
            "Phase": record.phase,
            "Factor": record.factor,
            "Level": record.level,
            "Operation": record.operation,
            "Run": record.run,
            "Started": record.started,
            "Ended": record.ended,
            "Image": record.image,
            "Errors": "; ".join(record.errors),
            "Provider 429s": record.provider_429s,
            "Excluded from": ", ".join(record.excluded_from),
            "Values": _pairs(record.values),
            "Totals": _pairs(record.totals),
            "Limits": "; ".join(
                f"{container}: {_pairs(values)}" for container, values in record.limits.items()
            ),
            "Replicas": _pairs(record.replicas),
        }
        for record in records
    ]


def _pairs(values: Mapping[str, float]) -> str:
    return ", ".join(f"{name}={value:g}" for name, value in values.items())


def measurement_rows(records: Iterable[Record]) -> list[dict[str, Any]]:
    return [
        {
            "Key": record.key,
            "Factor": record.factor,
            "Level": record.level,
            "Operation": record.operation,
            "Run": record.run,
            "Metric": measurement.name,
            "Container": measurement.container or "",
            "Value": measurement.value,
            "Shown as": format_value(measurement.name, measurement.value),
            "Excluded from fit": record.excluded(measurement.name),
        }
        for record in records
        for measurement in record.metrics
    ]


# --- charts -----------------------------------------------------------------


def metric_charts(
    records: Sequence[Record],
    operation: str,
    factor: str,
    selected: Sequence[str],
    fleet: Mapping[str, float],
) -> list[MetricChart]:
    """One chart per metric the factor's runs of the operation measured."""
    fleet_x = fleet_value(factor, fleet)
    limits = latest_limits(records)
    charts = []
    for metric in metric_names(records):
        owners = {
            measurement.container
            for record in records
            if record.operation == operation and record.factor == factor
            for measurement in record.metrics
            if measurement.name == metric
        }
        series = []
        for owner in [None, *selected]:
            if owner not in owners:
                continue
            points = series_points(records, operation, factor, metric, owner)
            if all(point.is_base for point in points):
                continue
            limit_key = CONTAINER_LIMIT_KEYS.get(metric)
            limit = limits.get(owner, {}).get(limit_key) if owner and limit_key else None
            series.append(
                ChartSeries(
                    owner or metric_spec(metric).label, owner, points, fit_line(points), limit
                )
            )
        if not series:
            continue
        largest = max(point.x for item in series for point in item.points)
        metric_info = metric_spec(metric)
        charts.append(
            MetricChart(
                metric=metric,
                factor=factor,
                operation=operation,
                title=metric_info.label,
                x_title=factor_label(factor),
                y_title=f"{metric_info.label} ({metric_info.unit})"
                if metric_info.unit
                else metric_info.label,
                unit=metric_info.unit,
                scale=metric_info.scale,
                series=tuple(series),
                lines=threshold_lines(records, metric, operation),
                x_end=max(largest * CHART_EXTENSION, fleet_x or 0.0),
                fleet_x=fleet_x,
            )
        )
    return charts
