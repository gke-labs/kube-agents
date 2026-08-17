"""Plotly visualizations for normalized activity."""

from __future__ import annotations

from collections import Counter, defaultdict
from html import escape

import plotly.graph_objects as go

from admin_console.causal_flow import CausalSource
from admin_console.domain import ActivityEvent
from admin_console.ui import ATTRIBUTION_COLORS, STATUS_COLORS, TRIGGER_COLORS

PLOT_BG = "rgba(0,0,0,0)"
GRID = "#26344c"
TEXT = "#b7c4d9"


def _base_layout(figure: go.Figure, *, height: int) -> go.Figure:
    figure.update_layout(
        height=height,
        margin={"l": 10, "r": 10, "t": 28, "b": 10},
        paper_bgcolor=PLOT_BG,
        plot_bgcolor=PLOT_BG,
        font={"color": TEXT, "family": "Inter, ui-sans-serif, system-ui"},
        hoverlabel={"bgcolor": "#151f32", "bordercolor": GRID, "font_color": "#edf3ff"},
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
        font={"color": "#edf3ff", "size": 20},
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
            source_descriptor = (
                source.primary.field if source.primary else "source unavailable"
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
        agent = node("agent", event.agent_name, "#7C9CFF")
        action = node("action", action_label, "#B58CFF")
        outcome = node(
            "outcome", outcome_label, STATUS_COLORS.get(event.status, "#8FA1BD")
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
                "line": {"color": "#26344c", "width": 1},
                "hovertemplate": "%{customdata}<extra></extra>",
            },
            link={
                "source": sources,
                "target": targets,
                "value": values,
                "color": "rgba(124,156,255,.18)",
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
                    "color": STATUS_COLORS.get(event.status, "#8FA1BD"),
                    "line": {"color": "#edf3ff", "width": 1},
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
