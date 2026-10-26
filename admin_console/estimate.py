"""Print the harness's breaking points from the measured data, as Markdown.

    python -m admin_console.estimate [--data FILE] [--commit SHA] [--mode MODE]
                                     [--against limits|requests | --limits FILE]

The same model as the console's Harness limits page (``harness_limits``): for
each check, the fleet size at which it first fails, for a fleet of clusters
shaped like the base point and for each factor alone; then the credential
proxy's throughput and the demand each operation put on it. The checks use the
limits of the latest run; ``--against requests`` uses its requests instead,
which is what an Autopilot cluster without bursting gives each container, and
``--limits`` reads a JSON or YAML mapping of container to ``memory_bytes`` and
``cpu_cores``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import yaml

from admin_console import harness_limits as hl

TABLE_SEPARATOR = " | "
NOT_REACHED = "not below {ceiling}"


def _table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    lines = [f"| {TABLE_SEPARATOR.join(headers)} |", f"|{'|'.join('---' for _ in headers)}|"]
    lines += [f"| {TABLE_SEPARATOR.join(row)} |" for row in rows]
    return "\n".join(lines)


def _number(value: float | None) -> str:
    return hl.NO_VALUE if value is None else hl._number_text(value)


def _threshold(check: hl.Check) -> str:
    critical = [line for line in check.lines if line.critical]
    if not critical:
        return hl.NO_VALUE
    line = min(critical, key=lambda item: item.value)
    return f"{hl.format_value(check.metric, line.value)} ({line.label})"


def capacity_table(items: Sequence[hl.Capacity], unit: str, ceiling: float) -> str:
    """The checks that pass at the base and fail below the ceiling, first failure first."""
    rows = [
        (
            item.check.label,
            _threshold(item.check),
            hl.format_value(item.check.metric, item.check.prediction.base if item.check.prediction else None),
            f"{hl._number_text(item.value)} {unit}",
            hl.basis(item.check.prediction),
        )
        for item in items
        # Counts that must stay at zero are reported as measured onsets, not as a line's crossing.
        if item.value is not None and not item.at_base and item.check.lines and item.check.lines != hl.ABOVE_ZERO
    ]
    if not rows:
        return f"No check fails below {hl._number_text(ceiling)} {unit}."
    return _table(("Check", "Threshold", "At the base", "Fails at", "Basis"), rows)


def proxy_section(records: Sequence[hl.Record]) -> str:
    levels = hl.proxy_levels(records)
    demand = hl.proxy_demand(records)
    parts = []
    if levels:
        parts.append(
            _table(
                ("Workers", "Runs", "Commands per second", "Median s", "95th percentile s", "Failed",
                 "Proxy CPU peak (cores)"),
                (
                    (_number(level.workers), str(level.runs), _number(level.per_second),
                     _number(level.p50_seconds), _number(level.p95_seconds), _number(level.errors),
                     _number(level.cpu_cores))
                    for level in levels
                ),
            )
        )
    saturation = max((level.per_second for level in levels), default=None)
    if demand:
        rows = []
        for item in demand:
            served = (
                saturation * hl.SECONDS_PER_MINUTE / item.per_cluster_per_minute
                if saturation and item.per_cluster_per_minute
                else None
            )
            rows.append(
                (item.operation, _number(item.clusters), str(item.runs), _number(item.commands_per_minute),
                 _number(item.per_cluster_per_minute), _number(item.seconds_per_command), _number(served))
            )
        parts.append(
            _table(
                ("Operation", "Clusters", "Runs", "Commands per minute", "Per cluster per minute",
                 "Seconds per command", "Clusters at the proxy's measured maximum"),
                rows,
            )
        )
    if not parts:
        return "No run recorded the credential proxy's commands."
    return "\n\n".join(parts)


def report(
    records: Sequence[hl.Record],
    limits: Mapping[str, Mapping[str, float]],
    title: str,
) -> str:
    containers = hl.containers(records)
    base = hl.base_fleet(records)
    sections = [f"# Harness breaking points: {title}"]
    shape = ", ".join(f"{hl.FACTORS[factor].input_label} {hl._number_text(value)}" for factor, value in base.items())
    sections.append(f"Base point: {shape}.")
    cluster_ceiling = base[hl.CLUSTERS] * hl.CAPACITY_CEILING
    shaped = hl.cluster_capacities(records, containers, limits, base)
    past = [item for item in shaped if item.at_base]
    if past:
        sections.append(
            "Past the threshold at the base point already: "
            + "; ".join(
                f"{item.check.label}, {hl.format_value(item.check.metric, item.check.prediction.base)} "
                f"against {_threshold(item.check)}"
                for item in past
            )
            + "."
        )
    sections.append(
        "## Clusters shaped like the base\n\n"
        "Pod churn and Pods failing at once grow with the clusters; every other input stays.\n\n"
        + capacity_table(shaped, "clusters", cluster_ceiling)
    )
    for factor in hl.factors(records):
        if factor not in hl.FACTORS:
            continue
        ceiling = max(base[factor], 1.0) * hl.CAPACITY_CEILING
        sections.append(
            f"## {hl.FACTORS[factor].input_label}, the other inputs at the base\n\n"
            + capacity_table(hl.factor_capacities(records, containers, limits, factor, base),
                             hl.FACTORS[factor].input_label.lower(), ceiling)
        )
    onsets = [onset for factor in hl.factors(records) if factor in hl.FACTORS
              for onset in hl.count_onsets(records, factor)]
    if onsets:
        sections.append(
            "## Counts that have to stay at zero, as measured\n\n"
            + _table(
                ("Count", "Factor", "Zero up to", "Above zero at", "Runs above zero"),
                (
                    (onset.label, hl.FACTORS[onset.factor].input_label, _number(onset.last_zero),
                     _number(onset.first_above), f"{onset.runs_above} of {onset.runs}")
                    for onset in onsets
                ),
            )
        )
    sections.append("## Credential proxy\n\n" + proxy_section(records))
    return "\n\n".join(sections) + "\n"


def read_limits(path: Path) -> dict[str, dict[str, float]]:
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected a mapping of container to memory_bytes and cpu_cores")
    return {str(name): {str(key): float(value) for key, value in (values or {}).items()} for name, values in raw.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=hl.DATA_PATH)
    parser.add_argument("--commit", help="the commit to report (default: the latest)")
    parser.add_argument("--mode", help=f"the mode to report (default: {hl.DEFAULT_MODE} when measured)")
    parser.add_argument("--against", choices=("limits", "requests"), default="limits",
                        help="check against the latest run's limits (default) or its requests")
    parser.add_argument("--limits", type=Path, help="JSON or YAML: container -> memory_bytes, cpu_cores")
    args = parser.parse_args(argv)
    dataset = hl.load_dataset(args.data)
    available = hl.commits(dataset.records)
    if not available:
        print(f"{hl.display_path(dataset.path)} has no records.", file=sys.stderr)
        return 1
    commit = next((item for item in available if args.commit and item.startswith(args.commit)), available[0])
    modes = hl.modes(dataset.records, commit)
    mode = args.mode or modes[hl.default_mode_index(modes)]
    records = hl.select(dataset.records, commit, mode)
    if args.limits:
        limits, against = read_limits(args.limits), f"limits from {args.limits}"
    elif args.against == "requests":
        limits, against = hl.latest_requests(records), "the latest run's requests"
        if not limits:
            print("No record of this commit and mode holds requests.", file=sys.stderr)
            return 1
    else:
        limits, against = hl.latest_limits(records), "the latest run's limits"
    title = f"{hl.commit_label(dataset.records, commit)}, mode {mode}, against {against}"
    if dataset.is_sample:
        title += " (sample data, not measured)"
    print(report(records, limits, title), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
