"""Connection lifecycle and Setup-page controls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import streamlit as st

from admin_console import connections
from admin_console.connection_persistence import (
    PersistedConnection,
    delete_connection,
    save_connection,
)
from admin_console.project_config import (
    DeploymentTarget,
    is_valid_cluster_name,
    is_valid_location,
    is_valid_project_id,
)

CONNECTION_REFRESH_INTERVAL = timedelta(minutes=10)
CONNECTION_ACTION_KEY = "connection_action"
CONNECTION_ACTIONS = {"connect", "select"}


def clear_connected_state() -> None:
    """Forget the verified target and its derived providers, but retain scope."""
    for key in (
        "connected_target",
        "telemetry_provider",
        "telemetry_provider_key",
        "telemetry_refresh",
        "connection_last_verified_at",
    ):
        st.session_state.pop(key, None)


def _persist_connection(
    target: DeploymentTarget,
    verified_at: datetime,
) -> None:
    """Persist only non-secret target metadata for the verified local account."""
    try:
        save_connection(
            str(st.session_state.get("authenticated_user", "")),
            target,
            verified_at,
        )
        st.session_state.persisted_connection = PersistedConnection(
            target,
            str(st.session_state.get("authenticated_user", "")),
            verified_at,
        )
        st.session_state.pop("connection_persistence_error", None)
    except (OSError, ValueError) as exc:
        st.session_state.connection_persistence_error = (
            f"Connection is active for this browser session but could not be "
            f"persisted ({type(exc).__name__})."
        )


def _mark_connected(
    target: DeploymentTarget,
    report: connections.ConnectionReport,
) -> None:
    st.session_state.connected_target = target
    st.session_state.connection_last_verified_at = report.checked_at
    _persist_connection(target, report.checked_at)


def _run_target_check(target: DeploymentTarget) -> connections.ConnectionReport:
    report = connections.run_connection_checks(
        target.project_id,
        expected_target=target,
        include_agent_runtime_probe=True,
    )
    st.session_state[f"connection_report:{target.project_id}"] = report
    return report


def _set_scope(project_id: str, cluster_name: str = "", location: str = "") -> None:
    st.session_state.selected_project = project_id
    st.query_params["project"] = project_id
    if is_valid_cluster_name(cluster_name) and is_valid_location(location):
        st.session_state.selected_cluster = cluster_name
        st.session_state.selected_location = location
        st.query_params["cluster"] = cluster_name
        st.query_params["location"] = location
    else:
        st.session_state.pop("selected_cluster", None)
        st.session_state.pop("selected_location", None)
        st.query_params.pop("cluster", None)
        st.query_params.pop("location", None)


def _target_for_cluster(
    project_id: str, cluster: connections.ClusterInfo
) -> DeploymentTarget:
    provisioned = st.session_state.get("provisioned_target")
    namespace = (
        provisioned.namespace
        if provisioned
        and provisioned.project_id == project_id
        and provisioned.cluster_name == cluster.name
        and provisioned.location == cluster.location
        else "kubeagents-system"
    )
    return DeploymentTarget(
        project_id,
        cluster.name,
        cluster.location,
        namespace=namespace,
        source=(
            "kube-agents-host label"
            if cluster.is_kube_agents_host
            else "manual selection"
        ),
    )


@st.fragment(run_every=CONNECTION_REFRESH_INTERVAL)
def maintain_connection() -> None:
    """Restore or revalidate directly under a dependency-loading indicator."""
    active_target = st.session_state.get("connected_target")
    is_connected = active_target is not None
    current_project = str(st.session_state.get("selected_project", "")).strip()

    persisted = st.session_state.get("persisted_connection")
    restore_key = ""
    if isinstance(persisted, PersistedConnection):
        target = persisted.target
        restore_key = f"{target.project_id}|{target.cluster_name}|{target.location}"
        scope_matches = (
            target.project_id == current_project
            and target.cluster_name == st.session_state.get("selected_cluster", "")
            and target.location == st.session_state.get("selected_location", "")
        )
        if (
            not is_connected
            and scope_matches
            and st.session_state.get("connection_restore_attempted") != restore_key
        ):
            st.session_state.connection_restore_attempted = restore_key
            try:
                with st.spinner(
                    "Please wait while we restore and verify your saved connection…",
                    show_time=True,
                    width="stretch",
                ):
                    report = _run_target_check(target)
            except Exception as exc:
                st.session_state.connection_action_error = (
                    "The saved connection could not be restored "
                    f"({type(exc).__name__}). Retry Connect."
                )
                return
            if connections.connection_is_ready(report):
                _mark_connected(target, report)
                st.rerun(scope="app")
            return

    last_verified = st.session_state.get("connection_last_verified_at")
    existing_report = (
        st.session_state.get(f"connection_report:{active_target.project_id}")
        if is_connected
        else None
    )
    if (
        is_connected
        and not isinstance(last_verified, datetime)
        and isinstance(existing_report, connections.ConnectionReport)
        and connections.connection_is_ready(existing_report)
    ):
        _mark_connected(active_target, existing_report)
        last_verified = existing_report.checked_at
    refresh_due = (
        is_connected
        and isinstance(last_verified, datetime)
        and datetime.now(timezone.utc) - last_verified >= CONNECTION_REFRESH_INTERVAL
    )
    if refresh_due:
        try:
            with st.spinner(
                "Please wait while we revalidate your connection…",
                show_time=True,
                width="stretch",
            ):
                report = _run_target_check(active_target)
        except Exception:
            clear_connected_state()
            st.session_state.connection_refresh_failed = True
            st.rerun(scope="app")
        if connections.connection_is_ready(report):
            _mark_connected(active_target, report)
        else:
            clear_connected_state()
            st.session_state.connection_refresh_failed = True
            st.rerun(scope="app")


def render_connection_controls() -> None:
    """Render project, cluster, connect, and disconnect controls on Setup."""
    active_target = st.session_state.get("connected_target")
    is_connected = active_target is not None
    current_project = str(st.session_state.get("selected_project", "")).strip()

    candidates = st.session_state.get("project_candidates", ())
    candidate_sources = st.session_state.get("project_candidate_sources", {})
    project_ids = [candidate.project_id for candidate in candidates]
    preferred_index = (
        project_ids.index(current_project)
        if current_project in project_ids
        else (0 if project_ids else None)
    )
    pending_action = st.session_state.get(CONNECTION_ACTION_KEY)
    action_kind = (
        str(pending_action.get("kind", ""))
        if isinstance(pending_action, dict)
        else ""
    )
    if action_kind not in CONNECTION_ACTIONS:
        action_kind = ""
    is_working = bool(action_kind)

    with st.container():
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
            disabled=is_connected or is_working,
            key="connection_project_option",
        )
        requested_project = str(selected_option or "").strip()
        project_is_valid = is_valid_project_id(requested_project)
        if requested_project and not project_is_valid:
            st.error("Enter a valid Google Cloud project ID.")

        if (
            not is_connected
            and project_is_valid
            and requested_project != current_project
        ):
            clear_connected_state()
            _set_scope(requested_project)
            st.rerun()

        project_id = requested_project
        report = (
            st.session_state.get(f"connection_report:{project_id}")
            if project_is_valid
            else None
        )
        host_count = len(report.kube_agents_hosts) if report else 0
        manual_selection_required = bool(
            report and report.clusters and host_count != 1 and not is_connected
        )
        if is_connected:
            st.caption(
                f"Cluster: {active_target.cluster_name} · {active_target.location}"
            )
        else:
            st.caption("Cluster is selected automatically from kube-agents-host=true.")

        connect, disconnect = st.columns(2)
        connecting = action_kind == "connect"
        connect_clicked = connect.button(
            "Connecting…" if connecting else "Connect",
            type="primary",
            icon=(":material/progress_activity:" if connecting else ":material/cable:"),
            width="stretch",
            disabled=(
                is_connected
                or manual_selection_required
                or is_working
                or not project_is_valid
            ),
            key=(
                "connect_to_kube_agents_busy"
                if connecting
                else "connect_to_kube_agents"
            ),
        )
        disconnect_clicked = disconnect.button(
            "Disconnect",
            icon=":material/link_off:",
            width="stretch",
            disabled=not is_connected or is_working,
            key="disconnect_project",
        )

        if disconnect_clicked:
            clear_connected_state()
            delete_connection()
            st.session_state.persisted_connection = None
            st.session_state.pop("connection_restore_attempted", None)
            st.toast("Disconnected from kube-agents.")
            st.rerun()

        if connect_clicked:
            st.session_state[CONNECTION_ACTION_KEY] = {
                "kind": "connect",
                "project_id": project_id,
            }
            st.rerun()

        cluster_by_key: dict[str, connections.ClusterInfo] = {}
        if manual_selection_required:
            if host_count == 0:
                st.error(
                    "Automatic cluster detection failed: no GKE cluster is labeled "
                    "kube-agents-host=true. Select the cluster that hosts kube-agents."
                )
            else:
                st.error(
                    "Automatic cluster detection failed: "
                    f"{host_count} GKE clusters are labeled kube-agents-host=true. "
                    "Select the intended host."
                )

            ordered_clusters = sorted(
                report.clusters,
                key=lambda cluster: (
                    not cluster.is_kube_agents_host,
                    cluster.name,
                    cluster.location,
                ),
            )
            cluster_by_key = {
                f"{cluster.name}|{cluster.location}": cluster
                for cluster in ordered_clusters
            }

            def cluster_label(key: str) -> str:
                cluster = cluster_by_key[key]
                suffix = " · kube-agents host" if cluster.is_kube_agents_host else ""
                return f"{cluster.name} · {cluster.location}{suffix}"

            selected_key = st.selectbox(
                "Cluster",
                list(cluster_by_key),
                format_func=cluster_label,
                disabled=is_working,
                key=f"manual_cluster:{project_id}",
            )
            selecting = action_kind == "select"
            select_clicked = st.button(
                "Selecting…" if selecting else "Select",
                type="primary",
                icon=(
                    ":material/progress_activity:"
                    if selecting
                    else ":material/check:"
                ),
                width="stretch",
                disabled=is_working,
                key=(
                    "select_kube_agents_cluster_busy"
                    if selecting
                    else "select_kube_agents_cluster"
                ),
            )
            if select_clicked:
                st.session_state[CONNECTION_ACTION_KEY] = {
                    "kind": "select",
                    "project_id": project_id,
                    "cluster_key": selected_key,
                }
                st.rerun()

        if is_connected:
            st.caption(
                f"Connected · {active_target.cluster_name} · {active_target.location}"
            )
            st.caption("Revalidated every 10 minutes while this portal is open.")
        else:
            st.caption("Not connected")
            if st.session_state.pop("connection_refresh_failed", False):
                st.warning(
                    "The saved connection failed revalidation. Reconnect to retry."
                )
            if report is not None:
                runtime_check = next(
                    (check for check in report.checks if check.key == "agent_runtime"),
                    None,
                )
                if runtime_check and runtime_check.status != connections.CheckStatus.PASS:
                    st.warning(runtime_check.summary)
        persistence_error = st.session_state.get("connection_persistence_error")
        if persistence_error:
            st.warning(persistence_error)
        action_error = st.session_state.pop("connection_action_error", None)
        if action_error:
            st.error(action_error)

        if action_kind == "connect":
            action_project = str(pending_action.get("project_id", ""))
            if not is_valid_project_id(action_project) or action_project != project_id:
                st.session_state.pop(CONNECTION_ACTION_KEY, None)
                st.session_state.connection_action_error = (
                    "The project selection changed. Retry Connect."
                )
                st.rerun()
            try:
                with st.spinner(f"Connecting to {action_project}…"):
                    report = connections.run_connection_checks(
                        action_project,
                        expected_target=None,
                        include_agent_runtime_probe=True,
                    )
                st.session_state[f"connection_report:{action_project}"] = report
                expected_target = None
                if len(report.kube_agents_hosts) == 1:
                    expected_target = _target_for_cluster(
                        action_project, report.kube_agents_hosts[0]
                    )
                    _set_scope(
                        action_project,
                        expected_target.cluster_name,
                        expected_target.location,
                    )
                elif report.clusters:
                    _set_scope(action_project)
                if (
                    connections.connection_is_ready(report)
                    and expected_target is not None
                ):
                    _mark_connected(expected_target, report)
                    _set_scope(
                        expected_target.project_id,
                        expected_target.cluster_name,
                        expected_target.location,
                    )
                    st.toast(f"Connected to {expected_target.cluster_name}.")
            except Exception as exc:
                st.session_state.connection_action_error = (
                    "Connection checks stopped unexpectedly "
                    f"({type(exc).__name__}). Retry Connect."
                )
            finally:
                st.session_state.pop(CONNECTION_ACTION_KEY, None)
            st.rerun()

        if action_kind == "select":
            action_project = str(pending_action.get("project_id", ""))
            cluster_key = str(pending_action.get("cluster_key", ""))
            selected_cluster = cluster_by_key.get(cluster_key)
            if action_project != project_id or selected_cluster is None:
                st.session_state.pop(CONNECTION_ACTION_KEY, None)
                st.session_state.connection_action_error = (
                    "The cluster selection changed. Retry Select."
                )
                st.rerun()
            expected_target = _target_for_cluster(action_project, selected_cluster)
            _set_scope(
                action_project,
                expected_target.cluster_name,
                expected_target.location,
            )
            try:
                with st.spinner(f"Verifying {selected_cluster.name}…"):
                    report = connections.run_connection_checks(
                        action_project,
                        expected_target=expected_target,
                        include_agent_runtime_probe=True,
                    )
                st.session_state[f"connection_report:{action_project}"] = report
                if connections.connection_is_ready(report):
                    _mark_connected(expected_target, report)
                    st.toast(f"Selected {expected_target.cluster_name}.")
            except Exception as exc:
                st.session_state.connection_action_error = (
                    "Cluster verification stopped unexpectedly "
                    f"({type(exc).__name__}). Retry Select."
                )
            finally:
                st.session_state.pop(CONNECTION_ACTION_KEY, None)
            st.rerun()
