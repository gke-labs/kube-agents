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
from admin_console.connection_session import recover_app_shell
from admin_console.connection_sidebar import (
    connection_controller,
    render_connection_controls,
)

recover_app_shell()
st.title("Connection")
render_connection_controls()
controller = connection_controller()
if not controller.project_id:
    st.stop()

report = controller.cluster.report
if report is None or controller.connected_target is None:
    st.stop()

st.divider()
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
encoded_project = urllib.parse.quote(controller.project_id, safe="")
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
