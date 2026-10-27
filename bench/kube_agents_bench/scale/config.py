"""The measurement plan: target, factors, levels and operations, read from YAML.

A phase is one plan file (``bench/scale/phase1.yaml``); a later phase is another
file of the same shape, so repeating or extending the measurement changes data,
not code. Every target value can be overridden from the environment, so the same
plan runs against any project.
"""

from __future__ import annotations

import itertools
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kube_agents_bench.scale import kube

BASE_FACTOR = "base"
# A factor whose levels are points that move several factors at once: level N is the plan's
# Nth ``combined`` point (1-based). It is listed under ``factors`` like any other, so the
# plan's order decides when it runs.
COMBINED_FACTOR = "combined"
ENV_PREFIX = "SCALE_"
PROXY_LOAD_KIND = "proxy_load"
TRIGGER_SOAK_KIND = "trigger_soak"
TARGET_KEYS = (
    "project_id",
    "host_context",
    "host_cluster",
    "host_location",
    "namespace",
    "zone",
    "cluster_prefix",
    "cluster_label",
    "mode",
    "data_file",
    "preflight",
)
REQUIRED_TARGET_KEYS = ("project_id", "host_cluster", "host_location", "namespace", "zone", "cluster_prefix", "cluster_label", "mode")

DEFAULT_RUNS = 1
# Settings the runner reads without a default; a plan that lacks one is refused at load, before
# any level touches a cluster.
REQUIRED_SETTINGS = ("churn_pod_lifetime_seconds", "fixture_machine_type", "fixture_pool", "pods_per_node")


@dataclass(frozen=True)
class Operation:
    name: str
    kind: str
    factors: tuple[str, ...]
    runs: int = DEFAULT_RUNS
    prompt: str = ""
    follow_up: str = ""
    window_seconds: int = 0
    timeout_seconds: int = 0
    # proxy_load: the command each worker repeats, as an argv after the CLI name.
    command: tuple[str, ...] = ()
    # trigger_soak: the cron jobs marked due at the window's start, and the home of the
    # profile whose roster holds them.
    jobs: tuple[str, ...] = ()
    jobs_home: str = ""


@dataclass(frozen=True)
class RunSpec:
    factor: str
    level: float
    operation: Operation
    run: int


@dataclass
class Plan:
    phase: str
    target: dict[str, str]
    base: dict[str, float]
    factors: dict[str, list[float]]
    operations: list[Operation]
    settings: dict[str, Any] = field(default_factory=dict)
    combined: list[dict[str, float]] = field(default_factory=list)

    def level_values(self, factor: str, level: float) -> dict[str, float]:
        """Every factor's value at one point: the base, with ``factor`` set to ``level``,
        or with the level's point applied when ``factor`` is the combined factor."""
        values = dict(self.base)
        if factor == COMBINED_FACTOR:
            values.update(self.combined[int(level) - 1])
        elif factor != BASE_FACTOR:
            values[factor] = level
        return values

    def matrix(self) -> list[RunSpec]:
        """Every run the plan asks for, base first, in factor and level order.

        The base point runs every operation once per configured run; each
        factor's other levels run only the operations that list the factor.
        """
        specs: list[RunSpec] = []
        for operation in self.operations:
            for run in range(1, operation.runs + 1):
                specs.append(RunSpec(BASE_FACTOR, 0, operation, run))
        for factor, levels in self.factors.items():
            for level, operation in itertools.product(levels, self.operations):
                if level == self.base.get(factor) or factor not in operation.factors:
                    continue
                for run in range(1, operation.runs + 1):
                    specs.append(RunSpec(factor, level, operation, run))
        return specs


def load_plan(path: Path, environ: dict[str, str] | None = None, validate_target: bool = True) -> Plan:
    environ = dict(os.environ) if environ is None else environ
    raw = yaml.safe_load(path.read_text())
    target = {key: str(value) for key, value in (raw.get("target") or {}).items()}
    for key in TARGET_KEYS:
        override = environ.get(ENV_PREFIX + key.upper())
        if override:
            target[key] = override
    missing = [key for key in REQUIRED_TARGET_KEYS if not target.get(key)] if validate_target else []
    if missing:
        names = ", ".join(ENV_PREFIX + key.upper() for key in missing)
        raise ValueError(f"{path}: target is missing {', '.join(missing)}; set {names}")
    expected = ""
    if validate_target:
        expected = kube.context_name(target["project_id"], target["host_location"], target["host_cluster"])
        target.setdefault("host_context", expected)
    # The runner resets the install this context reaches; refuse any context but the host cluster's.
    if validate_target and target["host_context"] != expected:
        raise ValueError(f"{path}: host_context {target['host_context']!r} is not the host cluster's context "
                         f"{expected!r}")
    operations = [
        Operation(
            name=name,
            kind=spec["kind"],
            factors=tuple(spec.get("factors") or ()),
            runs=int(spec.get("runs", DEFAULT_RUNS)),
            prompt=spec.get("prompt", ""),
            follow_up=spec.get("follow_up", ""),
            window_seconds=int(spec.get("window_seconds", 0)),
            timeout_seconds=int(spec.get("timeout_seconds", 0)),
            command=tuple(spec.get("command") or ()),
            jobs=tuple(spec.get("jobs") or ()),
            jobs_home=str(spec.get("jobs_home") or ""),
        )
        for name, spec in (raw.get("operations") or {}).items()
    ]
    factors = {name: [float(level) for level in levels] for name, levels in raw["factors"].items()}
    for op, spec in zip(operations, (raw.get("operations") or {}).values()):
        command = spec.get("command")
        if op.kind == PROXY_LOAD_KIND and not (
            isinstance(command, list) and command and all(isinstance(word, str) for word in command)
            and op.window_seconds > 0
        ):
            raise ValueError(f"{path}: operation {op.name} needs a command (a list of words) and window_seconds")
        jobs = spec.get("jobs")
        if op.kind == TRIGGER_SOAK_KIND and not (
            isinstance(jobs, list) and jobs and all(isinstance(job, str) for job in jobs)
            and op.jobs_home and op.window_seconds > 0
        ):
            raise ValueError(f"{path}: operation {op.name} needs jobs (a list of job ids), jobs_home and window_seconds")
    unknown = sorted({f for op in operations for f in op.factors} - set(factors))
    if unknown:
        raise ValueError(f"{path}: operations name unknown factors {unknown}")
    settings = raw.get("settings") or {}
    absent = [name for name in REQUIRED_SETTINGS if name not in settings]
    if absent:
        raise ValueError(f"{path}: settings is missing {', '.join(absent)}")
    combined = [{name: float(value) for name, value in point.items()} for point in raw.get("combined") or []]
    points = range(1, len(combined) + 1)
    bad = [level for level in factors.get(COMBINED_FACTOR, []) if level not in points]
    if bad:
        raise ValueError(f"{path}: combined levels {bad} name no point; the plan has {len(combined)}")
    strays = sorted({name for point in combined for name in point} - set(raw["base"]))
    if strays:
        raise ValueError(f"{path}: combined points set factors the base does not have: {strays}")
    return Plan(
        phase=str(raw["phase"]),
        target=target,
        base={name: float(value) for name, value in raw["base"].items()},
        factors=factors,
        operations=operations,
        settings=settings,
        combined=combined,
    )
