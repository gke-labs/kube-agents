"""Harness limits: the measured load and sizing of the agent harness."""

from __future__ import annotations

import math
import sys
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import streamlit as st

from admin_console import harness_limits as hl
from admin_console.charts import bottleneck_chart, breakdown_chart, harness_limit_chart
from admin_console.connection_session import recover_app_shell
from admin_console.ui import PALETTE

KEY_PREFIX = "harness_limits"
DESIGN_DOC = "docs/designs/agent-harness-bottlenecks.md"
# The commit selector's label carries the image, so it takes the wider column.
SELECTOR_WIDTHS = (3, 1)
FLEET_COLUMNS = 4
CHART_COLUMNS = 2
FLEET_STEP = 1.0
MIN_CLUSTERS = 1.0
# Shows 2 as "2" and 178.5 as "178.5".
NUMBER_FORMAT = "%.1f"
FLEET_DECIMALS = 1
CHART_CONFIG = {"displayModeBar": False}
# The largest fleet the forecast chart reaches, for the verdict when no line reaches its limit.
FORECAST_CEILING = "10,000"
# The resources a container size sets, charted in their own units under the share chart.
SIZED_METRICS = (hl.MEMORY_PEAK, hl.CPU_PEAK)
# The readings listed before the rest fold into an expander.
EVIDENCE_METRICS_SHOWN = 12
PROFILE_CAPTION = (
    "{projects:,} projects, {autopilot:.0%} of clusters on Autopilot, {workloads:,} workloads a day "
    "(each counted as one Pod created). Nodes and objects per cluster keep the measured clusters' shape."
)
STORY = (
    "Pick a fleet size and the container size you have in mind. The forecast says whether one "
    "kube-agents install holds that fleet with those containers, which limit it reaches first, and, when one install "
    "cannot hold it, how many installs it needs and what each one needs. Part 2 shows the "
    "measurements behind every number."
)
SPLIT_NONE = "One install holds all {clusters:,.0f} clusters."
SPLIT_NEEDED = (
    "One install holds about {held:.0f} clusters (measured), so {clusters:,.0f} clusters need "
    "**{installs:,} installs of about {per_install:.0f} clusters**, or {stall_installs:,} for Stall Watch "
    "to read every cluster each half hour."
)
VERDICT_OK = "**Yes.** {split} The {size} container size is enough for each install."
VERDICT_SHORT = (
    "**Not with this container size.** {split} With the {size} size each install runs short of "
    "**{short}**; pick *Recommended for this fleet* for the sizes listed below."
)
VERDICT_IMPOSSIBLE = (
    "**No.** {split} Even at one cluster per install, the {names} would need more than the largest "
    "container GKE can practically give (120 CPU, 480 GB on one n2-standard-128 node). This shape is "
    "past the measurements."
)
SHELL_FIXED_LINE = (
    "- **Sandbox shell: 2 CPU, fixed by the kube-agents operator, not by GKE.** Every brokered command "
    "starts there, so the command path carries about 1.5 commands per second. At {held:.0f} clusters "
    "the shell peaked at {shell:.0%} of it."
)
SHELL_SETTABLE_LINE = (
    "- **Sandbox shell: sized like the other containers here,** which assumes an operator change. "
    "Past {held:.0f} clusters nothing is measured, so the next limit there is unknown."
)
STALL_LINE = (
    "- **Stall Watch: one cluster at a time, about {seconds:.0f} seconds each,** within a 25-minute "
    "budget every half hour, so one pass covers about {stall:.0f} clusters."
)
STALL_UNMEASURED_LINE = (
    "- **Stall Watch: one cluster at a time,** within a 25-minute budget every half hour; this data "
    "has no measured pass time."
)
FIXED_WHY = """The limits no setting raises:

{shell_line}
- **Credential proxy: 8 commands at once.** A command that waits 60 seconds is refused.
- **Agent: 10 requests at once.** The 11th is refused.
{stall_line}

The model's straight lines, which take each input's largest increase over all operations, are more cautious: they put the shell at its limit at about {model:.0f} clusters, because a user's upgrade readiness report loads it. At the measured size that report runs slower; it does not fail."""
INSTALL_LINE = (
    "- **Nodes per install:** {nodes} × {machine}, for {memory} of memory and {cpu} requested; "
    "a data volume of at least {pvc}."
)
TOTAL_LINE = (
    "- **All {installs:,} installs:** {total_nodes:,} × {machine}, {vcpu:,} vCPU and {node_memory:,.0f} GB "
    "of node memory."
)
NEED_ONE = "One install holds this fleet. Its containers need:"
SIZED_MEASURED = "(From the runs measured at {clusters:.0f} clusters, plus 30%.)"
SIZED_MODEL = "(From the model's lines: this size is past every measured fleet.)"
NEED_INTRO = "Split into {installs:,} installs of about {per_install:.0f} clusters each. Each install needs:"
SHELL_TOGGLE = "Assume an operator change lets the sandbox shell be sized too"
SHELL_HELP = (
    "GKE would run a larger shell. Its 2 CPU and 2.1 GB (2 GiB) are fixed by the kube-agents operator "
    f"({hl.SHELL_RESOURCES_SOURCE}), and no PlatformAgent field changes them today."
)
LIMITS_TABLE_CAPTION = (
    "Each container's size is its limit: memory in GB, CPU in cores; 0 is no limit. A container can "
    "be as large as one node holds: on GKE Standard about 120 CPU and 480 GB on an n2-standard-128 "
    "node; GKE Autopilot caps a Pod lower, by compute class. The sandbox shell's 2 CPU and 2.1 GB "
    "(2 GiB) are set by the kube-agents operator, not by GKE, unless the option above assumes otherwise."
)
NO_MACHINE = "No listed machine type holds the largest pod: {memory} of memory and {cpu} of CPU."
NEED_CAPTION = (
    "Each size is the predicted peak for one install plus 30%. Set them in the PlatformAgent: "
    "`spec.deployment.resources` replaces "
    "the agent container's whole block; `credentialProxy.resources` and `agentAPIAuth.resources` "
    "replace only the keys they set. On Autopilot, set the requests to these values."
)
FORECAST_CAPTION = (
    "Each line is a key prediction as a share of its limit at the chosen container size, for this "
    "fleet's cluster shape at every size; a cross marks where it reaches the limit. Dots are every "
    "measured run, each at its own fleet shape and operation (hover one for its size), so they show "
    "the range measured, not this fleet. The shaded range is measured, the rest predicted; dashed "
    "lines mark the example fleets."
)
SIZED_CAPTION = (
    "On the share chart above, every limit is the 100% line, so a larger container size moves the "
    "lines down. Here the same predictions are in GB and cores, and each dotted line is a container's "
    "limit at the chosen size: it moves with the size picked at the top and with any edit to it."
)
EVIDENCE_STORY = (
    "The runs behind Part 1, read from `{path}`: **{runs:,} runs** on one experiment install, from "
    "{first} to {last}, in {phases} measurement phases. Each run applied one fleet point to the test "
    "clusters, ran one operation, and recorded what each container and the broker used."
)
EVIDENCE_CHARTS = (
    "The charts below take no input from Part 1: each resource in its own unit, one line per container "
    "fitted through the runs, for clusters shaped like the measured base, against the default limits."
)
CURVE_NOTE = (
    "Why the lines curve upward: they are not exponential. The model is a straight line in each "
    "input, a base plus a cost per cluster, per Pod created per minute and per failing Pod, and the "
    "last two grow with the fleet. The axis is logarithmic, so a straight line bends upward on it. "
    "Turn the logarithmic axis off to see the lines straight. A real queue, such as the broker's "
    "8 command slots, grows faster than a straight line once it fills, so past the measured range "
    "the lines are a lower bound."
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

with st.expander("Measurement data"):
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
containers = hl.containers(records)
colors = hl.container_colors(containers, PALETTE.series_colors, PALETTE.neutral)
base = hl.base_fleet(records)
defaults = hl.usual_limits(records)
measured = hl.measured_clusters(records)

# --- Part 1 ---------------------------------------------------------------------------------
st.header("Part 1: Limits forecast")
st.markdown(STORY)

size_column, limit_column = st.columns(2)
profiles = {profile.label: profile for profile in hl.FLEET_PROFILES}
choice = size_column.selectbox(
    "Fleet size", [*profiles, hl.CUSTOM_PROFILE], index=len(profiles) - 1, key=f"{KEY_PREFIX}:profile"
)
preset = limit_column.selectbox(
    "Container size", hl.LIMIT_PRESETS, index=hl.LIMIT_PRESETS.index(hl.LIMITS_M), key=f"{KEY_PREFIX}:limit_preset"
)
profile = profiles.get(choice)
if profile:
    st.caption(
        PROFILE_CAPTION.format(
            projects=profile.projects, autopilot=profile.autopilot_share, workloads=profile.workloads_per_day
        )
    )
start = profile.fleet(base) if profile else base
with st.expander("Fine-tune the fleet"):
    fleet: dict[str, float] = {}
    fleet_columns = st.columns(FLEET_COLUMNS)
    for index, (factor, spec) in enumerate(hl.FACTORS.items()):
        fleet[factor] = fleet_columns[index % FLEET_COLUMNS].number_input(
            spec.input_label,
            # A fleet has at least one cluster; every other input can be zero.
            min_value=MIN_CLUSTERS if factor == hl.CLUSTERS else 0.0,
            value=round(float(start[factor]), FLEET_DECIMALS),
            step=FLEET_STEP,
            format=NUMBER_FORMAT,
            help=spec.help or None,
            key=f"{KEY_PREFIX}:fleet:{factor}:{scope}:{choice}",
        )
plan = hl.install_plan(records, containers, fleet)
# What one install holds: the largest measured fleet that held, or the model's line without one.
held = hl.measured_install_capacity(records) or plan.capacity
stall_seconds = hl.stall_watch_seconds_per_cluster(records)
stall_clusters = hl.STALL_WATCH_BUDGET_SECONDS / stall_seconds if stall_seconds else None
per_install_clusters = min(fleet[hl.CLUSTERS], held) if held else plan.fleet[hl.CLUSTERS]
# Whole clusters per install, at least one, so the count and the "about N clusters" agree.
per_install_whole = max(int(per_install_clusters), 1) if per_install_clusters else 1
installs = max(math.ceil(fleet[hl.CLUSTERS] / per_install_whole), 1)
split = hl.split_fleet(records, fleet, containers, installs)
installs, install_fleet = split.installs, split.fleet
recommendations, sized_from = split.recommendations, split.sized_from
shell_settable = st.toggle(SHELL_TOGGLE, value=False, help=SHELL_HELP, key=f"{KEY_PREFIX}:shell_settable")
with st.expander("Change the container size"):
    st.caption(LIMITS_TABLE_CAPTION)
    number_column = st.column_config.NumberColumn(min_value=0.0, format=NUMBER_FORMAT)
    edited = st.data_editor(
        hl.limit_table(
            hl.preset_limits(preset, records, recommendations, shell_settable),
            [name for name in containers if name in defaults],
        ),
        hide_index=True,
        width="stretch",
        disabled=[hl.LIMIT_CONTAINER_COLUMN],
        column_config={hl.LIMIT_MEMORY_COLUMN: number_column, hl.LIMIT_CPU_COLUMN: number_column},
        key=f"{KEY_PREFIX}:limits:{scope}:{preset}:{choice}:{shell_settable}",
    )
limits = hl.limits_from_table(edited)

forecast = hl.bottleneck_series(records, fleet, limits)
first = hl.first_crossing(forecast)

st.subheader("Can this fleet be handled?")
stall_reach = stall_clusters or held
stall_installs = max(-(-int(round(fleet[hl.CLUSTERS])) // max(int(stall_reach or 1), 1)), 1)
if installs == 1:
    split_text = SPLIT_NONE.format(clusters=fleet[hl.CLUSTERS])
else:
    split_text = SPLIT_NEEDED.format(
        clusters=fleet[hl.CLUSTERS], held=held or 0.0, installs=installs,
        per_install=install_fleet[hl.CLUSTERS], stall_installs=stall_installs,
    )
# The same sizes the list below gives: a settable container is short where its need passes its size.
needs = hl.size_needs(recommendations, limits)
short = [
    f"{need.name} {resource}"
    for need in needs
    if need.container not in hl.FIXED_LIMIT_CONTAINERS or shell_settable or preset == hl.LIMITS_S
    for resource in need.short
]
size_name = "recommended" if preset == hl.LIMITS_RECOMMENDED else preset.split(":", 1)[0]
if split.impossible:
    names = ", ".join(hl.CONTAINER_NAMES.get(item.container, item.container) for item in split.impossible)
    st.error(VERDICT_IMPOSSIBLE.format(split=split_text, names=names))
elif short:
    st.error(VERDICT_SHORT.format(split=split_text, size=size_name, short=", ".join(short)))
else:
    st.success(VERDICT_OK.format(split=split_text, size=size_name))
shell_peak = max(
    (run.value(hl.CPU_PEAK, hl.SHELL_CONTAINER) or 0.0 for run in records if run.totals.get(hl.CLUSTERS) == held),
    default=0.0,
)
shell_limit = defaults.get(hl.SHELL_CONTAINER, {}).get(hl.LIMIT_CPU) or 1.0
with st.expander("Why these limits cannot be raised"):
    st.markdown(
        FIXED_WHY.format(
            shell_line=(SHELL_SETTABLE_LINE if shell_settable else SHELL_FIXED_LINE).format(
                held=held or 0.0, shell=shell_peak / shell_limit
            ),
            held=held or 0.0,
            stall_line=STALL_LINE.format(seconds=stall_seconds, stall=stall_clusters)
            if stall_seconds else STALL_UNMEASURED_LINE,
            model=plan.capacity or 0.0,
        )
    )

st.subheader("How big it needs to be")
bullets = []
for need in needs:
    size = f"{hl.cpu_cores_text(need.cpu_cores)} CPU, {hl.memory_text(need.memory_bytes)}"
    if need.container in hl.FIXED_LIMIT_CONTAINERS and not shell_settable:
        # A fixed container cannot be given the margin, so its peak itself is compared with the limit.
        fixed = need.current
        peak_cpu = (need.cpu_cores or 0.0) / (1 + hl.SAFETY_MARGIN)
        fixed_cpu = fixed.get(hl.LIMIT_CPU) or 0.0
        state = "within it" if not fixed_cpu or peak_cpu <= fixed_cpu else "past it, so every brokered command slows down"
        given = f"{hl.cpu_cores_text(fixed_cpu)} CPU, {hl.memory_text(fixed.get(hl.LIMIT_MEMORY))}"
        if preset == hl.LIMITS_S:
            bullets.append(
                f"- **{need.name}:** peaks at about {hl.cpu_cores_text(peak_cpu)} CPU; Autopilot without bursting "
                f"gives it only its request, {given} ({state})."
            )
        else:
            bullets.append(
                f"- **{need.name}:** peaks at about {hl.cpu_cores_text(peak_cpu)} CPU of its fixed {given}, "
                f"which no setting raises ({state})."
            )
    elif need.enough:
        bullets.append(f"- **{need.name}:** needs about {size}; the chosen container size is enough.")
    else:
        current = need.current
        bullets.append(
            f"- **{need.name}:** needs about {size}; the chosen container size gives "
            f"{hl.cpu_cores_text(current.get(hl.LIMIT_CPU))} CPU, {hl.memory_text(current.get(hl.LIMIT_MEMORY))}: "
            f"too little {' and '.join(need.short)}."
        )
bullets.append("- **LiteLLM, the operator and the rest:** their defaults are enough.")
nodes = hl.node_plan(recommendations, hl.latest_replicas(records))
pvc = hl.predict(records, hl.PVC_USED, None, install_fleet)
if nodes.pods and nodes.machine is not None:
    bullets.append(
        INSTALL_LINE.format(
            per_install=install_fleet[hl.CLUSTERS],
            nodes=nodes.nodes,
            machine=nodes.machine.name,
            memory=hl.memory_text(nodes.total_memory_bytes),
            cpu=f"{hl.cpu_cores_text(nodes.total_cpu_cores)} CPU",
            pvc=hl.storage_quantity(hl.with_margin(pvc.value)) if pvc else hl.NO_VALUE,
        )
    )
    if installs > 1:
        bullets.append(
            TOTAL_LINE.format(
                installs=installs,
                total_nodes=nodes.nodes * installs,
                machine=nodes.machine.name,
                vcpu=nodes.nodes * installs * nodes.machine.vcpu,
                node_memory=nodes.nodes * installs * nodes.machine.memory_gib * hl.GIB / hl.GB,
            )
        )
heading = NEED_INTRO.format(installs=installs, per_install=install_fleet[hl.CLUSTERS]) if installs > 1 else NEED_ONE
heading += " " + (SIZED_MEASURED.format(clusters=sized_from) if sized_from else SIZED_MODEL)
st.markdown(heading + "\n\n" + "\n".join(bullets))
st.caption(NEED_CAPTION)
if nodes.pods and nodes.machine is None:
    st.error(
        NO_MACHINE.format(
            memory=hl.memory_text(nodes.largest_memory_bytes), cpu=f"{hl.cpu_cores_text(nodes.largest_cpu_cores)} CPU"
        )
    )

st.subheader("Which limit the fleet reaches first")
if first is None:
    st.markdown(f"With this container size, no key limit is reached up to {FORECAST_CEILING} clusters.")
elif first[1] <= forecast[0].clusters[0]:
    st.markdown(f"With this container size, **{first[0].label}** is past its limit at every fleet size.")
else:
    st.markdown(f"With this container size, the first limit reached is **{first[0].label}**, at about {first[1]:,.0f} clusters.")
st.plotly_chart(
    bottleneck_chart(
        forecast,
        hl.FLEET_PROFILES,
        measured,
        fleet[hl.CLUSTERS],
        PALETTE.series_colors,
        fleet[hl.NODES],
        crossings=[hl.crossing(item) for item in forecast],
    ),
    width="stretch",
    config=CHART_CONFIG,
    key=f"{KEY_PREFIX}:forecast:{scope}",
)
st.caption(FORECAST_CAPTION)
# The same predictions in their own units, so the chosen container size shows as the limit lines.
size_columns = st.columns(CHART_COLUMNS)
for column, metric in zip(size_columns, SIZED_METRICS):
    spec = hl.metric_spec(metric)
    with column:
        st.markdown(f"**{spec.label} at the chosen container size**")
        st.plotly_chart(
            breakdown_chart(
                hl.breakdown_series(records, fleet, limits, metric),
                spec.label,
                spec.unit,
                hl.FLEET_PROFILES,
                measured,
                fleet[hl.CLUSTERS],
                colors,
                fleet[hl.NODES],
            ),
            width="stretch",
            config=CHART_CONFIG,
            key=f"{KEY_PREFIX}:sized:{scope}:{metric}",
        )
st.caption(SIZED_CAPTION)

# --- Part 2 ---------------------------------------------------------------------------------
st.divider()
st.header("Part 2: Empirical evidence")
summary = hl.dataset_summary(records)
st.markdown(
    EVIDENCE_STORY.format(
        path=hl.display_path(dataset.path), runs=summary.runs, first=summary.first, last=summary.last,
        phases=len(summary.phases),
    )
)
inputs_column, readings_column = st.columns(2)
with inputs_column:
    st.markdown("**The fleet points**")
    st.markdown("\n".join(
        f"- {item.label}: {hl.number_text(item.low)} to {hl.number_text(item.high)}, {item.levels} levels"
        for item in summary.inputs
    ))
    st.markdown(f"**The operations ({len(summary.operations)})**: " + ", ".join(f"`{name}`" for name in summary.operations))
with readings_column:
    st.markdown(f"**The readings ({len(summary.metrics)} metrics, runs that recorded each)**")
    st.markdown("\n".join(f"- {label}: {count}" for label, count in summary.metrics[:EVIDENCE_METRICS_SHOWN]))
    rest = summary.metrics[EVIDENCE_METRICS_SHOWN:]
    if rest:
        with st.expander(f"{len(rest)} more metrics"):
            st.markdown("\n".join(f"- {label}: {count}" for label, count in rest))
    st.markdown(f"**The containers ({len(summary.containers)})**: " + ", ".join(f"`{name}`" for name in summary.containers))
st.markdown(EVIDENCE_CHARTS)
log_axis = st.toggle("Logarithmic axis", value=True, key=f"{KEY_PREFIX}:log_axis")
for first_metric in range(0, len(hl.BREAKDOWN_OWNERS), CHART_COLUMNS):
    row = st.columns(CHART_COLUMNS)
    for column, metric in zip(row, list(hl.BREAKDOWN_OWNERS)[first_metric : first_metric + CHART_COLUMNS]):
        spec = hl.metric_spec(metric)
        with column:
            st.markdown(f"**{spec.label}**")
            st.plotly_chart(
                breakdown_chart(
                    hl.breakdown_series(records, base, defaults, metric),
                    spec.label,
                    spec.unit,
                    hl.FLEET_PROFILES,
                    measured,
                    None,
                    colors,
                    base[hl.NODES],
                    log_axis,
                ),
                width="stretch",
                config=CHART_CONFIG,
                key=f"{KEY_PREFIX}:breakdown:{scope}:{metric}:{log_axis}",
            )
st.caption(CURVE_NOTE)

st.subheader("Every metric, run by run")
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
    for first_chart in range(0, len(charts), CHART_COLUMNS):
        row = st.columns(CHART_COLUMNS)
        for column, chart in zip(row, charts[first_chart : first_chart + CHART_COLUMNS]):
            with column:
                st.markdown(f"**{chart.title}**")
                st.plotly_chart(
                    harness_limit_chart(chart, colors),
                    width="stretch",
                    config=CHART_CONFIG,
                    key=f"{KEY_PREFIX}:chart:{scope}:{operation}:{factor}:{chart.metric}",
                )
    st.caption(CHART_CAPTION)
