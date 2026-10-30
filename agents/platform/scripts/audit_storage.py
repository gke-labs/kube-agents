#!/usr/bin/env python3
"""audit_storage.py - High-performance Kubernetes storage auditor.

Single-pass concurrent collector for PersistentVolumeClaims (PVCs),
PersistentVolumes (PVs), StorageClasses, VolumeAttachments, and projected Pod
PVC references. Discovers and audits cluster storage state in one pass.
Part of kube-agents #3285.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess
import sys
import unicodedata

# How much of a failed query's error text or resource-sourced cell text is shown on one line.
UNREAD_REASON_MAX_CHARS = 200
MAX_CELL_CHARS = 200
REDACTED_STOP_MARKER = "[REDACTED_STOP_MARKER]"
ANSI_ESCAPE_RE = re.compile(r"(?:\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\x9B[0-?]*[ -/]*[@-~])")
STOP_MARKER_RE = re.compile(r"clean[\W_]*audit[\W_]*stop", re.IGNORECASE)
# What a summary line prints in place of a count for a kind whose listing failed.
UNREAD_COUNT_TEXT = "unread (listing failed; see the verdict below)"
# A StorageClass binding mode that leaves a PVC Pending, by design, until a pod uses it.
WAIT_FOR_FIRST_CONSUMER = "WaitForFirstConsumer"
# Built-in StorageClass provisioner string for static PV binding (e.g. Local SSD); the scheduler
# binds directly to an existing PV without writing `volume.kubernetes.io/selected-node` on the PVC.
NO_PROVISIONER = "kubernetes.io/no-provisioner"
UNKNOWN_PROVISIONER = "unknown"
# Annotation the scheduler sets on a dynamic WaitForFirstConsumer PVC once a pod using it has picked a node.
SELECTED_NODE_ANNOTATION = "volume.kubernetes.io/selected-node"
# Maximum age of an error-free unattached or deleting VolumeAttachment before it counts as stalled
# rather than in-flight (CSI external-attacher's default RPC `--timeout` is 15 s; 2 minutes
# covers slow cloud disk attach/detach calls while surfacing a down or wedged external-attacher).
ATTACHMENT_IN_PROGRESS_GRACE = timedelta(minutes=2)
# Per-call kubectl deadline: 60 s broker admission wait + 60 s broker kubectl run cap + 5 s margin,
# matching cluster_preflight.sh so queued calls on the shared 8-slot sandbox pool do not time out locally.
KUBECTL_TIMEOUT_SECONDS = 125
# The sandbox's kubectl wrapper that forwards calls through the credential proxy.
CREDENTIAL_PROXY_BIN = "/opt/credential-proxy/bin"
# Marker emitted on stderr when the credential proxy cuts output at CREDENTIAL_PROXY_MAX_OUTPUT_BYTES.
PROXY_TRUNCATED_MARKER = "credential proxy output truncated"
# PVC spec keys and StorageClass display labels.
SPEC_KEY = "spec"
STORAGE_CLASS_NAME_KEY = "storageClassName"
DEFAULT_STORAGE_CLASS_UNREAD_DISPLAY = "(default: unread)"
DEFAULT_STORAGE_CLASS_NONE_DISPLAY = "(default: none)"
STATIC_STORAGE_CLASS_DISPLAY = '"" (static)'
NO_DEFAULT_STORAGE_CLASS = "None"
STORAGE_CLASS_FIELD = "storage_class"
# Projected pod read pulling only
# namespace, name, status.phase, and PVC claimNames so large clusters stay well under
# CREDENTIAL_PROXY_MAX_OUTPUT_BYTES; the leading "PODS\n" literal distinguishes 0 pods from an empty reply.
POD_CLAIMS_HEADER = "PODS"
POD_CLAIMS_FIELD_COUNT = 4
POD_CLAIMS_JSONPATH = (
    '{"PODS\\n"}'
    '{range .items[*]}{.metadata.namespace}{"\\t"}{.metadata.name}{"\\t"}'
    '{.status.phase}{"\\t"}{.spec.volumes[*].persistentVolumeClaim.claimName}{"\\n"}{end}'
)
TERMINAL_POD_PHASES = frozenset({"Succeeded", "Failed"})
MAX_DISPLAYED_CLAIMING_PODS = 3
CLAIMING_POD_NAME_MAX_CHARS = 60
STORAGE_SCOPE_NOTE = (
    "PVC/PV phases, WFFC consumer pods, PV reclaim state and VolumeAttachment attach/detach status checked; "
    "PVC resize conditions and PVC/PV in-progress deletions not read"
)


def _is_printable_cell_char(ch: str) -> bool:
    code = ord(ch)
    if ch.isspace():
        return True
    if code < 32 or 127 <= code <= 159 or code >= 0xE0000:
        return False
    return not (
        0x200B <= code <= 0x200F
        or 0x202A <= code <= 0x202E
        or 0x2060 <= code <= 0x206F
        or code in (0xFEFF, 0x00AD, 0x034F, 0x061C, 0x180E)
    )


def sanitize_cell(value: object, limit: int = MAX_CELL_CHARS) -> str:
    """Fold untrusted cluster text to one bounded line and neutralize CLEAN_AUDIT_STOP markers."""
    raw = unicodedata.normalize("NFKC", "" if value is None else str(value))
    stripped = "".join(ch for ch in ANSI_ESCAPE_RE.sub("", raw) if _is_printable_cell_char(ch))
    flat = " ".join(stripped.split())
    safe = STOP_MARKER_RE.sub(REDACTED_STOP_MARKER, flat)
    return safe[:limit] if limit else safe


def has_dynamic_provisioner(provisioner: str | None) -> bool:
    """True when a StorageClass names a dynamic provisioner rather than `kubernetes.io/no-provisioner`."""
    return bool(provisioner) and provisioner not in (UNKNOWN_PROVISIONER, NO_PROVISIONER)


def _now_utc() -> datetime:
    """Return the current UTC timestamp (isolated so tests can simulate per-listing broker delays)."""
    return datetime.now(timezone.utc)


def format_age(creation_ts: object, now: datetime) -> str:
    """Compact human-readable age (`45s`, `12m`, `3h`, `4d`), or `N/A` when `creationTimestamp` is absent."""
    if not isinstance(creation_ts, str) or not creation_ts:
        return "N/A"
    try:
        created = datetime.fromisoformat(creation_ts.replace("Z", "+00:00"))
        if created.tzinfo is None or created.tzinfo.utcoffset(created) is None:
            created = created.replace(tzinfo=timezone.utc)
        seconds = max(0, int((now - created).total_seconds()))
    except (ValueError, TypeError):
        return "N/A"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def is_within_grace(creation_ts: object, now: datetime, grace: timedelta) -> bool:
    """True when `creation_ts` is a valid ISO-8601 timestamp newer than `now - grace`."""
    if not isinstance(creation_ts, str) or not creation_ts:
        return False
    try:
        created = datetime.fromisoformat(creation_ts.replace("Z", "+00:00"))
        if created.tzinfo is None or created.tzinfo.utcoffset(created) is None:
            created = created.replace(tzinfo=timezone.utc)
        return (now - created) < grace
    except (ValueError, TypeError):
        return False


def run_kubectl_json(args: list[str], env: dict[str, str]) -> tuple[int, dict]:
    cmd = ["kubectl"] + args + ["-o", "json"]
    try:
        res = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=KUBECTL_TIMEOUT_SECONDS,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            stderr_msg = res.stderr.strip()
            if PROXY_TRUNCATED_MARKER in stderr_msg.lower():
                return 1, {"error": stderr_msg}
            try:
                return 0, json.loads(res.stdout)
            except Exception as exc:
                err_text = f"{stderr_msg} ({exc})" if stderr_msg else str(exc)
                return 1, {"error": err_text}
        return res.returncode or 1, {"error": res.stderr.strip() or f"kubectl exit {res.returncode} with no output"}
    except Exception as e:
        return 1, {"error": str(e)}


def parse_pod_pvc_claims_output(stdout: str) -> tuple[int, dict]:
    """Parse `POD_CLAIMS_JSONPATH` stdout into a PodList-shaped `{"items": [...]}` dict."""
    lines = stdout.split("\n")
    if not lines or lines[0] != POD_CLAIMS_HEADER:
        return 1, {"error": "unexpected kubectl pods jsonpath output"}
    items: list[dict] = []
    for line in lines[1:]:
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != POD_CLAIMS_FIELD_COUNT:
            return 1, {"error": f"malformed pod PVC claims row ({len(fields)} fields)"}
        namespace, name, phase, raw_claims = fields
        claims = [c for c in raw_claims.split() if c]
        items.append(
            {
                "metadata": {"namespace": namespace or "default", "name": name},
                "status": {"phase": phase},
                "claims": claims,
            }
        )
    return 0, {"items": items}


def run_kubectl_pods(env: dict[str, str]) -> tuple[int, dict]:
    """Fetch projected Pod phases and PVC volume references without pulling full Pod specs."""
    cmd = ["kubectl", "get", "pods", "-A", "-o", f"jsonpath={POD_CLAIMS_JSONPATH}"]
    try:
        res = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=KUBECTL_TIMEOUT_SECONDS,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            stderr_msg = res.stderr.strip()
            if PROXY_TRUNCATED_MARKER in stderr_msg.lower():
                return 1, {"error": stderr_msg}
            code, data = parse_pod_pvc_claims_output(res.stdout)
            if code != 0 and stderr_msg:
                return code, {"error": f"{stderr_msg} ({data.get('error', '')})"}
            return code, data
        return res.returncode or 1, {"error": res.stderr.strip() or f"kubectl exit {res.returncode} with no output"}
    except Exception as e:
        return 1, {"error": str(e)}


def extract_active_pvc_consumers(
    pod_items: object,
) -> tuple[dict[tuple[str, str], list[str]], set[tuple[str, str]]]:
    """Map `(namespace, claim_name)` to sorted non-terminal claiming Pod names, plus active `(ns, pod)` keys."""
    if not isinstance(pod_items, list):
        return {}, set()
    claims_to_pods: dict[tuple[str, str], set[str]] = {}
    active_pods: set[tuple[str, str]] = set()
    for item in pod_items:
        if not isinstance(item, dict):
            continue
        meta = item.get("metadata") or {}
        pod_ns = meta.get("namespace") or "default"
        pod_name = meta.get("name") or ""
        if not isinstance(pod_ns, str) or not isinstance(pod_name, str) or not pod_name:
            continue
        phase = ((item.get("status") or {}).get("phase") or "").strip()
        if phase in TERMINAL_POD_PHASES:
            continue
        active_pods.add((pod_ns, pod_name))
        raw_claims = item.get("claims")
        claim_names: list[str] = []
        if isinstance(raw_claims, list):
            claim_names.extend(c for c in raw_claims if isinstance(c, str) and c)
        for vol in ((item.get(SPEC_KEY) or {}).get("volumes") or []):
            if isinstance(vol, dict):
                pvc_src = vol.get("persistentVolumeClaim") or {}
                if isinstance(pvc_src, dict):
                    claim = (pvc_src.get("claimName") or "").strip()
                    if claim:
                        claim_names.append(claim)
        for claim in claim_names:
            claims_to_pods.setdefault((pod_ns, claim), set()).add(pod_name)
    return {key: sorted(names) for key, names in claims_to_pods.items()}, active_pods


def _format_pod_names(pod_names: list[str]) -> tuple[str, str]:
    safe = [sanitize_cell(p, CLAIMING_POD_NAME_MAX_CHARS) for p in pod_names[:MAX_DISPLAYED_CLAIMING_PODS]]
    extra = len(pod_names) - len(safe)
    label = ", ".join(safe) + (f" (+{extra} more)" if extra > 0 else "")
    noun = "pod" if len(pod_names) == 1 else "pods"
    return noun, label


def format_claiming_pods(claiming_pods: list[str]) -> str:
    noun, label = _format_pod_names(claiming_pods)
    return f"claimed by {noun} {label}; no node selected"


def audit_storage(kubeconfig: str | None = None) -> int:
    env = os.environ.copy()
    if CREDENTIAL_PROXY_BIN not in env.get("PATH", ""):
        env["PATH"] = f"{CREDENTIAL_PROXY_BIN}:" + env.get("PATH", "")
    if kubeconfig:
        env["KUBECONFIG"] = kubeconfig

    tasks = {
        "pvcs": ["get", "pvc", "-A"],
        "pvs": ["get", "pv"],
        "scs": ["get", "sc"],
        "attachments": ["get", "volumeattachments"],
    }

    results = {}
    fetched_at: dict[str, datetime] = {}
    # Kinds whose listing failed (timeout, Forbidden, cut or unparseable reply): never read as empty.
    unread: dict[str, str] = {}

    def _timed_call(fn, *call_args) -> tuple[int, dict, datetime]:
        code, data = fn(*call_args)
        return code, data, _now_utc()

    with ThreadPoolExecutor(max_workers=len(tasks) + 1) as executor:
        future_to_key = {
            executor.submit(_timed_call, run_kubectl_json, args, env): key
            for key, args in tasks.items()
        }
        future_to_key[executor.submit(_timed_call, run_kubectl_pods, env)] = "pods"
        for future in future_to_key:
            key = future_to_key[future]
            try:
                code, data, completed_at = future.result()
            except Exception as e:
                code, data, completed_at = 1, {"error": str(e)}, _now_utc()
            fetched_at[key] = completed_at
            results[key] = data if code == 0 else {}
            if code != 0:
                unread[key] = sanitize_cell(data.get("error", ""), UNREAD_REASON_MAX_CHARS)

    pvc_items = results.get("pvcs", {}).get("items", [])
    pv_items = results.get("pvs", {}).get("items", [])
    sc_items = results.get("scs", {}).get("items", [])
    va_items = results.get("attachments", {}).get("items", [])
    active_pvc_consumers, active_pod_keys = extract_active_pvc_consumers(results.get("pods", {}).get("items", []))

    # 1. Parse StorageClasses
    sc_list = []
    raw_default_sc: str | None = None
    for item in sc_items:
        raw_name = item.get("metadata", {}).get("name", "")
        raw_provisioner = (item.get("provisioner") or "").strip()
        provisioner = raw_provisioner or UNKNOWN_PROVISIONER
        raw_reclaim_policy = item.get("reclaimPolicy", "Delete")
        raw_binding_mode = item.get("volumeBindingMode", "Immediate")
        annotations = item.get("metadata", {}).get("annotations", {}) or {}
        is_default = (
            annotations.get("storageclass.kubernetes.io/is-default-class") == "true"
            or annotations.get("storageclass.beta.kubernetes.io/is-default-class") == "true"
        )
        if is_default:
            raw_default_sc = raw_name
        sc_list.append({
            "raw_name": raw_name,
            "name": sanitize_cell(raw_name),
            "raw_provisioner": provisioner,
            "provisioner": sanitize_cell(provisioner),
            "reclaim_policy": sanitize_cell(raw_reclaim_policy),
            "raw_binding_mode": raw_binding_mode,
            "binding_mode": sanitize_cell(raw_binding_mode),
            "is_default": is_default,
        })

    default_sc = NO_DEFAULT_STORAGE_CLASS if raw_default_sc is None else sanitize_cell(raw_default_sc)
    binding_modes = {sc["raw_name"]: sc["raw_binding_mode"] for sc in sc_list}
    sc_provisioners = {sc["raw_name"]: sc["raw_provisioner"] for sc in sc_list}

    # 2. Parse PVCs (age each kind against the UTC instant its own listing returned so a slow
    # sibling listing queued on the 8-slot broker pool does not skew already-fetched timestamps).
    pvc_now = fetched_at.get("pvcs") or _now_utc()
    pvc_list = []
    pending_pvcs = []
    # Unbound Pending PVCs on a dynamic WaitForFirstConsumer class that no active pod has claimed yet: expected, not degraded.
    waiting_pvcs = []
    unverified_wffc_pvcs = []
    for item in pvc_items:
        meta = item.get("metadata") or {}
        spec = item.get(SPEC_KEY) or {}
        raw_ns = meta.get("namespace") or "default"
        raw_name = meta.get("name") or ""
        ns = sanitize_cell(raw_ns)
        name = sanitize_cell(raw_name)
        age = format_age(meta.get("creationTimestamp"), pvc_now)
        raw_phase = (item.get("status") or {}).get("phase", "Unknown")
        phase = sanitize_cell(raw_phase)
        vol_name = (spec.get("volumeName") or "").strip()
        raw_sc_name = spec.get(STORAGE_CLASS_NAME_KEY)
        if raw_sc_name is None:
            if "scs" in unread:
                sc_display = DEFAULT_STORAGE_CLASS_UNREAD_DISPLAY
            elif raw_default_sc is None:
                sc_display = DEFAULT_STORAGE_CLASS_NONE_DISPLAY
            else:
                sc_display = f"(default: {default_sc})"
        else:
            sc_display = sanitize_cell(raw_sc_name) if raw_sc_name else STATIC_STORAGE_CLASS_DISPLAY
        req_storage = sanitize_cell(
            ((spec.get("resources") or {}).get("requests") or {}).get("storage", "N/A")
        )
        pvc_info = {
            "namespace": ns,
            "name": name,
            "phase": phase,
            "is_bound": raw_phase == "Bound",
            "age": age,
            "volume": sanitize_cell(vol_name) if vol_name else "(unbound)",
            STORAGE_CLASS_FIELD: sc_display,
            "capacity": req_storage,
        }
        pvc_list.append(pvc_info)
        annotations = meta.get("annotations") or {}
        effective_sc = (raw_default_sc or "") if raw_sc_name is None else raw_sc_name
        if (
            raw_phase == "Pending"
            and not vol_name
            and binding_modes.get(effective_sc) == WAIT_FOR_FIRST_CONSUMER
            and has_dynamic_provisioner(sc_provisioners.get(effective_sc))
            and SELECTED_NODE_ANNOTATION not in annotations
        ):
            owner_pods: list[str] = []
            for owner_ref in meta.get("ownerReferences") or []:
                if isinstance(owner_ref, dict) and owner_ref.get("kind") == "Pod":
                    owner_pod = (owner_ref.get("name") or "").strip()
                    if owner_pod and owner_pod not in owner_pods:
                        owner_pods.append(owner_pod)
            if "pods" in unread:
                if owner_pods:
                    noun, shown_owners = _format_pod_names(sorted(owner_pods))
                    pvc_info["phase"] = sanitize_cell(
                        f"Pending (consumer pods unread; owner {noun} {shown_owners} unverified)"
                    )
                else:
                    pvc_info["phase"] = "Pending (consumer pods unread)"
                unverified_wffc_pvcs.append(pvc_info)
            else:
                claiming_pods = list(active_pvc_consumers.get((raw_ns, raw_name), ()))
                for owner_pod in owner_pods:
                    if (raw_ns, owner_pod) in active_pod_keys and owner_pod not in claiming_pods:
                        claiming_pods.append(owner_pod)
                if claiming_pods:
                    pvc_info["phase"] = sanitize_cell(f"Pending ({format_claiming_pods(sorted(claiming_pods))})")
                    pending_pvcs.append(pvc_info)
                else:
                    pvc_info["phase"] = "Pending (waiting for first consumer)"
                    waiting_pvcs.append(pvc_info)
        elif raw_phase != "Bound":
            pending_pvcs.append(pvc_info)

    # 3. Parse PVs
    pv_list = []
    unhealthy_pvs = []
    for item in pv_items:
        name = sanitize_cell(item.get("metadata", {}).get("name", ""))
        raw_phase = item.get("status", {}).get("phase", "Unknown")
        phase = sanitize_cell(raw_phase)
        capacity = sanitize_cell(item.get("spec", {}).get("capacity", {}).get("storage", "N/A"))
        reclaim = sanitize_cell(item.get("spec", {}).get("persistentVolumeReclaimPolicy", "Delete"))
        claim_ref = item.get("spec", {}).get("claimRef", {}) or {}
        claim_str = (
            sanitize_cell(f"{claim_ref.get('namespace', '')}/{claim_ref.get('name', '')}")
            if claim_ref.get("name")
            else "None"
        )
        pv_info = {
            "name": name,
            "phase": phase,
            "is_bound": raw_phase == "Bound",
            "is_available": raw_phase == "Available",
            "capacity": capacity,
            "reclaim_policy": reclaim,
            "claim": claim_str,
        }
        pv_list.append(pv_info)
        if raw_phase in ("Failed", "Released"):
            unhealthy_pvs.append(pv_info)

    # 4. Parse VolumeAttachments (age against the UTC instant the `volumeattachments` listing
    # returned so a slow sibling listing does not age an in-flight attach/detach past 2m grace).
    va_now = fetched_at.get("attachments") or _now_utc()
    va_list = []
    attachment_errors = []
    # Freshly created unattached VolumeAttachments or freshly deleted VolumeAttachments (no
    # attachError/detachError within ATTACHMENT_IN_PROGRESS_GRACE) are normal while CSI
    # ControllerPublishVolume / ControllerUnpublishVolume runs; older unattached or deleting
    # objects with no error (e.g. CSI external-attacher down or wedged) count as stalled.
    inflight_vas = []
    for item in va_items:
        meta = item.get("metadata") or {}
        spec = item.get("spec") or {}
        status = item.get("status") or {}
        name = sanitize_cell(meta.get("name", ""))
        creation_ts = meta.get("creationTimestamp")
        deletion_ts = meta.get("deletionTimestamp")
        age = format_age(creation_ts, va_now)
        node = sanitize_cell(spec.get("nodeName", "unknown"))
        pv_source = sanitize_cell((spec.get("source") or {}).get("persistentVolumeName", ""))
        attached = bool(status.get("attached", False))
        attach_err = (status.get("attachError") or {}).get("message", "") or ""
        detach_err = (status.get("detachError") or {}).get("message", "") or ""
        raw_err_msg = attach_err or detach_err
        err_msg = (sanitize_cell(raw_err_msg) or "CSI status error (non-printable message)") if raw_err_msg else ""
        va_info = {
            "name": name,
            "node": node,
            "pv": pv_source,
            "attached": attached,
            "age": age,
            "state": "attached" if attached else "unattached",
            "error": err_msg,
        }
        va_list.append(va_info)
        if raw_err_msg:
            attachment_errors.append(va_info)
        elif deletion_ts is not None and deletion_ts != "":
            del_age = format_age(deletion_ts, va_now)
            if is_within_grace(deletion_ts, va_now, ATTACHMENT_IN_PROGRESS_GRACE):
                va_info["state"] = f"detaching ({del_age})"
                inflight_vas.append(va_info)
            else:
                va_info["error"] = (
                    f"deleting for {del_age} (>=2m grace) while attached={attached} with no CSI status error"
                    if del_age != "N/A"
                    else f"deleting with invalid deletionTimestamp while attached={attached} and no CSI status error"
                )
                attachment_errors.append(va_info)
        elif not attached:
            if is_within_grace(creation_ts, va_now, ATTACHMENT_IN_PROGRESS_GRACE):
                va_info["state"] = "attaching"
                inflight_vas.append(va_info)
            else:
                va_info["error"] = (
                    f"unattached for {age} (>=2m grace) with no CSI status error"
                    if age != "N/A"
                    else "unattached with no valid creationTimestamp or CSI status error"
                )
                attachment_errors.append(va_info)

    # Summary Statistics
    total_pvcs = len(pvc_list)
    bound_pvcs = sum(1 for p in pvc_list if p["is_bound"])
    total_pvs = len(pv_list)
    bound_pvs = sum(1 for p in pv_list if p["is_bound"])
    available_pvs = sum(1 for p in pv_list if p["is_available"])
    total_scs = len(sc_list)
    total_vas = len(va_list)
    attached_vas = sum(1 for va in va_list if va["attached"])

    def counted(key: str, text: str) -> str:
        # A kind whose listing failed has no count; printing the empty fallback's 0 would read as measured.
        return UNREAD_COUNT_TEXT if key in unread else text

    unverified_wffc_suffix = (
        f", {len(unverified_wffc_pvcs)} WFFC consumer pods unread" if unverified_wffc_pvcs else ""
    )
    print("## Kubernetes Storage Fleet Audit\n")
    print(
        "- **PersistentVolumeClaims (PVCs):** "
        + counted(
            "pvcs",
            f"{total_pvcs} total ({bound_pvcs} Bound, {len(waiting_pvcs)} waiting for first consumer"
            f"{unverified_wffc_suffix}, {len(pending_pvcs)} Pending/Unbound)",
        )
    )
    print("- **PersistentVolumes (PVs):** " + counted("pvs", f"{total_pvs} total ({bound_pvs} Bound, {available_pvs} Available, {len(unhealthy_pvs)} Failed/Released)"))
    print("- **StorageClasses:** " + counted("scs", f"{total_scs} available (Default: `{default_sc}`)"))
    print("- **VolumeAttachments:** " + counted("attachments", f"{total_vas} total ({attached_vas} Attached, {len(inflight_vas)} in-flight <2m, {len(attachment_errors)} errors/stalled)") + "\n")

    # Stop-on-Clean Rule Status
    is_clean = len(pending_pvcs) == 0 and len(unhealthy_pvs) == 0 and len(attachment_errors) == 0
    if unread:
        degraded_suffix = (
            ""
            if is_clean
            else (
                f"; degraded among read kinds: {len(pending_pvcs)} PVCs not Bound, "
                f"{len(unhealthy_pvs)} Failed/Released PVs, {len(attachment_errors)} VolumeAttachment errors/stalled"
            )
        )
        stop_detail = (
            "Report the unread kinds below as unread, not healthy."
            if is_clean
            else "Report the unread kinds below as unread, not healthy, and inspect degraded storage resources below."
        )
        print(f"**Storage Audit Verdict: Incomplete** (could not read: {', '.join(sorted(unread))}{degraded_suffix}).")
        print(f"CLEAN_AUDIT_STOP: NO — {stop_detail}\n")
        for key, reason in sorted(unread.items()):
            print(f"- `{key}`: {reason}")
        print("")
    elif is_clean and inflight_vas:
        print(f"**Storage Audit Verdict: Clean (In-Flight Attach/Detach)** ({bound_pvcs} PVCs Bound, {len(waiting_pvcs)} waiting for first consumer, 0 Failed/Released PVs, 0 VolumeAttachment errors/stalled, {len(inflight_vas)} VolumeAttachments in-flight <2m).")
        print(f"CLEAN_AUDIT_STOP: YES — No degraded storage resources ({len(inflight_vas)} VolumeAttachment(s) in-flight <2m; {STORAGE_SCOPE_NOTE}); call kanban_complete immediately.\n")
    elif is_clean:
        print(f"**Storage Audit Verdict: Clean & Fully Healthy** ({bound_pvcs} PVCs Bound, {len(waiting_pvcs)} waiting for first consumer, 0 Failed/Released PVs, 0 VolumeAttachment errors/stalled).")
        print(f"CLEAN_AUDIT_STOP: YES — No degraded storage resources ({STORAGE_SCOPE_NOTE}); call kanban_complete immediately.\n")
    else:
        print(f"**Storage Audit Verdict: Degradation Detected** ({len(pending_pvcs)} PVCs not Bound, {len(unhealthy_pvs)} Failed/Released PVs, {len(attachment_errors)} VolumeAttachment errors/stalled).")
        print("CLEAN_AUDIT_STOP: NO — Inspect degraded storage resources below.\n")

    if sc_list:
        print("### StorageClasses")
        print(f"{'NAME':<25} {'PROVISIONER':<35} {'RECLAIM':<12} {'BINDING MODE':<18} {'DEFAULT'}")
        print("-" * 100)
        for sc in sorted(sc_list, key=lambda x: (not x["is_default"], x["name"])):
            def_str = "YES (default)" if sc["is_default"] else "no"
            print(f"{sc['name']:<25} {sc['provisioner']:<35} {sc['reclaim_policy']:<12} {sc['binding_mode']:<18} {def_str}")
        print("")

    if pvc_list:
        print("### PersistentVolumeClaims (PVCs)")
        print(f"{'NAMESPACE':<22} {'NAME':<32} {'STATUS':<12} {'AGE':<8} {'CAPACITY':<10} {'STORAGECLASS':<20} {'VOLUME'}")
        print("-" * 124)
        for p in sorted(pvc_list, key=lambda x: (x["is_bound"], x["namespace"], x["name"])):
            print(f"{p['namespace']:<22} {p['name']:<32} {p['phase']:<12} {p['age']:<8} {p['capacity']:<10} {p['storage_class']:<20} {p['volume']}")
        print("")
    elif "pvcs" not in unread:
        print("No PersistentVolumeClaims found on this cluster.\n")

    if pv_list:
        print("### PersistentVolumes (PVs)")
        print(f"{'NAME':<38} {'STATUS':<12} {'CAPACITY':<10} {'RECLAIM':<12} {'CLAIM'}")
        print("-" * 95)
        for p in sorted(pv_list, key=lambda x: (x["is_bound"], x["name"])):
            print(f"{p['name']:<38} {p['phase']:<12} {p['capacity']:<10} {p['reclaim_policy']:<12} {p['claim']}")
        print("")
    elif "pvs" not in unread:
        print("No PersistentVolumes found on this cluster.\n")

    if attachment_errors:
        print("### Degraded VolumeAttachments (Errors or Stalled Attach/Detach)")
        print(f"{'NAME':<35} {'NODE':<30} {'PV':<30} {'ATTACHED':<10} {'AGE':<8} {'ERROR / REASON'}")
        print("-" * 125)
        for va in attachment_errors:
            print(f"{va['name']:<35} {va['node']:<30} {va['pv']:<30} {str(va['attached']):<10} {va['age']:<8} {va['error']}")
        print("")

    if inflight_vas:
        print("### In-Flight VolumeAttachments (In-Flight <2m, No Error)")
        print(f"{'NAME':<35} {'NODE':<30} {'PV':<30} {'ATTACHED':<10} {'STATE':<18} {'AGE'}")
        print("-" * 135)
        for va in inflight_vas:
            print(f"{va['name']:<35} {va['node']:<30} {va['pv']:<30} {str(va['attached']):<10} {va['state']:<18} {va['age']}")
        print("")

    return 0


def main():
    parser = argparse.ArgumentParser(description="Audit Kubernetes storage resources.", allow_abbrev=False)
    parser.add_argument("--kubeconfig", default=None, help="Path to kubeconfig file")
    args = parser.parse_args()
    sys.exit(audit_storage(args.kubeconfig))


if __name__ == "__main__":
    main()
