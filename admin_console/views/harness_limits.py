"""Harness limits: the measured load and sizing of the agent harness."""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import streamlit as st

from admin_console import harness_limits as hl
from admin_console.charts import bottleneck_chart, harness_limit_chart
from admin_console.connection_session import recover_app_shell
from admin_console.ui import PALETTE, section_title

KEY_PREFIX = "harness_limits"
DESIGN_DOC = "docs/designs/agent-harness-bottlenecks.md"
# The commit selector's label carries the image, so it takes the wider column.
SELECTOR_WIDTHS = (3, 1)
FLEET_COLUMNS = 4
CHART_COLUMNS = 2
FLEET_STEP = 1.0
# Shows 2 as "2" and 178.5 as "178.5".
NUMBER_FORMAT = "%.1f"
FLEET_DECIMALS = 1
CHART_CONFIG = {"displayModeBar": False}
PROFILE_CAPTION = (
    "{projects:,} projects, {autopilot:.0%} of clusters on Autopilot, {workloads:,} workloads a day "
    "(each counted as one Pod created). Nodes and objects per cluster keep the measured clusters' shape."
)
INSTALLS_NOTE = (
    "One install holds about **{capacity:.0f} clusters**, where {limit} reaches its fixed limit. "
    "This fleet needs **{installs:,} installs** of about {per_install:.0f} clusters each."
)
PLAN_LINE = "Per install: **{nodes} × {machine}**, {memory} of memory and {cpu} of CPU requested."
TOTAL_LINE = "All {installs:,} installs: **{nodes:,} × {machine}**, {vcpu:,} vCPU and {memory:,.0f} GiB of node memory."
NO_MACHINE = "No listed machine type holds the largest pod: {memory} of memory and {cpu} of CPU."
NEED_CAPTION = (
    "Each limit is the predicted peak plus 30%. `spec.deployment.resources` replaces the agent "
    "container's whole block; `credentialProxy.resources` and `agentAPIAuth.resources` replace only "
    "the keys they set. On Autopilot, set the requests to these values."
)
BOTTLENECK_CAPTION = (
    "Each line is a prediction against the default limit, for this fleet's cluster shape at every "
    "size. A line above 100% is past its limit. The shaded range is measured; the rest is predicted "
    "from straight lines, and a queue grows faster than a straight line once it fills. Dashed lines "
    "mark the example fleets."
)
CHART_CAPTION = (
    "Filled points are runs; open points are left out of the fit (a failed or declined prompt, work "
    "that shared the broker, or a CPU peak at its limit). Solid: the fit; dashed: predicted; dotted: "
    "thresholds."
)

recover_app_shell()


def table(rows: list[dict]) -> None:
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")


st.title("Harness limits")
st.caption(f"How large a fleet one kube-agents install holds, from the measurements in `{DESIGN_DOC}`.")

try:
    dataset = hl.load_dataset()
except hl.HarnessDataError as exc:
    st.error(f"The measurements cannot be read: {exc}")
    st.stop()

if dataset.is_sample:
    st.warning(
        f"Sample data. `{hl.display_path(hl.DATA_PATH)}` does not exist yet, so the "
        f"page shows `{hl.display_path(dataset.path)}`. Its values are made up, not "
        "measured."
    )
if not dataset.records:
    st.info(f"`{hl.display_path(dataset.path)}` has no records yet.")
    st.stop()

profiles = {profile.label: profile for profile in hl.FLEET_PROFILES}
choice = st.selectbox(
    "Fleet", [*profiles, hl.CUSTOM_PROFILE], index=len(profiles) - 1, key=f"{KEY_PREFIX}:profile"
)
profile = profiles.get(choice)
if profile:
    st.caption(
        PROFILE_CAPTION.format(
            projects=profile.projects, autopilot=profile.autopilot_share, workloads=profile.workloads_per_day
        )
    )

with st.expander("Data and fleet inputs"):
    commit_column, mode_column = st.columns(SELECTOR_WIDTHS)
    commit = commit_column.selectbox(
        "Commit",
        hl.commits(dataset.records),
        format_func=lambda item: hl.commit_label(dataset.records, item),
        key=f"{KEY_PREFIX}:commit",
    )
    available_modes = hl.modes(dataset.records, commit)
    mode = mode_column.selectbox(
        "Mode",
        available_modes,
        index=hl.default_mode_index(available_modes),
        help="The results hold only for the mode measured.",
        key=f"{KEY_PREFIX}:mode:{commit}",
    )
    records = hl.select(dataset.records, commit, mode)
    scope = f"{commit}:{mode}"
    base = hl.base_fleet(records)
    start = profile.fleet(base) if profile else base
    fleet: dict[str, float] = {}
    fleet_columns = st.columns(FLEET_COLUMNS)
    for index, (factor, spec) in enumerate(hl.FACTORS.items()):
        fleet[factor] = fleet_columns[index % FLEET_COLUMNS].number_input(
            spec.input_label,
            min_value=0.0,
            value=round(float(start[factor]), FLEET_DECIMALS),
            step=FLEET_STEP,
            format=NUMBER_FORMAT,
            help=spec.help or None,
            key=f"{KEY_PREFIX}:fleet:{factor}:{scope}:{choice}",
        )
containers = hl.containers(records)
colors = hl.container_colors(containers, PALETTE.series_colors, PALETTE.neutral)

st.divider()
section_title("1. Are these container limits enough for this fleet?", "One install, with the limits below.")
with st.expander("Container limits: the defaults, or your PlatformAgent values (0 for none)"):
    number_column = st.column_config.NumberColumn(min_value=0.0, format=NUMBER_FORMAT)
    edited = st.data_editor(
        hl.limit_table(hl.usual_limits(records), containers),
        hide_index=True,
        width="stretch",
        disabled=[hl.LIMIT_CONTAINER_COLUMN],
        column_config={hl.LIMIT_MEMORY_COLUMN: number_column, hl.LIMIT_CPU_COLUMN: number_column},
        key=f"{KEY_PREFIX}:limits:{scope}",
    )
limits = hl.limits_from_table(edited)
resource_checks = hl.resource_checks(records, fleet, containers, limits)
threshold_checks = hl.threshold_checks(records, fleet, containers)
first = hl.first_limit(resource_checks + threshold_checks)
failing = [check for check in resource_checks + threshold_checks if check.status == hl.FAIL]
summary = f"{len(failing)} limits fail." if failing else "Every limit holds."
if first is not None and hl.past_at_base(first):
    summary += f" First limit reached: **{first.label}**, already at the measured base."
elif first is not None:
    summary += f" First limit reached: **{first.label}**, at {hl.percent(first.reached)} of this fleet's growth."
(st.error if failing else st.success)(summary)
table(hl.resource_rows(resource_checks))
# Restarts and OOMKills are listed only when one is predicted: a column of passes per container says nothing.
table(
    hl.threshold_rows(
        check for check in threshold_checks if check.status != hl.PASS or check.metric not in (hl.RESTARTS, hl.OOMKILLS)
    )
)

st.divider()
section_title("2. What node size and container limits does this fleet need?", NEED_CAPTION)
plan = hl.install_plan(records, containers, fleet)
if plan.installs > 1:
    st.warning(
        INSTALLS_NOTE.format(
            capacity=plan.capacity, limit=plan.limit.label, installs=plan.installs, per_install=plan.fleet[hl.CLUSTERS]
        )
    )
recommendations = hl.recommendations(records, plan.fleet, containers)
table(hl.recommendation_rows(recommendations))
nodes = hl.node_plan(recommendations, hl.latest_replicas(records))
if nodes.pods and nodes.machine is not None:
    lines = [
        PLAN_LINE.format(
            nodes=nodes.nodes,
            machine=nodes.machine.name,
            memory=hl.memory_quantity(nodes.total_memory_bytes),
            cpu=hl.cpu_quantity(nodes.total_cpu_cores),
        )
    ]
    if plan.installs > 1:
        lines.append(
            TOTAL_LINE.format(
                installs=plan.installs,
                nodes=nodes.nodes * plan.installs,
                machine=nodes.machine.name,
                vcpu=nodes.nodes * plan.installs * nodes.machine.vcpu,
                memory=nodes.nodes * plan.installs * nodes.machine.memory_gib,
            )
        )
    pvc = hl.predict(records, hl.PVC_USED, None, plan.fleet)
    if pvc is not None:
        lines.append(f"Data volume: `spec.storage.size` of at least {hl.storage_quantity(hl.with_margin(pvc.value))}.")
    st.markdown("  \n".join(lines))
elif nodes.pods:
    st.error(
        NO_MACHINE.format(
            memory=hl.memory_quantity(nodes.largest_memory_bytes), cpu=hl.cpu_quantity(nodes.largest_cpu_cores)
        )
    )
st.markdown("**Limits that resources cannot fix**")
table(hl.unfixable_rows(hl.threshold_checks(records, plan.fleet, containers)))

st.divider()
section_title("Why: the limits as the fleet grows", BOTTLENECK_CAPTION)
st.plotly_chart(
    bottleneck_chart(
        hl.bottleneck_series(records, fleet, limits),
        hl.FLEET_PROFILES,
        hl.measured_clusters(records),
        fleet[hl.CLUSTERS],
        PALETTE.series_colors,
    ),
    width="stretch",
    config=CHART_CONFIG,
    key=f"{KEY_PREFIX}:bottlenecks:{scope}",
)

with st.expander("Measured runs"):
    operation_column, factor_column = st.columns(2)
    operation = operation_column.selectbox("Operation", hl.operations(records), key=f"{KEY_PREFIX}:operation:{scope}")
    operation_factors = hl.factors(records, operation)
    if not operation_factors:
        st.info("This operation has base runs only.")
    else:
        factor = factor_column.selectbox(
            "Factor", operation_factors, format_func=hl.factor_label, key=f"{KEY_PREFIX}:factor:{scope}:{operation}"
        )
        charts = hl.metric_charts(records, operation, factor, containers, fleet)
        for first in range(0, len(charts), CHART_COLUMNS):
            row = st.columns(CHART_COLUMNS)
            for column, chart in zip(row, charts[first : first + CHART_COLUMNS]):
                with column:
                    st.markdown(f"**{chart.title}**")
                    st.plotly_chart(
                        harness_limit_chart(chart, colors),
                        width="stretch",
                        config=CHART_CONFIG,
                        key=f"{KEY_PREFIX}:chart:{scope}:{operation}:{factor}:{chart.metric}",
                    )
        st.caption(CHART_CAPTION)
