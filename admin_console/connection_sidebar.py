"""Connection page rendering over the shared connection controller."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import streamlit as st

from admin_console import connections
from admin_console.connection_controller import (
    CONNECTION_CONTROLLER_KEY,
    ConnectionAction,
    ConnectionController,
    ConnectionPhase,
)
from admin_console.connection_session import initialize_connection_controller
from admin_console.project_config import is_valid_project_id


@st.cache_resource
def connection_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=2, thread_name_prefix="kube-agents-connect")


def connection_controller() -> ConnectionController:
    controller = st.session_state.get(CONNECTION_CONTROLLER_KEY)
    if not isinstance(controller, ConnectionController):
        package_parent = Path(__file__).resolve().parents[1]
        controller = initialize_connection_controller(
            package_parent,
            connection_executor(),
        )
    return controller


@st.fragment(run_every=1)
def maintain_connection() -> None:
    """Poll the one session controller and schedule bounded revalidation."""
    controller = connection_controller()
    if controller.reconcile_persisted_lease():
        st.rerun(scope="app")

    event = controller.poll()
    if event is not None:
        if event.outcome == "connected" and event.message:
            st.toast(event.message)
        st.rerun(scope="app")

    if controller.refresh_due():
        controller.refresh(connection_executor())
        st.rerun(scope="app")

    job = controller.job
    if job is None:
        return
    label = {
        ConnectionAction.PROJECT: "Connecting to the selected project…",
        ConnectionAction.CLUSTER: "Connecting to the selected cluster…",
        ConnectionAction.REFRESH: "Revalidating the cluster connection…",
    }[job.action]
    status = st.status(label, state="running", expanded=True)
    status.caption("You can continue using the navigation while this finishes.")


def _render_connection_actions(
    key: str,
    *,
    connected: bool,
    connecting: bool,
    revalidating: bool = False,
    blocked: bool = False,
    connect_disabled: bool = False,
    can_disconnect: bool = False,
) -> tuple[bool, bool]:
    """Render one primary action and only the currently relevant escape action."""
    show_secondary = connecting or (connected and can_disconnect)
    if show_secondary:
        primary, secondary = st.columns(2)
    else:
        primary = st.container()
        secondary = None
    if connecting:
        primary_label = "Connecting…"
        primary_icon = ":material/progress_activity:"
        state = "connecting"
    elif revalidating:
        primary_label = "Revalidating…"
        primary_icon = ":material/progress_activity:"
        state = "revalidating"
    elif connected:
        primary_label = "Connected"
        primary_icon = ":material/check_circle:"
        state = "connected"
    else:
        primary_label = "Connect"
        primary_icon = ":material/cable:"
        state = "disconnected"
    primary_clicked = primary.button(
        primary_label,
        type="primary",
        icon=primary_icon,
        width="stretch",
        disabled=(
            connected or connecting or revalidating or blocked or connect_disabled
        ),
        key=f"{key}_primary_{state}",
    )
    secondary_clicked = False
    if secondary is not None:
        secondary_clicked = secondary.button(
            "Abort" if connecting else "Disconnect",
            icon=(":material/cancel:" if connecting else ":material/link_off:"),
            width="stretch",
            disabled=blocked and not (connected and can_disconnect),
            key=f"{key}_secondary_{'abort' if connecting else 'disconnect'}",
        )
    return primary_clicked, secondary_clicked


def _cluster_key(cluster: connections.ClusterInfo) -> str:
    return f"{cluster.name}|{cluster.location}"


def render_connection_controls() -> None:
    """Render project and cluster controls from the same state pages consume."""
    controller = connection_controller()
    candidates = controller.project_candidates
    candidate_sources = {
        candidate.project_id: candidate.source for candidate in candidates
    }
    project_ids = [candidate.project_id for candidate in candidates]
    preferred_index = (
        project_ids.index(controller.project_id)
        if controller.project_id in project_ids
        else (0 if project_ids else None)
    )
    project_connecting = controller.action is ConnectionAction.PROJECT
    project_connected = controller.project.phase is ConnectionPhase.CONNECTED

    with st.container(border=True):
        st.markdown("#### Step 1 · Project")
        st.caption("Verify Google Cloud access and discover clusters.")
        selected_option = st.selectbox(
            "Project",
            project_ids,
            index=preferred_index,
            format_func=lambda value: (
                f"{value} · {candidate_sources[value]}"
                if value in candidate_sources
                else value
            ),
            placeholder="Select or enter a Google Cloud project ID",
            accept_new_options=True,
            disabled=project_connected or controller.working,
            label_visibility="collapsed",
            key="connection_project_option",
        )
        project_id = str(selected_option or "").strip()
        project_is_valid = is_valid_project_id(project_id)
        if project_id and not project_is_valid:
            st.error("Enter a valid Google Cloud project ID.")
        if project_is_valid and project_id != controller.project_id:
            controller.select_project(project_id)
            st.query_params["project"] = project_id
            st.query_params.pop("cluster", None)
            st.query_params.pop("location", None)
            st.rerun()

        project_connect_clicked, project_secondary_clicked = (
            _render_connection_actions(
                "project_connection",
                connected=project_connected,
                connecting=project_connecting,
                blocked=controller.working and not project_connecting,
                connect_disabled=not project_is_valid,
                can_disconnect=(
                    project_connected
                    and controller.connected_target is None
                    and not controller.working
                ),
            )
        )
        if project_connect_clicked:
            controller.connect_project(connection_executor())
            st.rerun()
        if project_secondary_clicked:
            if project_connecting:
                controller.abort()
                st.toast("Project connection aborted.")
            else:
                controller.disconnect_project()
                st.session_state.pop("connection_project_option", None)
                st.query_params.pop("cluster", None)
                st.query_params.pop("location", None)
                st.toast("Disconnected from project.")
            st.rerun()
        if controller.project.error and not project_connecting:
            st.error(controller.project.error)

    if not project_connected:
        with st.container(border=True):
            st.markdown("#### Step 2 · Cluster")
            st.caption("Connect the project to choose its kube-agents host.")
        return

    report = controller.project.report
    clusters = report.clusters if report else ()
    display_target = controller.connected_target or controller.selected_target
    if display_target and not any(
        item.name == display_target.cluster_name
        and item.location == display_target.location
        for item in clusters
    ):
        clusters = (
            *clusters,
            connections.ClusterInfo(
                display_target.cluster_name,
                display_target.location,
                "RUNNING",
                display_target.source == "kube-agents-host label",
            ),
        )
    ordered_clusters = sorted(
        clusters,
        key=lambda cluster: (
            not cluster.is_kube_agents_host,
            cluster.name,
            cluster.location,
        ),
    )
    cluster_by_key = {_cluster_key(cluster): cluster for cluster in ordered_clusters}
    selected_target = controller.selected_target
    selected_key = (
        f"{selected_target.cluster_name}|{selected_target.location}"
        if selected_target
        else ""
    )
    cluster_keys = list(cluster_by_key)
    selected_index = cluster_keys.index(selected_key) if selected_key in cluster_keys else 0
    cluster_connecting = controller.action is ConnectionAction.CLUSTER
    cluster_refreshing = controller.action is ConnectionAction.REFRESH
    cluster_connected = controller.cluster.phase is ConnectionPhase.CONNECTED

    with st.container(border=True):
        st.markdown("#### Step 2 · Cluster")
        st.caption("Verify the selected kube-agents runtime.")

        def cluster_label(cluster_key: str) -> str:
            cluster = cluster_by_key[cluster_key]
            suffix = " · kube-agents host" if cluster.is_kube_agents_host else ""
            return f"{cluster.name} · {cluster.location}{suffix}"

        selected_key = st.selectbox(
            "Cluster",
            cluster_keys,
            index=selected_index if cluster_keys else None,
            format_func=cluster_label,
            placeholder="No GKE clusters found",
            disabled=cluster_connected or controller.working,
            label_visibility="collapsed",
            key=f"cluster_connection_option:{controller.project_id}",
        )
        selected_cluster = cluster_by_key.get(str(selected_key)) if selected_key else None
        if selected_cluster is not None:
            target = controller.target_for_cluster(selected_cluster)
            current = controller.selected_target
            if current is None or (
                current.project_id,
                current.cluster_name,
                current.location,
            ) != (target.project_id, target.cluster_name, target.location):
                controller.select_target(target)
            st.query_params["cluster"] = target.cluster_name
            st.query_params["location"] = target.location

        cluster_connect_clicked, cluster_secondary_clicked = (
            _render_connection_actions(
                "cluster_connection",
                connected=cluster_connected,
                connecting=cluster_connecting,
                revalidating=cluster_refreshing and not cluster_connected,
                blocked=controller.working and not cluster_connecting,
                connect_disabled=selected_cluster is None,
                can_disconnect=cluster_connected,
            )
        )
        if cluster_connect_clicked:
            controller.connect_cluster(connection_executor())
            st.rerun()
        if cluster_secondary_clicked:
            if cluster_connecting:
                controller.abort()
                st.toast("Cluster connection aborted.")
            else:
                controller.disconnect_cluster()
                st.toast("Disconnected from cluster.")
            st.rerun()
        if cluster_refreshing and cluster_connected:
            st.caption("Revalidating in the background. Disconnect remains available.")
        elif cluster_refreshing:
            st.caption("Revalidating the saved connection before enabling runtime access.")
        if not cluster_connected and not cluster_keys:
            st.info("No GKE clusters were found in this project.")
        elif not cluster_connected and not any(
            cluster.is_kube_agents_host for cluster in clusters
        ):
            st.caption("No kube-agents host label was found; choose the host cluster.")
        elif not cluster_connected and len(
            tuple(c for c in clusters if c.is_kube_agents_host)
        ) > 1:
            st.caption("Multiple host labels were found; choose the intended cluster.")
        if controller.cluster.error and not cluster_connecting:
            st.error(controller.cluster.error)

    if controller.persistence_error:
        st.warning(controller.persistence_error)
