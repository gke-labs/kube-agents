"""Streamlit entry point for the Kube Agents Console."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import streamlit as st

# Streamlit executes this file as a script and puts ``admin_console/`` rather
# than the repository root on sys.path. Add the package parent before importing
# application modules so local, container, and proxied launches behave alike.
PACKAGE_PARENT = Path(__file__).resolve().parents[1]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from admin_console.connection_gate import current_connection
from admin_console.connection_persistence import load_connection
from admin_console.connection_sidebar import maintain_connection
from admin_console.project_config import (
    build_project_candidates,
    is_valid_cluster_name,
    is_valid_location,
    is_valid_project_id,
    load_provisioned_target,
)
from admin_console.ui import apply_theme

st.set_page_config(
    page_title="Kube Agents Console",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)
apply_theme()

authenticated_user = os.environ.get("KUBE_AGENTS_ADMIN_USER", "").strip()
if not authenticated_user:
    st.error(
        "No verified local identity is available. Start the portal with "
        "`scripts/admin_portal.sh` so the active gcloud login can be verified."
    )
    st.stop()
st.session_state.authenticated_user = authenticated_user
persisted_connection = load_connection(authenticated_user)
st.session_state.persisted_connection = persisted_connection

with st.sidebar:
    st.markdown("## ◈ Kube Agents")
    st.divider()

provisioned_target = load_provisioned_target(
    PACKAGE_PARENT / "k8s-operator" / "scripts" / "vars.sh"
)
st.session_state.provisioned_target = provisioned_target
query_project = str(st.query_params.get("project", "")).strip()
configured_project = os.environ.get("KUBE_AGENTS_GCLOUD_PROJECT", "").strip()
persisted_project = (
    persisted_connection.target.project_id if persisted_connection else ""
)
project_candidates = build_project_candidates(
    provisioned_target,
    configured_project,
    query_project,
    persisted_project,
)
project_ids = [candidate.project_id for candidate in project_candidates]
candidate_sources = {
    candidate.project_id: candidate.source for candidate in project_candidates
}
st.session_state.project_candidates = project_candidates
st.session_state.project_candidate_sources = candidate_sources

# The Connection page owns project selection. A valid URL selection wins;
# otherwise initialize once from the active gcloud/provisioned candidates.
# Disconnect removes the saved connection while retaining its URL scope.
if is_valid_project_id(query_project):
    st.session_state.selected_project = query_project
elif "selected_project" not in st.session_state:
    st.session_state.selected_project = (
        persisted_project or (project_ids[0] if project_ids else "")
    )
selected_project = st.session_state.get("selected_project", "")
if selected_project and query_project != selected_project:
    st.query_params["project"] = selected_project

query_cluster = str(st.query_params.get("cluster", "")).strip()
query_location = str(st.query_params.get("location", "")).strip()
session_cluster = str(st.session_state.get("selected_cluster", "")).strip()
session_location = str(st.session_state.get("selected_location", "")).strip()
if is_valid_cluster_name(query_cluster) and is_valid_location(query_location):
    st.session_state.selected_cluster = query_cluster
    st.session_state.selected_location = query_location
elif is_valid_cluster_name(session_cluster) and is_valid_location(session_location):
    # Page navigation may clear query parameters. A verified session selection
    # must survive.
    st.session_state.selected_cluster = session_cluster
    st.session_state.selected_location = session_location
elif (
    persisted_connection
    and persisted_connection.target.project_id == selected_project
):
    st.session_state.selected_cluster = persisted_connection.target.cluster_name
    st.session_state.selected_location = persisted_connection.target.location

selected_cluster = st.session_state.get("selected_cluster", "")
selected_location = st.session_state.get("selected_location", "")
if is_valid_cluster_name(selected_cluster) and is_valid_location(selected_location):
    if query_cluster != selected_cluster:
        st.query_params["cluster"] = selected_cluster
    if query_location != selected_location:
        st.query_params["location"] = selected_location

pages = {
    "Setup": [
        st.Page(
            "pages/connections.py",
            title="Connection",
            icon=":material/cable:",
            default=True,
        ),
    ],
    "Agentic": [
        st.Page(
            "pages/chat.py",
            title="Chat",
            icon=":material/forum:",
        ),
    ],
    "Observability": [
        st.Page(
            "pages/overview.py",
            title="Overview",
            icon=":material/space_dashboard:",
        ),
        st.Page(
            "pages/activity.py",
            title="Activity Explorer",
            icon=":material/account_tree:",
        ),
        st.Page(
            "pages/kanban.py",
            title="Task Kanban",
            icon=":material/view_kanban:",
        ),
        st.Page(
            "pages/autonomous.py",
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
        st.caption("Connect to enable Observability")

navigation.run()
