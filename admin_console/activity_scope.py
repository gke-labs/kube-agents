"""Page-local controls for bounded Cloud activity reads."""

from __future__ import annotations

import streamlit as st

from admin_console.connection_gate import require_connection
from admin_console.telemetry import MAX_TRACE_PAGES, CloudTelemetryProvider

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


def _trace_pages() -> int:
    try:
        return max(1, min(int(_query_value("trace_pages", "1")), MAX_TRACE_PAGES))
    except ValueError:
        return 1


def render_activity_scope() -> CloudTelemetryProvider:
    """Render compact page-local scope controls and return the live provider."""
    target = require_connection()

    requested_window = _query_value("window", "24h")
    if requested_window not in WINDOW_OPTIONS:
        requested_window = "24h"
    requested_cluster = _query_value("activity_cluster")
    if requested_cluster == "all":
        requested_cluster = ""
    requested_trace_pages = _trace_pages()

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
        requested_trace_pages = 1
        st.query_params.pop("trace_pages", None)
    elif requested_trace_pages > 1:
        if _query_value("trace_pages") != str(requested_trace_pages):
            st.query_params["trace_pages"] = str(requested_trace_pages)
    else:
        st.query_params.pop("trace_pages", None)
    if refresh:
        st.session_state.telemetry_refresh = (
            st.session_state.get("telemetry_refresh", 0) + 1
        )

    provider_key = (
        target.project_id,
        selected_cluster,
        selected_window,
        requested_trace_pages,
        st.session_state.get("telemetry_refresh", 0),
    )
    if st.session_state.get("telemetry_provider_key") != provider_key:
        st.session_state.telemetry_provider = CloudTelemetryProvider(
            target.project_id,
            account=st.session_state.get("authenticated_user", ""),
            cluster=selected_cluster,
            namespace=target.namespace,
            hours=WINDOW_OPTIONS[selected_window][1],
            trace_pages=requested_trace_pages,
        )
        st.session_state.telemetry_provider_key = provider_key
    return st.session_state.telemetry_provider
