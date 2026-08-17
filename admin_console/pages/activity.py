"""Search and explain normalized agent activity."""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import streamlit as st

from admin_console.activity_scope import (
    load_activity_snapshot,
    render_activity_load_more,
    render_activity_scope,
)
from admin_console.causal_flow import CausalFlowProjection
from admin_console.connection_session import recover_app_shell
from admin_console.charts import causality_sankey, interaction_timeline
from admin_console.ui import (
    paginated_selectable_table,
    render_telemetry_status,
    section_title,
)

recover_app_shell()
st.title("Activity Explorer")
provider = render_activity_scope()
snapshot = load_activity_snapshot(provider)
all_events = list(snapshot.events)

render_telemetry_status(snapshot)
render_activity_load_more(provider)

with st.container(border=True):
    filter_columns = st.columns([1.2, 1, 1, 1, 1.2])
    users = sorted({event.user_id for event in all_events if event.user_id})
    triggers = sorted({event.trigger_kind.value for event in all_events})
    agents = sorted({event.agent_name for event in all_events})
    statuses = sorted({event.status for event in all_events})
    query = filter_columns[0].text_input(
        "Search", placeholder="message, tool, resource…"
    )
    selected_users = filter_columns[1].multiselect("User", users)
    selected_triggers = filter_columns[2].multiselect("Trigger", triggers)
    selected_agents = filter_columns[3].multiselect("Agent", agents)
    selected_statuses = filter_columns[4].multiselect("Status", statuses)

filter_token = repr(
    (
        query,
        tuple(selected_users),
        tuple(selected_triggers),
        tuple(selected_agents),
        tuple(selected_statuses),
    )
)
previous_filter_token = st.session_state.get("activity_filter_token")
if previous_filter_token is not None and previous_filter_token != filter_token:
    st.query_params.pop("activity_page", None)
    st.query_params.pop("activity_event", None)
st.session_state.activity_filter_token = filter_token


def matches(event) -> bool:
    searchable = f"{event.summary} {event.action_name} {event.tool_name} {event.cluster} {event.namespace} {event.resource} {event.task_id} {event.trace_id}".lower()
    return (
        (not query or query.lower() in searchable)
        and (not selected_users or event.user_id in selected_users)
        and (not selected_triggers or event.trigger_kind.value in selected_triggers)
        and (not selected_agents or event.agent_name in selected_agents)
        and (not selected_statuses or event.status in selected_statuses)
    )


events = [event for event in all_events if matches(event)]
st.caption(f"{len(events)} of {len(all_events)} events · newest first")

if not events:
    st.info("No activity matches these filters.")
    st.stop()

flow_tab, timeline_tab, ledger_tab = st.tabs(["Flow", "Timeline", "Forensic ledger"])

with flow_tab:
    causal_flow = CausalFlowProjection.from_events(events)
    section_title(
        "Causal flow",
        "Normalized OTel source → agent → LLM work → outcome. Edge width is "
        "canonical action count; raw source fields remain in node evidence.",
    )
    if causal_flow.events:
        st.plotly_chart(
            causality_sankey(list(causal_flow.events)),
            width="stretch",
            config={"displayModeBar": False},
        )
    else:
        st.info("No LLM or LLM-produced work exists in this scope.")
    st.caption(causal_flow.summary)

with timeline_tab:
    interaction_options = sorted(
        {event.interaction_id for event in events},
        key=lambda interaction_id: max(
            event.occurred_at
            for event in events
            if event.interaction_id == interaction_id
        ),
        reverse=True,
    )
    selected_interaction = st.selectbox(
        "Interaction",
        interaction_options,
        format_func=lambda value: (
            next(event.action_name for event in events if event.interaction_id == value)
            + f" · {value}"
        ),
    )
    interaction_events = [
        event for event in events if event.interaction_id == selected_interaction
    ]
    st.plotly_chart(
        interaction_timeline(interaction_events),
        width="stretch",
        config={"displayModeBar": False},
    )
    for event in sorted(interaction_events, key=lambda item: item.occurred_at):
        with st.expander(
            f"{event.occurred_at:%H:%M:%S} · {event.agent_name} · {event.action_name}"
        ):
            st.write(event.summary)
            detail_columns = st.columns(4)
            detail_columns[0].caption("ATTRIBUTION")
            detail_columns[0].write(event.attribution.value)
            detail_columns[1].caption("TASK")
            detail_columns[1].code(event.task_id or "—")
            detail_columns[2].caption("TRACE")
            detail_columns[2].code(event.trace_id or "—")
            detail_columns[3].caption("DURATION")
            detail_columns[3].write(
                f"{event.duration_ms / 1000:.2f}s" if event.duration_ms else "—"
            )

with ledger_tab:
    ordered_events = sorted(
        events, key=lambda item: item.occurred_at, reverse=True
    )
    rows = [
        {
            "Time": event.occurred_at.strftime("%Y-%m-%d %H:%M:%S"),
            "Source": event.details.get("source", "—"),
            "Trigger": event.trigger_kind.value,
            "User": event.user_id or "—",
            "Agent": event.agent_name,
            "Action": event.action_name,
            "Tool": event.tool_name or "—",
            "Status": event.status,
            "Attribution": event.attribution.value,
            "Cluster": event.cluster or "—",
            "Event ID": event.event_id,
        }
        for event in ordered_events
    ]
    event_ids = [event.event_id for event in ordered_events]
    requested_event = str(st.query_params.get("activity_event", "")).strip()
    selected_event = (
        requested_event if requested_event in event_ids else event_ids[0]
    )
    clicked_event, _ = paginated_selectable_table(
        rows,
        event_ids,
        selected_event,
        key_prefix="activity_ledger",
        page_query="activity_page",
        selection_query="activity_event",
        state_token=filter_token,
        page_size=50,
        column_config={
            "Action": st.column_config.TextColumn(width="large"),
            "Event ID": st.column_config.TextColumn(width="large"),
        },
    )
    if clicked_event != selected_event:
        st.query_params["activity_event"] = clicked_event
        st.rerun()
    st.query_params["activity_event"] = selected_event
    event = ordered_events[event_ids.index(selected_event)]
    with st.container(border=True):
        st.subheader(event.action_name)
        st.write(event.summary)
        st.json(
            {
                "event_id": event.event_id,
                "interaction_id": event.interaction_id,
                "session_id": event.session_id,
                "task_id": event.task_id,
                "parent_task_id": event.parent_task_id,
                "trace_id": event.trace_id,
                "resource": {
                    "cluster": event.cluster,
                    "namespace": event.namespace,
                    "name": event.resource,
                },
                "attribution": event.attribution.value,
                "evidence": event.details,
            }
        )
        evidence_url = event.details.get("evidence_url")
        if evidence_url:
            st.link_button("Open source evidence", evidence_url)
