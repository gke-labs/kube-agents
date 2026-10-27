"""Harness limits: the measured load and sizing of the agent harness."""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import streamlit as st

from admin_console import harness_limits as hl
from admin_console.charts import harness_limit_chart
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
MODEL_CAPTION = (
    "How a prediction combines factors. Each factor has one straight line per "
    "operation, fitted against the factor's measured value: per cluster for nodes "
    "and objects. A prediction starts from the largest base value of all "
    "operations. It adds, for each factor, the largest increase that a line "
    "predicts from the factor's base to this fleet. The sum treats the factors as "
    "independent."
)
INTERACTION_WARNING = (
    "This fleet differs from the base point in more than one factor: {factors}. "
    "The prediction adds their effects one by one and does not measure how they "
    "interact, so it can be low. For example, memory that holds every watched "
    "object grows with clusters × objects per cluster."
)
REACHED_CAPTION = (
    "Reached at: the part of this fleet's growth from the base point at which the "
    "prediction crosses its lowest failing threshold, with all inputs scaled "
    "together. Below 100%, this fleet is past the threshold."
)
FIELD_CAPTION = (
    "`spec.deployment.resources` replaces the agent container's whole resources "
    "block. `credentialProxy.resources` and `agentAPIAuth.resources` replace only "
    "the keys they set. The controller-manager's limit is a Helm chart value."
)
NODE_CAPTION = (
    "All kube-agents pods run on one node pool, so the node answer counts every "
    "measured container, selected or not. Each container requests its recommended "
    "limit, so all containers can reach their peaks at the same time. A pod is its "
    "containers. The gateway pod holds {gateway}. Every other container is a pod "
    "of its own. The node count is the requests of all pods, replicas included, "
    "divided by one node's allocatable memory and CPU. It takes the larger of the "
    "two results. On Autopilot there is no node choice: set the requests only."
)
NODE_PLAN = (
    "Node pool: **{nodes} × {machine}**. It is the smallest machine type that holds "
    "the largest pod requests: {memory} of memory ({memory_pod}) and {cpu} of CPU "
    "({cpu_pod}). All pods, replicas included, request {total_memory} of memory and "
    "{total_cpu} of CPU."
)
NO_MACHINE = (
    "No listed machine type holds the largest pod requests: {memory} of memory "
    "({memory_pod}) and {cpu} of CPU ({cpu_pod})."
)
NO_PODS = "No container has a memory or CPU measurement to size a node from."
MARGIN_NOTE = (
    "Why 30%: the margin covers three things that the fit does not. They are the "
    "spread between the runs at a point, bursts shorter than the sampling interval "
    "(the boot burst, a fan-out), and garbage-collector headroom: the event watcher "
    "sets GOMEMLIMIT to half of its container limit. The 30% is the whole margin. "
    "Phase 1 draws no error band, because two points give no spread to build one "
    "from. A prediction past the largest measured point is labelled predicted, "
    "not measured."
)
CHART_CAPTION = (
    "Filled points are runs. Open points are runs left out of the fit: a response "
    "time from a run that saw a provider 429, a failed prompt, or a prompt answered "
    "without performing its operation (declined, cancelled, cut off, or recorded "
    "before answers were kept), and a CPU peak at 90% or more of the container's "
    "CPU limit, which shows the limit rather than the need. The solid line is the fit over the measured range; "
    "with two distinct planned points it is a two-point "
    "estimate. The dashed line is predicted, not measured. Dotted lines are "
    "thresholds: a container's limit in its colour, the fixed thresholds in the "
    "warning and failure colours."
)

COMBINED_CAPTION = (
    "A prediction adds each factor's effect as if the factors were independent. "
    "Above 1, the measured peak exceeds the prediction: the factors interact, and "
    "predictions for fleets that move the same factors are low."
)

recover_app_shell()


def table(rows: list[dict]) -> None:
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")


st.title("Harness limits")
st.caption(
    "Load and sizing measurements of the agent harness, read from the repository; "
    f"the plan, metrics and thresholds are in `{DESIGN_DOC}`. This page needs no "
    "connection."
)

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

available = hl.containers(records)
selected = st.multiselect(
    "Containers", available, default=available, key=f"{KEY_PREFIX}:containers:{scope}"
)
colors = hl.container_colors(available, PALETTE.series_colors, PALETTE.neutral)

section_title("Fleet", "The defaults put every factor at the base point of the data.")
base = hl.base_fleet(records)
fleet: dict[str, float] = {}
fleet_columns = st.columns(FLEET_COLUMNS)
for index, (factor, spec) in enumerate(hl.FACTORS.items()):
    fleet[factor] = fleet_columns[index % FLEET_COLUMNS].number_input(
        spec.input_label,
        min_value=0.0,
        # One decimal: a measured base (objects per cluster from totals) is rarely a whole number.
        value=round(float(base[factor]), FLEET_DECIMALS),
        step=FLEET_STEP,
        format=NUMBER_FORMAT,
        help=spec.help or None,
        key=f"{KEY_PREFIX}:fleet:{factor}:{scope}",
    )
unmapped = hl.unmapped_factors(records)
if unmapped:
    st.caption(f"No input sets {', '.join(unmapped)}; predictions hold it at its base.")
grown = hl.grown_factors(records, fleet)
if len(grown) > 1:
    st.warning(INTERACTION_WARNING.format(factors=", ".join(grown)))

st.divider()
section_title(
    "Are these container limits enough for this fleet?",
    "The limits default to those of the latest run. Set 0 for no limit.",
)
limits: dict[str, dict[str, float]] = {}
if selected:
    number_column = st.column_config.NumberColumn(min_value=0.0, format=NUMBER_FORMAT)
    edited = st.data_editor(
        hl.limit_table(hl.latest_limits(records), selected),
        hide_index=True,
        width="stretch",
        disabled=[hl.LIMIT_CONTAINER_COLUMN],
        column_config={hl.LIMIT_MEMORY_COLUMN: number_column, hl.LIMIT_CPU_COLUMN: number_column},
        key=f"{KEY_PREFIX}:limits:{scope}:{','.join(selected)}",
    )
    limits = hl.limits_from_table(edited)

resource_checks = hl.resource_checks(records, fleet, selected, limits)
threshold_checks = hl.threshold_checks(records, fleet, selected)
checks = resource_checks + threshold_checks
evaluated = [check for check in checks if check.status in (hl.PASS, hl.WARN, hl.FAIL)]
failing = [check for check in evaluated if check.status == hl.FAIL]
warning = [check for check in evaluated if check.status == hl.WARN]
if failing:
    st.error(
        f"{len(failing)} of {len(evaluated)} checks fail for this fleet: "
        + ", ".join(check.label for check in failing)
        + "."
    )
elif warning:
    st.warning(
        f"No check fails; {len(warning)} warn: "
        + ", ".join(check.label for check in warning)
        + "."
    )
else:
    st.success(f"All {len(evaluated)} checks pass for this fleet.")
first = hl.first_limit(checks)
if first is not None and hl.past_at_base(first):
    st.markdown(f"**First limit reached:** {first.label}, already at the base point of the data.")
elif first is not None:
    st.markdown(
        f"**First limit reached:** {first.label}, at {hl.percent(first.reached)} of "
        "this fleet's growth from the base point."
    )
else:
    st.markdown("**First limit reached:** none. No prediction grows toward a limit.")
if any(check.prediction is not None and check.prediction.outside for check in checks):
    st.warning(
        "Some inputs are past the largest measured value. Those predictions are "
        "predicted, not measured."
    )
table(hl.resource_rows(resource_checks))
table(hl.threshold_rows(threshold_checks))
st.caption(REACHED_CAPTION)
st.caption(MODEL_CAPTION)
with st.expander("How each prediction is built"):
    table(hl.contribution_rows(checks))

st.divider()
section_title(
    "What node size and container limits does this fleet need?",
    "Each recommended limit is the predicted peak plus 30%.",
)
recommendations = hl.recommendations(records, fleet, available)
if selected:
    table(
        hl.recommendation_rows(
            item for item in recommendations if item.container in selected
        )
    )
    st.caption(FIELD_CAPTION)
else:
    st.info("Select a container to see its recommended limits.")
plan = hl.node_plan(recommendations, hl.latest_replicas(records))
if not plan.pods:
    st.info(NO_PODS)
else:
    largest = {
        "memory": hl.memory_quantity(plan.largest_memory_bytes),
        "memory_pod": plan.largest_memory_pod.name,
        "cpu": hl.cpu_quantity(plan.largest_cpu_cores),
        "cpu_pod": plan.largest_cpu_pod.name,
    }
    if plan.machine is not None:
        st.success(
            NODE_PLAN.format(
                nodes=plan.nodes,
                machine=plan.machine.name,
                total_memory=hl.memory_quantity(plan.total_memory_bytes),
                total_cpu=hl.cpu_quantity(plan.total_cpu_cores),
                **largest,
            )
        )
    else:
        st.error(NO_MACHINE.format(**largest))
    table(hl.pod_rows(plan))
    table(hl.machine_rows(plan))
    *first, last = hl.POD_CONTAINERS[hl.GATEWAY_POD]
    st.caption(NODE_CAPTION.format(gateway=f"{', '.join(first)} and {last}"))
pvc = hl.predict(records, hl.PVC_USED, None, fleet)
if pvc is not None:
    st.markdown(
        f"**Data volume:** predicted use {hl.format_value(hl.PVC_USED, pvc.value)}. "
        "With the margin, set `spec.storage.size` to at least "
        f"{hl.storage_quantity(hl.with_margin(pvc.value))}."
    )
st.info(MARGIN_NOTE)
st.markdown("**Limits that container resources do not fix**")
table(hl.unfixable_rows(threshold_checks))

st.divider()
section_title(
    "Measurements",
    "One chart per metric against one factor, for one operation: the base runs "
    "and the factor's runs.",
)
operation_column, factor_column = st.columns(2)
operation = operation_column.selectbox(
    "Operation", hl.operations(records), key=f"{KEY_PREFIX}:operation:{scope}"
)
operation_factors = hl.factors(records, operation)
if not operation_factors:
    st.info("This operation has base runs only.")
else:
    factor = factor_column.selectbox(
        "Factor",
        operation_factors,
        format_func=hl.factor_label,
        key=f"{KEY_PREFIX}:factor:{scope}:{operation}",
    )
    charts = hl.metric_charts(records, operation, factor, selected, fleet)
    for start in range(0, len(charts), CHART_COLUMNS):
        row = st.columns(CHART_COLUMNS)
        for column, chart in zip(row, charts[start : start + CHART_COLUMNS]):
            with column:
                st.markdown(f"**{chart.title}**")
                st.plotly_chart(
                    harness_limit_chart(chart, colors),
                    width="stretch",
                    config=CHART_CONFIG,
                    key=f"{KEY_PREFIX}:chart:{scope}:{operation}:{factor}:{chart.metric}",
                )
    st.caption(CHART_CAPTION)

combined = hl.combined_rows(records, selected)
if combined:
    section_title(
        "Combined points",
        "Runs that move several factors at once, against what this page predicts for "
        "the same fleet from the single-factor runs.",
    )
    table(combined)
    st.caption(COMBINED_CAPTION)

with st.expander("Raw records"):
    st.caption(
        f"{len(records)} records of commit {commit[: hl.COMMIT_LABEL_LENGTH]} in mode "
        f"{mode}, from `{hl.display_path(dataset.path)}`."
    )
    table(hl.record_rows(records))
    table(hl.measurement_rows(records))
