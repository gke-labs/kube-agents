"""Reusable connection state and page gate for provider-backed surfaces."""

from __future__ import annotations

import streamlit as st

from admin_console.project_config import DeploymentTarget


def current_connection() -> DeploymentTarget | None:
    """Return the verified connection only when it matches the current scope."""
    target = st.session_state.get("connected_target")
    if not isinstance(target, DeploymentTarget):
        return None
    if (
        target.project_id != st.session_state.get("selected_project", "")
        or target.cluster_name != st.session_state.get("selected_cluster", "")
        or target.location != st.session_state.get("selected_location", "")
    ):
        return None
    return target


def require_connection() -> DeploymentTarget:
    """Stop a provider-backed page after rendering consistent connect guidance."""
    target = current_connection()
    if target is None:
        st.info("Connect to kube-agents on Connection.")
        st.page_link(
            "pages/connections.py",
            label="Open Connection",
            icon=":material/cable:",
        )
        st.stop()
    return target
