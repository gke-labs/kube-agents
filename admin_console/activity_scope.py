"""Page-local controls for bounded Cloud activity reads."""

from __future__ import annotations

import streamlit as st

from admin_console.connection_gate import require_connection
from admin_console.telemetry import CloudTelemetryProvider, TelemetrySnapshot

WINDOW_OPTIONS = {
    "1h": ("Last hour", 1),
    "6h": ("Last 6 hours", 6),
    "24h": ("Last 24 hours", 24),
    "72h": ("Last 3 days", 72),
    "168h": ("Last 7 days", 168),
    "720h": ("Last 30 days", 720),
}


def _query_value(name: str, default: str = "") -> str:
    return str(st.query_params.get(name, default)).strip()


def render_activity_scope() -> CloudTelemetryProvider:
    """Render compact page-local scope controls and return the live provider."""
    target = require_connection()

    requested_window = _query_value("window", "24h")
    if requested_window not in WINDOW_OPTIONS:
        requested_window = "24h"
    requested_cluster = _query_value("activity_cluster")
    if requested_cluster == "all":
        requested_cluster = ""
    with st.container(border=True):
        columns = st.columns([1, 1, 1.5])
        selected_window = columns[0].selectbox(
            "Time window",
            list(WINDOW_OPTIONS),
            index=list(WINDOW_OPTIONS).index(requested_window),
            format_func=lambda value: WINDOW_OPTIONS[value][0],
        )
        cluster_options = ["", target.cluster_name]
        selected_cluster = columns[1].selectbox(
            "Cluster",
            cluster_options,
            index=(
                cluster_options.index(requested_cluster)
                if requested_cluster in cluster_options
                else 1
            ),
            format_func=lambda value: value or "All clusters",
        )
        refresh = columns[2].button(
            "Refresh",
            icon=":material/refresh:",
            width="stretch",
        )

    scope_changed = (
        _query_value("window") != selected_window
        or _query_value("activity_cluster") != (selected_cluster or "all")
    )
    if _query_value("window") != selected_window:
        st.query_params["window"] = selected_window
    cluster_parameter = selected_cluster or "all"
    if _query_value("activity_cluster") != cluster_parameter:
        st.query_params["activity_cluster"] = cluster_parameter
    if scope_changed:
        st.query_params.pop("activity_page", None)
        st.query_params.pop("activity_event", None)
    st.query_params.pop("trace_pages", None)
    if refresh:
        st.session_state.telemetry_refresh = (
            st.session_state.get("telemetry_refresh", 0) + 1
        )
        st.session_state.telemetry_load_reason = "refresh"
        st.query_params.pop("activity_page", None)
        st.query_params.pop("activity_event", None)

    account = st.session_state.get("authenticated_user", "")
    provider_key = (
        target.project_id,
        selected_cluster,
        target.namespace,
        account,
        selected_window,
        st.session_state.get("telemetry_refresh", 0),
    )
    if st.session_state.get("telemetry_provider_key") != provider_key:
        st.session_state.telemetry_provider = CloudTelemetryProvider(
            target.project_id,
            account=account,
            cluster=selected_cluster,
            namespace=target.namespace,
            hours=WINDOW_OPTIONS[selected_window][1],
        )
        st.session_state.telemetry_provider_key = provider_key
    return st.session_state.telemetry_provider


def load_activity_snapshot(provider: CloudTelemetryProvider) -> TelemetrySnapshot:
    """Load one snapshot with consistent visible feedback on every page."""
    if provider.loaded:
        return provider.get_snapshot()
    reason = st.session_state.pop("telemetry_load_reason", "initial")
    label = (
        "Refreshing activity from Cloud Logging and Cloud Trace…"
        if reason == "refresh"
        else "Loading activity from Cloud Logging and Cloud Trace…"
    )
    with st.spinner(label, show_time=True):
        return provider.get_snapshot()


def render_activity_load_more(provider: CloudTelemetryProvider) -> None:
    """Append bounded source pages while keeping source cursors server-side."""
    if not provider.can_load_more:
        return
    action, context = st.columns([1, 3], vertical_alignment="center")
    if action.button(
        "Load more activity",
        icon=":material/add:",
        width="stretch",
    ):
        with st.spinner("Loading more activity…", show_time=True):
            provider.load_more()
        st.query_params.pop("activity_page", None)
        st.query_params.pop("activity_event", None)
        st.rerun()
    context.caption("Fetch the next bounded Logging and Trace pages.")
