"""Streamlit entry point for the Kube Agents Console."""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

# Streamlit executes this file as a script and puts ``admin_console/`` rather
# than the repository root on sys.path. Add the package parent before importing
# application modules so local, container, and proxied launches behave alike.
PACKAGE_PARENT = Path(__file__).resolve().parents[1]
APP_DIRECTORY = Path(__file__).resolve().parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from admin_console.connection_gate import current_connection
from admin_console.connection_session import (
    APP_SHELL_ACTIVE_KEY,
    initialize_connection_controller,
)
from admin_console.connection_sidebar import connection_executor, maintain_connection
from admin_console.ui import apply_theme

st.set_page_config(
    page_title="Kube Agents Console",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)
apply_theme()

controller = initialize_connection_controller(PACKAGE_PARENT, connection_executor())
authenticated_user = controller.account
st.session_state[APP_SHELL_ACTIVE_KEY] = True

with st.sidebar:
    st.markdown("## ◈ Kube Agents")
    st.divider()

pages = {
    "Setup": [
        st.Page(
            APP_DIRECTORY / "pages" / "connections.py",
            title="Connection",
            icon=":material/cable:",
            default=True,
        ),
    ],
    "Agentic": [
        st.Page(
            APP_DIRECTORY / "pages" / "chat.py",
            title="Chat",
            icon=":material/forum:",
        ),
    ],
    "Observability": [
        st.Page(
            APP_DIRECTORY / "pages" / "overview.py",
            title="Overview",
            icon=":material/space_dashboard:",
        ),
        st.Page(
            APP_DIRECTORY / "pages" / "activity.py",
            title="Activity Explorer",
            icon=":material/account_tree:",
        ),
        st.Page(
            APP_DIRECTORY / "pages" / "kanban.py",
            title="Task Kanban",
            icon=":material/view_kanban:",
        ),
        st.Page(
            APP_DIRECTORY / "pages" / "autonomous.py",
            title="Scheduled Cron",
            icon=":material/autorenew:",
            url_path="scheduled-cron",
        ),
    ],
}

connection_is_current = current_connection() is not None
navigation = st.navigation(pages)

with st.sidebar:
    st.divider()
    maintain_connection()
    st.caption(f"Signed in as {authenticated_user}")
    if not connection_is_current:
        st.caption("Connect a project and cluster to enable Observability")

navigation.run()
