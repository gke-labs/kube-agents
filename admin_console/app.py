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

from admin_console.connection_controller import (
    CONNECTION_CONTROLLER_KEY,
    ConnectionController,
)
from admin_console.connection_gate import current_connection
from admin_console.connection_persistence import load_connection
from admin_console.connection_sidebar import connection_executor, maintain_connection
from admin_console.project_config import (
    DeploymentTarget,
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

with st.sidebar:
    st.markdown("## ◈ Kube Agents")
    st.divider()

provisioned_target = load_provisioned_target(
    PACKAGE_PARENT / "k8s-operator" / "scripts" / "vars.sh"
)
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

query_cluster = str(st.query_params.get("cluster", "")).strip()
query_location = str(st.query_params.get("location", "")).strip()

# The controller is the session's only connection state. URL and persisted
# values initialize or project its selection; neither can silently reconnect.
initial_project = (
    query_project
    if is_valid_project_id(query_project)
    else persisted_project or (project_ids[0] if project_ids else "")
)
initial_target = None
if is_valid_cluster_name(query_cluster) and is_valid_location(query_location):
    namespace = (
        provisioned_target.namespace
        if provisioned_target
        and provisioned_target.project_id == initial_project
        and provisioned_target.cluster_name == query_cluster
        and provisioned_target.location == query_location
        else "kubeagents-system"
    )
    initial_target = DeploymentTarget(
        initial_project,
        query_cluster,
        query_location,
        namespace,
        "manual selection",
    )
elif persisted_connection and persisted_connection.target.project_id == initial_project:
    initial_target = persisted_connection.target

controller = st.session_state.get(CONNECTION_CONTROLLER_KEY)
if not isinstance(controller, ConnectionController):
    persisted_scope_matches = bool(
        persisted_connection
        and initial_target
        and (
            initial_target.project_id,
            initial_target.cluster_name,
            initial_target.location,
            initial_target.namespace,
        )
        == (
            persisted_connection.target.project_id,
            persisted_connection.target.cluster_name,
            persisted_connection.target.location,
            persisted_connection.target.namespace,
        )
    )
    if persisted_connection and persisted_scope_matches:
        controller = ConnectionController.restored(
            authenticated_user,
            persisted_connection.target,
            persisted_connection.verified_at,
        )
        controller.provisioned_target = provisioned_target
        controller.project_candidates = tuple(project_candidates)
        controller.refresh(connection_executor())
    else:
        controller = ConnectionController(
            account=authenticated_user,
            project_id=initial_project,
            selected_target=initial_target,
            provisioned_target=provisioned_target,
            project_candidates=tuple(project_candidates),
        )
    st.session_state[CONNECTION_CONTROLLER_KEY] = controller
else:
    controller.account = authenticated_user
    controller.provisioned_target = provisioned_target
    controller.project_candidates = tuple(project_candidates)
    if not controller.connected_project and not controller.working:
        requested_project = (
            query_project
            if is_valid_project_id(query_project)
            else controller.project_id or initial_project
        )
        controller.select_project(requested_project)
        explicit_target = (
            initial_target
            if is_valid_cluster_name(query_cluster)
            and is_valid_location(query_location)
            else None
        )
        if explicit_target and explicit_target.project_id == controller.project_id:
            controller.selected_target = explicit_target
        elif controller.selected_target is None and initial_target:
            controller.selected_target = initial_target

if controller.project_id:
    st.query_params["project"] = controller.project_id
if controller.selected_target:
    st.query_params["cluster"] = controller.selected_target.cluster_name
    st.query_params["location"] = controller.selected_target.location

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
        st.caption("Connect a project and cluster to enable Observability")

navigation.run()
