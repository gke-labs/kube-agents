"""Raw LiteLLM evidence and provider configuration."""

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


st.title("LLM Gateway")
target = require_connection()
st.caption(
    f"{target.project_id} · {target.cluster_name} · {target.location} · "
    f"{target.namespace}"
)
client = portal_api(target)

try:
    with st.spinner("Checking LiteLLM resources and model connection…"):
        snapshot = client.inspect_llm_gateway()
except PortalApiError as exc:
    st.error(str(exc))
    if exc.guidance:
        st.caption(exc.guidance)
    st.stop()

evidence = list(snapshot.get("evidence", []))
scope_key = ":".join(
    (target.project_id, target.cluster_name, target.location, target.namespace)
)
configurations = st.session_state.setdefault("llm_configurations", {})
device_waits = st.session_state.setdefault("llm_device_waits", {})
changes = st.session_state.setdefault("llm_configuration_changes", {})
verification = snapshot.get("verification")
configuration = configurations.get(scope_key)
waiting_for_device = bool(device_waits.get(scope_key))
latest_change = changes.get(scope_key)


def connection_succeeded(item: object) -> bool:
    return isinstance(item, dict) and int(item.get("returncode", 1)) == 0


def concise_failure(item: object) -> str:
    if not isinstance(item, dict):
        return "The connection test returned no result."
    output = str(item.get("stderr") or item.get("stdout") or "")
    if output.strip():
        compact = " ".join(output.split())
        return compact if len(compact) <= 500 else compact[:497] + "…"
    source = str(item.get("source") or "Connection test")
    return f"{source} exited {int(item.get('returncode', 1))}."


if waiting_for_device:
    connection_status = "Waiting for sign-in"
elif connection_succeeded(verification):
    connection_status = "Connected"
else:
    connection_status = "Failed"

current = snapshot.get("configuration") or {}
configuration_writable = bool(snapshot.get("configurationWritable", True))
status_columns = st.columns((1, 1, 2))
status_columns[0].metric("Connection", connection_status)
status_columns[1].metric("Provider", current.get("providerLabel") or "Unknown")
status_columns[2].metric("Model", current.get("model") or "Unknown")
if waiting_for_device:
    st.info(
        "Waiting for sign-in — complete authorization below; the connection "
        "will then be tested automatically."
    )
elif connection_succeeded(verification):
    st.success(
        "Connected — the Platform Agent completed a model request through "
        "LiteLLM."
    )
else:
    st.error(f"Connection failed — {concise_failure(verification)}")

st.button(
    "Refresh status",
    type="primary",
    disabled=waiting_for_device,
)

@st.fragment(run_every="5s")
def monitor_device_sign_in() -> None:
    if not device_waits.get(scope_key):
        return
    st.subheader("Complete device sign-in")
    try:
        status = client.llm_gateway_device_status()
    except PortalApiError as exc:
        st.caption(f"Waiting for LiteLLM: {exc}")
        return
    status_evidence = list(status.get("evidence", []))
    polled_log = next(
        (
            item
            for item in status_evidence
            if item.get("source") == "ChatGPT device authorization log"
        ),
        None,
    )
    stored_log = next(
        (
            item
            for item in (
                configuration.get("evidence", [])
                if isinstance(configuration, dict)
                else []
            )
            if item.get("source") == "ChatGPT device authorization log"
        ),
        None,
    )
    polled_output = (
        str(polled_log.get("stdout") or polled_log.get("stderr") or "")
        if isinstance(polled_log, dict)
        else ""
    )
    device_log = (
        polled_log
        if polled_output and polled_log.get("returncode") == 0
        else stored_log
    )
    if device_log:
        output = str(device_log.get("stdout") or device_log.get("stderr") or "")
        if output:
            st.code(output, language="text", wrap_lines=True)
        if isinstance(configuration, dict) and device_log is polled_log:
            retained = [
                item
                for item in configuration.get("evidence", [])
                if item.get("source") != "ChatGPT device authorization log"
            ]
            configuration["evidence"] = [*retained, device_log]
    if not status.get("ready"):
        st.caption("Waiting for device authorization…")
        return
    changes[scope_key]["state"] = "testing"
    device_waits.pop(scope_key, None)
    st.rerun(scope="app")


st.divider()
st.subheader("Configuration")
if not configuration_writable:
    st.info(
        str(snapshot.get("configurationGuidance"))
        or "This installation manages LiteLLM outside the portal."
    )
catalog = snapshot["catalog"]
providers = list(catalog["providers"])
provider_by_id = {item["id"]: item for item in providers}
provider_ids = list(provider_by_id)
current_provider = str(current.get("providerId") or catalog["defaultProvider"])
if current_provider not in provider_by_id:
    current_provider = str(catalog["defaultProvider"])

selected_provider_id = st.selectbox(
    "Provider",
    provider_ids,
    index=provider_ids.index(current_provider),
    format_func=lambda item: provider_by_id[item]["label"],
    key=f"llm_provider_{scope_key}",
    disabled=not configuration_writable,
)
provider = provider_by_id[selected_provider_id]
auth = provider["authentication"]
credential = ""
settings: dict[str, str] = {}
with st.form(
    f"llm_configuration_{selected_provider_id}",
    clear_on_submit=True,
):
    model = st.text_input(
        "Model",
        value=(
            str(current["model"])
            if selected_provider_id == current.get("providerId")
            and current.get("model")
            else str(provider["defaultModel"])
        ),
        key=f"llm_model_{scope_key}_{selected_provider_id}",
        help=(
            "Any model identifier accepted by the selected LiteLLM provider "
            "is supported."
        ),
        disabled=not configuration_writable,
    )
    if auth["type"] == "api_key":
        credential = st.text_input(
            "API key",
            type="password",
            placeholder="Leave blank to keep the existing key",
            help=(
                f"Stored as {auth['environmentVariable']}. Write-only: the portal "
                "never reads the existing value or persists this input."
            ),
            disabled=not configuration_writable,
        )
    elif auth["type"] == "workload_identity":
        current_settings = (
            current.get("settings", {})
            if selected_provider_id == current.get("providerId")
            else {}
        )
        for setting in provider.get("settings", []):
            # The location prefill is "global", not the cluster's region: a
            # model is only callable from a location that serves it, and the
            # cluster's often is not one. This field is submitted verbatim, so
            # a regional prefill would override the same default the gateway
            # applies -- see DEFAULT_VERTEX_LOCATION in
            # k8s-operator/scripts/installer_common.sh.
            default = current_settings.get(setting["id"]) or (
                target.project_id if setting["id"] == "project_id" else "global"
            )
            settings[setting["id"]] = st.text_input(
                setting["label"],
                value=default,
                disabled=not configuration_writable,
            )
        st.caption("Vertex AI uses Workload Identity; no API key is stored.")
    elif auth["type"] == "device_oauth":
        st.info(
            "Applying this configuration starts ChatGPT device authorization. "
            "The authorization URL and code will appear below exactly as "
            "LiteLLM logs them."
        )

    button_label = (
        "Start device sign-in"
        if auth["type"] == "device_oauth"
        else "Apply configuration"
    )
    configure = st.form_submit_button(
        button_label,
        width="stretch",
        disabled=not configuration_writable,
    )

if configure:
    device_waits.pop(scope_key, None)
    provider_label = str(provider["label"])
    try:
        with st.spinner("Applying configuration…"):
            result = client.configure_llm_gateway(
                provider_id=selected_provider_id,
                model=model,
                credential=credential,
                settings=settings,
            )
        configurations[scope_key] = result
        result_evidence = list(result.get("evidence", []))
        if not result.get("configurationApplied"):
            failed_item = next(
                (
                    item
                    for item in result_evidence
                    if int(item.get("returncode", 1)) != 0
                ),
                None,
            )
            failure = concise_failure(failed_item)
            changes[scope_key] = {
                "state": "apply_failed",
                "provider": provider_label,
                "model": model,
                "message": failure,
            }
            st.rerun()
        if auth["type"] == "device_oauth":
            device_waits[scope_key] = True
            changes[scope_key] = {
                "state": "waiting",
                "provider": provider_label,
                "model": model,
                "message": "",
            }
        elif not result.get("readyForTest"):
            failed_item = next(
                (
                    item
                    for item in result_evidence
                    if int(item.get("returncode", 1)) != 0
                ),
                None,
            )
            failure = concise_failure(failed_item)
            changes[scope_key] = {
                "state": "rollout_failed",
                "provider": provider_label,
                "model": model,
                "message": failure,
            }
        else:
            changes[scope_key] = {
                "state": "testing",
                "provider": provider_label,
                "model": model,
                "message": "",
            }
        st.rerun()
    except PortalApiError as exc:
        changes[scope_key] = {
            "state": "apply_failed",
            "provider": provider_label,
            "model": model,
            "message": str(exc),
            "guidance": exc.guidance,
        }
        st.rerun()

latest_change = changes.get(scope_key)
if isinstance(latest_change, dict):
    if latest_change.get("state") == "testing":
        latest_change["state"] = (
            "connected" if connection_succeeded(verification) else "connection_failed"
        )
        latest_change["message"] = concise_failure(verification)
    change_target = " · ".join(
        value
        for value in (
            str(latest_change.get("provider") or ""),
            str(latest_change.get("model") or ""),
        )
        if value
    )
    state = latest_change.get("state")
    if state == "connected":
        st.success(f"Configuration applied · Connected · {change_target}")
    elif state == "waiting":
        st.info(f"Configuration applied · Waiting for device sign-in · {change_target}")
    elif state == "connection_failed":
        st.error(
            f"Configuration applied · Connection failed · {change_target} — "
            f"{latest_change.get('message') or 'No result was returned.'}"
        )
    elif state == "rollout_failed":
        st.error(
            f"Configuration applied · Rollout failed · {change_target} — "
            f"{latest_change.get('message') or 'No result was returned.'}"
        )
    else:
        st.error(
            f"Configuration failed · {change_target} — "
            f"{latest_change.get('message') or 'No result was returned.'}"
        )
        if latest_change.get("guidance"):
            st.caption(str(latest_change["guidance"]))

monitor_device_sign_in()

st.divider()
st.subheader("Raw Logs")
st.caption(
    "Exact Kubernetes command output, container logs, and connection responses. "
    "The portal does not replace upstream messages."
)
if st.toggle("Show raw logs", value=False):
    raw_items = list(evidence)
    if isinstance(verification, dict):
        raw_items.append(verification)
    if isinstance(configuration, dict):
        raw_items.extend(configuration.get("evidence", []))
    for item in raw_items:
        render_command_evidence(item, expanded=False)
