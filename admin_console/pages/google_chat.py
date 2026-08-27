"""Google Chat integration: verdict, fix, milestones — all served by the API."""

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
    with st.spinner("Checking the Google Chat integration…"):
        snapshot = client.inspect_google_chat_integration()
except PortalApiError as exc:
    st.error(str(exc))
    if exc.guidance:
        st.caption(exc.guidance)
    st.stop()

status = str(snapshot.get("status") or "Verification incomplete")
message = str(snapshot.get("message") or "No integration result was returned.")
severity = str(snapshot.get("severity") or "warning")
configuration = snapshot.get("configuration") or {}
checks = [item for item in snapshot.get("checks") or [] if isinstance(item, dict)]
evidence = list(snapshot.get("evidence") or [])

# The service decides what is wrong and what to do; this page only renders.
banner = {
    "success": st.success,
    "info": st.info,
    "warning": st.warning,
    "error": st.error,
}.get(severity, st.warning)
passed_checks = [item for item in checks if item.get("status") == "passed"]
open_checks = [item for item in checks if item.get("status") != "passed"]

# With open items, their first entry carries the diagnosis; keep the banner
# to the verdict so nothing is said twice.
banner(f"**{status}**" if open_checks else f"**{status}** — {message}")

next_steps = [
    item for item in snapshot.get("nextSteps") or [] if isinstance(item, dict)
]
if next_steps:
    st.subheader("Next step")
    for index, action in enumerate(next_steps):
        text = str(action.get("text") or "")
        if text and index == 0:
            st.warning(text)
        elif text:
            st.markdown(text)
        copy_value = str(action.get("copy") or "")
        if copy_value:
            st.code(copy_value, language="text")

if passed_checks:
    with st.expander(f"✅ {len(passed_checks)} steps completed"):
        st.markdown(
            "  \n".join(
                f"✅ {item.get('label') or item.get('id')}"
                for item in passed_checks
            )
        )

icons = {"failed": "❌", "unknown": "❓", "not_observed": "◻️"}
for index, item in enumerate(open_checks):
    icon = icons.get(str(item.get("status")), "❓")
    st.markdown(f"{icon} **{item.get('label') or item.get('id')}**")
    if index == 0:
        detail = str(item.get("detail") or "")
        if detail:
            st.markdown(detail)
        actions = list(item.get("actions") or [])
        if actions:
            st.markdown("**What to do:**")
            for action in actions:
                text = str(action.get("text") or "")
                if text:
                    st.markdown(text)
                copy_value = str(action.get("copy") or "")
                if copy_value:
                    st.code(copy_value, language="text")

if checks:
    with st.expander("Check details"):
        st.dataframe(
            [
                {
                    "Check": str(item.get("label") or item.get("id") or "Check"),
                    "Result": str(item.get("status") or "unknown"),
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

if configuration:
    with st.expander("Integration details"):
        left, right = st.columns(2)
        left.caption("Topic")
        left.code(
            str(configuration.get("topicPath") or "Not configured"),
            language="text",
        )
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
    with st.expander("Raw evidence"):
        st.caption(
            "Commands and responses used for the checks. Credential-shaped "
            "Kubernetes values are redacted."
        )
        if st.toggle("Show raw evidence", value=False):
            for item in evidence:
                render_command_evidence(item, expanded=True)


raw_evidence_section()
