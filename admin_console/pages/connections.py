"""Connection checklist and diagnostics results."""

from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parents[2]
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

import streamlit as st

from admin_console.connections import CheckStatus
from admin_console.connection_sidebar import render_connection_controls

st.title("Connection")
render_connection_controls()
st.divider()
project_id = str(st.session_state.get("selected_project", "")).strip()
connected_target = st.session_state.get("connected_target")

if not project_id:
    st.info("Choose a project above to connect.")
    st.stop()

report = st.session_state.get(f"connection_report:{project_id}")
if report is None:
    st.info("Connect above to run the verification checklist.")
    st.stop()

if connected_target and connected_target.project_id == project_id:
    st.success(
        f"Connected to {connected_target.cluster_name} · "
        f"{connected_target.location}."
    )
elif report.clusters and len(report.kube_agents_hosts) != 1:
    if report.kube_agents_hosts:
        reason = (
            f"{len(report.kube_agents_hosts)} clusters are labeled "
            "kube-agents-host=true"
        )
    else:
        reason = "no cluster is labeled kube-agents-host=true"
    st.error(
        f"Automatic cluster detection failed because {reason}. "
        "Select the kube-agents host above."
    )
elif report.failed:
    st.error("Connection was not established. Resolve the failed checks and retry.")
else:
    st.info("Not connected. Review the non-passing checks and retry above.")

st.caption(f"Last checked {report.checked_at:%Y-%m-%d %H:%M:%S UTC}")
metrics = st.columns(3)
metrics[0].metric("Passed", report.passed)
metrics[1].metric("Warnings", report.warnings)
metrics[2].metric("Failed", report.failed)

st.subheader("Checks")
icons = {
    CheckStatus.PASS: "✅",
    CheckStatus.WARNING: "⚠️",
    CheckStatus.FAIL: "❌",
    CheckStatus.SKIPPED: "⏭️",
}
for check in report.checks:
    with st.container(border=True):
        st.markdown(f"#### {icons[check.status]} {check.label}")
        st.write(check.summary)
        if check.guidance:
            st.caption("GUIDANCE")
            st.markdown(check.guidance)

st.subheader("Google Cloud")
encoded_project = urllib.parse.quote(project_id, safe="")
link_columns = st.columns(3)
link_columns[0].link_button(
    "Logs Explorer",
    f"https://console.cloud.google.com/logs/query?project={encoded_project}",
    width="stretch",
)
link_columns[1].link_button(
    "Trace Explorer",
    f"https://console.cloud.google.com/traces/list?project={encoded_project}",
    width="stretch",
)
link_columns[2].link_button(
    "GKE Workloads",
    "https://console.cloud.google.com/kubernetes/workload/overview"
    f"?project={encoded_project}",
    width="stretch",
)
