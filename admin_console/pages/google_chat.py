"""Live Google Chat integration status and configuration handoff."""

from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import streamlit as st

from admin_console.clients.portal_api import PortalApiClient, PortalApiError
from admin_console.connection_gate import require_connection
from admin_console.connection_session import recover_app_shell
from admin_console.project_config import DeploymentTarget
from admin_console.ui import render_command_evidence

recover_app_shell()


@st.cache_resource
def portal_api(target: DeploymentTarget) -> PortalApiClient:
    return PortalApiClient(target)


st.title("Google Chat")
target = require_connection()
st.caption(
    f"{target.project_id} · {target.cluster_name} · {target.location} · "
    f"{target.namespace}"
)

st.button("Refresh status", type="primary")
client = portal_api(target)
try:
    with st.spinner("Checking the PlatformAgent and Google Chat backend…"):
        snapshot = client.inspect_google_chat_integration()
except PortalApiError as exc:
    st.error(str(exc))
    if exc.guidance:
        st.caption(exc.guidance)
    st.stop()

status = str(snapshot.get("status") or "Verification incomplete")
message = str(snapshot.get("message") or "No integration result was returned.")
configuration = snapshot.get("configuration") or {}
activity = snapshot.get("activity") or {}
checks = list(snapshot.get("checks") or [])
evidence = list(snapshot.get("evidence") or [])

session_count = activity.get("sessionCount")
activity_truncated = bool(activity.get("truncated"))
status_columns = st.columns(3)
status_columns[0].metric("Backend", status)
status_columns[1].metric(
    "Delivery evidence",
    (
        "Unknown"
        if session_count is None
        else "None in sample"
        if activity_truncated and session_count == 0
        else f"{session_count}+ sessions"
        if activity_truncated
        else "None observed"
        if session_count == 0
        else f"{session_count} sessions"
    ),
)
status_columns[2].metric(
    "PlatformAgent", configuration.get("platformAgent") or "Unavailable"
)

if status == "Backend ready":
    st.success(message)
elif status == "Disabled":
    st.info(message)
elif status == "Needs attention":
    st.error(message)
else:
    st.warning(message)

if session_count == 0 and activity_truncated:
    st.warning(
        "No Google Chat event appears in the newest 500 sessions. The result "
        "is partial, so older events in the 30-day window may exist."
    )
elif session_count == 0 and status == "Backend ready":
    st.info(
        "No Google Chat event was observed in the last 30 days. Confirm the "
        "Chat app topic below, send a message, then refresh status."
    )
elif isinstance(session_count, int) and session_count > 0:
    latest_at = str(activity.get("latestAt") or "")
    qualifier = "at least " if activity_truncated else ""
    st.caption(
        f"Observed {qualifier}{session_count} Google Chat session"
        f"{'s' if session_count != 1 else ''}; latest activity {latest_at}."
    )

topic_path = str(configuration.get("topicPath") or "")
if configuration.get("enabled") and topic_path:
    st.subheader("Google Chat setup")
    st.write(
        "Set the Chat app connection to **Cloud Pub/Sub** with this topic:"
    )
    st.code(topic_path, language="text")
    configuration_url = str(configuration.get("configurationUrl") or "")
    if configuration_url:
        st.link_button(
            "Open Google Chat configuration",
            configuration_url,
            icon=":material/open_in_new:",
        )
    st.caption(
        "Backend ready does not prove this separate Google Chat setting was saved."
    )

if checks:
    status_label = {
        "passed": "Passed",
        "failed": "Failed",
        "unknown": "Could not verify",
        "not_observed": "Not observed",
        "not_applicable": "Not applicable",
    }
    passed = sum(item.get("status") == "passed" for item in checks)
    unresolved = sum(
        item.get("status") in {"failed", "unknown"} for item in checks
    )
    check_summary = f"Live checks · {passed} passed"
    if unresolved:
        check_summary += f" · {unresolved} need attention"
    with st.expander(check_summary, expanded=unresolved > 0):
        st.dataframe(
            [
                {
                    "Check": str(item.get("label") or item.get("id") or "Check"),
                    "Result": status_label.get(
                        str(item.get("status") or "unknown"), "Could not verify"
                    ),
                    "Detail": str(item.get("detail") or ""),
                }
                for item in checks
            ],
            hide_index=True,
            width="stretch",
            column_config={
                "Check": st.column_config.TextColumn(width="medium"),
                "Result": st.column_config.TextColumn(width="small"),
                "Detail": st.column_config.TextColumn(width="large"),
            },
        )
else:
    st.caption("No checks were returned.")

if configuration:
    with st.expander("Integration details"):
        left, right = st.columns(2)
        left.caption("Subscription")
        left.code(
            str(configuration.get("subscriptionPath") or "Not configured"),
            language="text",
        )
        left.caption("Hermes mode")
        left.write(str(configuration.get("mode") or "default"))
        left.caption("Allowed users")
        allowed_users = list(configuration.get("allowedUsers") or [])
        left.write(
            "All users"
            if configuration.get("allowsAllUsers")
            else ", ".join(str(value) for value in allowed_users)
        )
        right.caption("Platform Agent service account")
        right.code(
            str(configuration.get("agentServiceAccount") or "Not declared"),
            language="text",
        )
        right.caption("Google Chat service account")
        right.code(
            str(configuration.get("workspaceServiceAccount") or "Unavailable"),
            language="text",
        )
        right.caption("Home channel")
        right.write(str(configuration.get("homeChannel") or "Not configured"))

@st.fragment
def raw_evidence_section() -> None:
    st.divider()
    st.subheader("Raw evidence")
    st.caption(
        "Commands and responses used for the backend checks. Credential-shaped "
        "Kubernetes values are redacted."
    )
    if st.toggle("Show raw evidence", value=False):
        for item in evidence:
            render_command_evidence(item, expanded=True)


raw_evidence_section()
