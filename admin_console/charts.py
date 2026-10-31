"""Plotly visualizations for normalized activity and harness measurements."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from html import escape

import plotly.graph_objects as go

from admin_console.causal_flow import CausalSource
from admin_console.domain import ActivityEvent
from admin_console.harness_limits import (
    LEAST_SQUARES_LABEL,
    LIMIT_LABEL,
    PREDICTED_LABEL,
    TWO_POINT_LABEL,
    MetricChart,
)
from admin_console.ui import ATTRIBUTION_COLORS, PALETTE, STATUS_COLORS, TRIGGER_COLORS

PLOT_BG = "rgba(0,0,0,0)"
GRID = PALETTE.border
TEXT = PALETTE.chart_text
HARNESS_CHART_HEIGHT = 360
HARNESS_MARKER_SIZE = 9
HARNESS_LINE_WIDTH = 2
THRESHOLD_LINE_WIDTH = 1
PREDICTION_DASH = "dash"
THRESHOLD_DASH = "dot"
FLEET_DASH = "dashdot"
MEASURED_SYMBOL = "circle"
EXCLUDED_SYMBOL = "circle-open"
EXCLUDED_LABEL = "excluded from fit"
FLEET_LABEL = "this fleet"
CONTAINER_LIMIT_KEY = "container limit"


def _base_layout(figure: go.Figure, *, height: int) -> go.Figure:
    figure.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 28, "b": 10},
        paper_bgcolor=PLOT_BG,
        plot_bgcolor=PLOT_BG,
        font={"color": TEXT, "family": "Inter, ui-sans-serif, system-ui"},
        hoverlabel={
            "bgcolor": PALETTE.chart_hover_background,
            "bordercolor": GRID,
            "font_color": PALETTE.text,
        },
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.01,
            "xanchor": "left",
            "x": 0,
        },
    )
    return figure


def activity_over_time(events: list[ActivityEvent]) -> go.Figure:
    grouped: dict[tuple[object, str], int] = defaultdict(int)
    for event in events:
        bucket = event.occurred_at.replace(
            minute=(event.occurred_at.minute // 15) * 15,
            second=0,
            microsecond=0,
        )
        grouped[(bucket, event.trigger_kind.value)] += 1

    figure = go.Figure()
    trigger_names = sorted({event.trigger_kind.value for event in events})
    for trigger_name in trigger_names:
        points = sorted(
            (bucket, count)
            for (bucket, trigger), count in grouped.items()
            if trigger == trigger_name
        )
        if not points:
            continue
        color = TRIGGER_COLORS[
            next(kind for kind in TRIGGER_COLORS if kind.value == trigger_name)
        ]
        figure.add_trace(
            go.Scatter(
                x=[point[0] for point in points],
                y=[point[1] for point in points],
                name=trigger_name.replace("_", " ").title(),
                mode="lines+markers",
                stackgroup="activity",
                line={"color": color, "width": 2},
                marker={"size": 6},
                hovertemplate="%{x|%H:%M}<br>%{y} events<extra></extra>",
            )
        )
    figure.update_xaxes(showgrid=False, tickformat="%H:%M")
    figure.update_yaxes(showgrid=True, gridcolor=GRID, rangemode="tozero", title=None)
    return _base_layout(figure, height=300)


def attribution_donut(events: list[ActivityEvent]) -> go.Figure:
    counts = Counter(event.attribution for event in events)
    labels = list(counts)
    figure = go.Figure(
        go.Pie(
            labels=[label.value.title() for label in labels],
            values=[counts[label] for label in labels],
            hole=0.72,
            marker={"colors": [ATTRIBUTION_COLORS[label] for label in labels]},
            textinfo="none",
            hovertemplate="%{label}: %{value} events<extra></extra>",
        )
    )
    explicit = counts.get(
        next(level for level in ATTRIBUTION_COLORS if level.value == "explicit"), 0
    )
    inherited = counts.get(
        next(level for level in ATTRIBUTION_COLORS if level.value == "inherited"), 0
    )
    coverage = round(100 * (explicit + inherited) / max(len(events), 1))
    figure.add_annotation(
        text=f"<b>{coverage}%</b><br><span style='font-size:11px'>linked</span>",
        showarrow=False,
        font={"color": PALETTE.text, "size": 20},
    )
    return _base_layout(figure, height=300)


def causality_sankey(events: list[ActivityEvent]) -> go.Figure:
    """Aggregate raw OTel origin → agent → action → outcome edges."""
    labels: list[str] = []
    colors: list[str] = []
    node_details: list[str] = []
    index: dict[tuple[str, str], int] = {}

    def node(
        stage: str,
        label: str,
        color: str,
        detail: str = "",
        identity: str = "",
    ) -> int:
        key = (stage, identity or label)
        if key not in index:
            index[key] = len(labels)
            labels.append(label)
            colors.append(color)
            node_details.append(detail or label)
        return index[key]

    sources = [CausalSource.from_event(event) for event in events]
    source_sessions: dict[str, set[str]] = defaultdict(set)
    source_evidence: dict[str, dict[tuple[str, str], set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for source in sources:
        if source.session_id:
            source_sessions[source.key].add(source.session_id)
        for evidence in source.evidence:
            source_evidence[source.key][
                (evidence.scope, evidence.field)
            ].add(evidence.value)

    edge_counts: Counter[tuple[int, int]] = Counter()
    for event, source in zip(events, sources, strict=True):
        session_count = len(source_sessions[source.key])
        session_label = "session" if session_count == 1 else "sessions"
        source_descriptor = "cron"
        if source.source_type != "cron":
            # The label names the product, not the runtime (#1864); the
            # hover detail below keeps the raw attribute name.
            source_descriptor = (
                source.primary.field.removeprefix("hermes.")
                if source.primary
                else "source unavailable"
            )
        source_label = (
            f"{escape(source.source_id)} ({source_descriptor} · "
            f"{session_count} {session_label})"
        )
        source_detail_parts = [
            f"source.type={escape(source.source_type)}",
            f"source.id={escape(source.source_id)}",
        ]
        source_detail_parts.append(f"distinct sessions={session_count}")
        for (scope, field), raw_values in sorted(
            source_evidence[source.key].items()
        ):
            values = sorted(raw_values)
            sample = ", ".join(escape(value) for value in values[:5])
            suffix = "" if len(values) <= 5 else f" (+{len(values) - 5} more)"
            source_detail_parts.append(
                f"raw {scope} {field}={sample}{suffix}"
            )
        trigger_color = TRIGGER_COLORS[event.trigger_kind]
        action_label = (
            event.tool_name
            or ("LLM call" if event.action_type == "model" else "")
            or (
                event.action_name
                if event.action_type in {"skill", "approval"}
                else ""
            )
            or event.action_type.replace("_", " ").title()
        )
        outcome_label = event.status.replace("_", " ").title()

        trigger = node(
            "trigger",
            source_label,
            trigger_color,
            "<br>".join(source_detail_parts),
            source.key,
        )
        agent = node("agent", event.agent_name, PALETTE.accent)
        action = node("action", action_label, PALETTE.violet)
        outcome = node(
            "outcome", outcome_label, STATUS_COLORS.get(event.status, PALETTE.neutral)
        )
        edge_counts[(trigger, agent)] += 1
        edge_counts[(agent, action)] += 1
        edge_counts[(action, outcome)] += 1

    sources, targets, values = [], [], []
    for (source, target), value in edge_counts.items():
        sources.append(source)
        targets.append(target)
        values.append(value)

    figure = go.Figure(
        go.Sankey(
            arrangement="snap",
            node={
                "label": labels,
                "color": colors,
                "customdata": node_details,
                "pad": 20,
                "thickness": 14,
                "line": {"color": GRID, "width": 1},
                "hovertemplate": "%{customdata}<extra></extra>",
            },
            link={
                "source": sources,
                "target": targets,
                "value": values,
                "color": PALETTE.flow_link,
                "hovertemplate": "%{source.label} → %{target.label}<br>%{value} events<extra></extra>",
            },
        )
    )
    return _base_layout(figure, height=520)


def interaction_timeline(events: list[ActivityEvent]) -> go.Figure:
    ordered_agents = list(dict.fromkeys(event.agent_name for event in events))
    figure = go.Figure()
    for event in events:
        figure.add_trace(
            go.Scatter(
                x=[event.occurred_at],
                y=[event.agent_name],
                mode="markers",
                marker={
                    "size": max(11, min(24, 10 + event.duration_ms / 800)),
                    "color": STATUS_COLORS.get(event.status, PALETTE.neutral),
                    "line": {"color": PALETTE.marker_outline, "width": 1},
                },
                name=event.status.title(),
                legendgroup=event.status,
                showlegend=False,
                customdata=[[event.action_name, event.summary, event.event_id]],
                hovertemplate=(
                    "<b>%{customdata[0]}</b><br>%{y} · %{x|%H:%M:%S}"
                    "<br>%{customdata[1]}<br><code>%{customdata[2]}</code><extra></extra>"
                ),
            )
        )
    figure.update_xaxes(showgrid=True, gridcolor=GRID, tickformat="%H:%M")
    figure.update_yaxes(
        showgrid=True,
        gridcolor=GRID,
        categoryorder="array",
        categoryarray=list(reversed(ordered_agents)),
    )
    return _base_layout(figure, height=max(260, 80 + len(ordered_agents) * 48))


def _style_key(figure: go.Figure, name: str, *, dash: str = "", symbol: str = "") -> None:
    """A legend entry that explains a line or marker style, with no data."""
    figure.add_trace(
        go.Scatter(
            x=[None],
            y=[None],
            name=name,
            mode="markers" if symbol else "lines",
            line={"color": PALETTE.neutral, "width": HARNESS_LINE_WIDTH, "dash": dash or None},
            marker={"color": PALETTE.neutral, "size": HARNESS_MARKER_SIZE, "symbol": symbol or None},
            hoverinfo="skip",
        )
    )


def harness_limit_chart(chart: MetricChart, colors: Mapping[str | None, str]) -> go.Figure:
    """One metric against one factor: the runs, the fitted lines, and the thresholds."""
    figure = go.Figure()
    scale = chart.scale
    unit = f" {chart.unit}" if chart.unit else ""
    for series in chart.series:
        color = colors.get(series.container, PALETTE.accent)
        name = escape(series.name)
        for excluded in (False, True):
            points = [point for point in series.points if point.excluded is excluded]
            if not points:
                continue
            figure.add_trace(
                go.Scatter(
                    x=[point.x for point in points],
                    y=[point.y / scale for point in points],
                    mode="markers",
                    name=f"{series.name} ({EXCLUDED_LABEL})" if excluded else series.name,
                    legendgroup=series.name,
                    showlegend=not excluded,
                    marker={
                        "color": color,
                        "size": HARNESS_MARKER_SIZE,
                        "symbol": EXCLUDED_SYMBOL if excluded else MEASURED_SYMBOL,
                    },
                    customdata=[[point.key, point.run] for point in points],
                    hovertemplate=(
                        f"<b>{name}</b><br>%{{x:,.1f}} · %{{y:,.2f}}{unit}"
                        "<br>run %{customdata[1]} · %{customdata[0]}<extra></extra>"
                    ),
                )
            )
        fit = series.fit
        if fit is not None:
            segments = [((fit.x_min, fit.x_max), fit.label, None)]
            if chart.x_end > fit.x_max:
                segments.append(((fit.x_max, chart.x_end), PREDICTED_LABEL, PREDICTION_DASH))
            for (start, end), label, dash in segments:
                figure.add_trace(
                    go.Scatter(
                        x=[start, end],
                        y=[fit.at(start) / scale, fit.at(end) / scale],
                        mode="lines",
                        name=f"{series.name} · {label}",
                        legendgroup=series.name,
                        showlegend=False,
                        line={"color": color, "width": HARNESS_LINE_WIDTH, "dash": dash},
                        hovertemplate=(
                            f"<b>{name}</b> · {label}<br>%{{x:,.1f}} · %{{y:,.2f}}{unit}"
                            "<extra></extra>"
                        ),
                    )
                )
        if series.limit is not None:
            # A trace, not a labelled shape: it toggles with its container and
            # names itself on hover instead of covering the base runs with text.
            limit_name = LIMIT_LABEL.format(container=series.name)
            figure.add_trace(
                go.Scatter(
                    x=[0, chart.x_end],
                    y=[series.limit / scale] * 2,
                    mode="lines",
                    name=limit_name,
                    legendgroup=series.name,
                    showlegend=False,
                    line={"color": color, "width": THRESHOLD_LINE_WIDTH, "dash": THRESHOLD_DASH},
                    hovertemplate=(
                        f"<b>{escape(limit_name)}</b><br>%{{y:,.2f}}{unit}<extra></extra>"
                    ),
                )
            )
    for line in chart.lines:
        figure.add_hline(
            y=line.value / scale,
            line={
                "color": PALETTE.danger if line.critical else PALETTE.warning,
                "width": THRESHOLD_LINE_WIDTH,
                "dash": THRESHOLD_DASH,
            },
            annotation_text=escape(line.label),
            annotation_position="top right",
            annotation_font_color=TEXT,
        )
    if chart.fleet_x is not None:
        figure.add_vline(
            x=chart.fleet_x,
            line={"color": PALETTE.neutral, "width": THRESHOLD_LINE_WIDTH, "dash": FLEET_DASH},
        )
        _style_key(figure, FLEET_LABEL, dash=FLEET_DASH)
    if any(series.limit is not None for series in chart.series):
        _style_key(figure, CONTAINER_LIMIT_KEY, dash=THRESHOLD_DASH)
    fits = [series.fit for series in chart.series if series.fit is not None]
    if fits:
        two_point = all(fit.two_point for fit in fits)
        _style_key(figure, TWO_POINT_LABEL if two_point else LEAST_SQUARES_LABEL)
        if any(chart.x_end > fit.x_max for fit in fits):
            _style_key(figure, PREDICTED_LABEL, dash=PREDICTION_DASH)
    if any(point.excluded for series in chart.series for point in series.points):
        _style_key(figure, EXCLUDED_LABEL, symbol=EXCLUDED_SYMBOL)
    figure.update_xaxes(
        title=chart.x_title, showgrid=True, gridcolor=GRID, rangemode="tozero", zeroline=False
    )
    figure.update_yaxes(
        title=chart.y_title, showgrid=True, gridcolor=GRID, rangemode="tozero", zeroline=False
    )
    return _base_layout(figure, height=HARNESS_CHART_HEIGHT)
