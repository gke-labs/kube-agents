"""Reusable connection state and page gate for provider-backed surfaces."""

from __future__ import annotations

import streamlit as st

from admin_console.connection_controller import (
    CONNECTION_CONTROLLER_KEY,
    ConnectionController,
)
from admin_console.project_config import DeploymentTarget


def current_connection() -> DeploymentTarget | None:
    """Read the same verified target used by Connection's controls."""
    controller = st.session_state.get(CONNECTION_CONTROLLER_KEY)
    if not isinstance(controller, ConnectionController):
        return None
    return controller.connected_target


def require_connection() -> DeploymentTarget:
    """Stop a provider-backed page after rendering consistent connect guidance."""
    target = current_connection()
    if target is None:
        st.info("Connect a project and cluster on Connection.")
        st.page_link(
            "pages/connections.py",
            label="Open Connection",
            icon=":material/cable:",
        )
        st.stop()
    return target
