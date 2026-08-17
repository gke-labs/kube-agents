"""Fleet activity overview."""

from __future__ import annotations

import html
import sys
from collections import Counter
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import streamlit as st

from admin_console.charts import activity_over_time, attribution_donut
from admin_console.connection_session import recover_app_shell
from admin_console.activity_scope import (
    load_activity_snapshot,
    render_activity_load_more,
    render_activity_scope,
)
from admin_console.domain import AttributionLevel, TriggerKind
from admin_console.ui import render_telemetry_status, section_title, status_label

recover_app_shell()
st.title("Overview")
provider = render_activity_scope()
snapshot = load_activity_snapshot(provider)
events = list(snapshot.events)
interactions = {event.interaction_id for event in events}
human_interactions = {
    event.interaction_id for event in events if event.trigger_kind == TriggerKind.HUMAN
}
autonomous_interactions = {
    event.interaction_id
    for event in events
    if event.trigger_kind
    in {
        TriggerKind.CRON,
        TriggerKind.EVENT,
        TriggerKind.RETRY,
        TriggerKind.AGENT_FOLLOWUP,
    }
}
errors = [event for event in events if event.status in {"failed", "blocked"}]
linked = [
    event
    for event in events
    if event.attribution in {AttributionLevel.EXPLICIT, AttributionLevel.INHERITED}
]

render_telemetry_status(snapshot)
render_activity_load_more(provider)

metric_columns = st.columns(5)
metrics = (
    ("Interactions", len(interactions), "selected window"),
    ("Human initiated", len(human_interactions), f"{len(events)} total events"),
    ("Autonomous", len(autonomous_interactions), "cron + event triggers"),
    ("Needs attention", len(errors), "failed or blocked"),
    (
        "Attribution",
        f"{round(100 * len(linked) / max(len(events), 1))}%",
        "explicit + inherited",
    ),
)
for column, (label, value, delta) in zip(metric_columns, metrics, strict=True):
    column.metric(label, value, delta)

left, right = st.columns([1.7, 1], gap="large")
with left:
    section_title("Activity pulse", "Events grouped into 15-minute windows by trigger.")
    st.plotly_chart(
        activity_over_time(events),
        width="stretch",
        config={"displayModeBar": False},
    )
with right:
    section_title(
        "Attribution coverage", "How confidently activity links to its origin."
    )
    st.plotly_chart(
        attribution_donut(events),
        width="stretch",
        config={"displayModeBar": False},
    )

section_title("Recent work")

latest_by_interaction = {}
for event in sorted(events, key=lambda item: item.occurred_at):
    latest_by_interaction[event.interaction_id] = event

for event in sorted(
    latest_by_interaction.values(), key=lambda item: item.occurred_at, reverse=True
)[:5]:
    trigger = event.trigger_kind.value.replace("_", " ").title()
    identity = event.user_id or trigger
    title = html.escape(f"{status_label(event.status)} · {event.action_name}")
    summary = html.escape(event.summary)
    metadata = html.escape(
        f"{event.occurred_at:%H:%M UTC} · {identity} · {event.agent_name} "
        f"· {event.attribution.value} attribution"
    )
    st.markdown(
        f"""
        <div class="ka-card">
          <div class="ka-card-title">{title}</div>
          <div>{summary}</div>
          <div class="ka-card-meta">{metadata}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

agent_counts = Counter(event.agent_name for event in events)
with st.expander("Most active agents"):
    st.bar_chart(dict(agent_counts), horizontal=True)

with st.expander("Read provenance"):
    st.json(
        {
            "project": snapshot.project_id,
            "cluster": snapshot.cluster or "all",
            "from": snapshot.start_time.isoformat(),
            "to": snapshot.end_time.isoformat(),
            "sources": [
                {
                    "name": source.name,
                    "status": source.status,
                    "records_read": source.records_read,
                    "truncated": source.truncated,
                    "detail": source.detail,
                }
                for source in snapshot.sources
            ],
        }
    )
