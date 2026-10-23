#!/usr/bin/env python3
"""audit_storage.py - High-performance Kubernetes storage auditor.

Single-pass concurrent collector for PersistentVolumeClaims (PVCs),
PersistentVolumes (PVs), StorageClasses, and VolumeAttachments.
Discovers and audits cluster storage state in one pass.
Part of kube-agents #3285.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys

# How much of a failed query's error text the unread list shows per kind.
UNREAD_REASON_MAX_CHARS = 200
# What a summary line prints in place of a count for a kind whose listing failed.
UNREAD_COUNT_TEXT = "unread (listing failed; see the verdict below)"
# A StorageClass binding mode that leaves a PVC Pending, by design, until a pod uses it.
WAIT_FOR_FIRST_CONSUMER = "WaitForFirstConsumer"
# Annotation the scheduler sets on a WaitForFirstConsumer PVC once a pod using it has picked a node.
SELECTED_NODE_ANNOTATION = "volume.kubernetes.io/selected-node"
# Per-call kubectl deadline: 60 s broker admission wait + 60 s broker kubectl run cap + 5 s margin,
# matching cluster_preflight.sh so queued calls on the shared 8-slot sandbox pool do not time out locally.
KUBECTL_TIMEOUT_SECONDS = 125
# The sandbox's kubectl wrapper that forwards calls through the credential proxy.
CREDENTIAL_PROXY_BIN = "/opt/credential-proxy/bin"
# Marker emitted on stderr when the credential proxy cuts output at CREDENTIAL_PROXY_MAX_OUTPUT_BYTES.
PROXY_TRUNCATED_MARKER = "credential proxy output truncated"


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
    # Kinds whose listing failed (timeout, Forbidden, cut or unparseable reply): never read as empty.
    unread: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_to_key = {
            executor.submit(run_kubectl_json, args, env): key
            for key, args in tasks.items()
        }
        for future in future_to_key:
            key = future_to_key[future]
            try:
                code, data = future.result()
            except Exception as e:
                code, data = 1, {"error": str(e)}
            results[key] = data if code == 0 else {}
            if code != 0:
                unread[key] = str(data.get("error", ""))[:UNREAD_REASON_MAX_CHARS]

    pvc_items = results.get("pvcs", {}).get("items", [])
    pv_items = results.get("pvs", {}).get("items", [])
    sc_items = results.get("scs", {}).get("items", [])
    va_items = results.get("attachments", {}).get("items", [])

    # 1. Parse StorageClasses
    sc_list = []
    default_sc = "None"
    for item in sc_items:
        name = item.get("metadata", {}).get("name", "")
        provisioner = item.get("provisioner", "unknown")
        reclaim_policy = item.get("reclaimPolicy", "Delete")
        binding_mode = item.get("volumeBindingMode", "Immediate")
        annotations = item.get("metadata", {}).get("annotations", {}) or {}
        is_default = (
            annotations.get("storageclass.kubernetes.io/is-default-class") == "true"
            or annotations.get("storageclass.beta.kubernetes.io/is-default-class") == "true"
        )
        if is_default:
            default_sc = name
        sc_list.append({
            "name": name,
            "provisioner": provisioner,
            "reclaim_policy": reclaim_policy,
            "binding_mode": binding_mode,
            "is_default": is_default,
        })

    binding_modes = {sc["name"]: sc["binding_mode"] for sc in sc_list}

    # 2. Parse PVCs
    pvc_list = []
    pending_pvcs = []
    # Pending PVCs on a WaitForFirstConsumer class that no pod has claimed yet: expected, not degraded.
    waiting_pvcs = []
    for item in pvc_items:
        ns = item.get("metadata", {}).get("namespace", "default")
        name = item.get("metadata", {}).get("name", "")
        phase = item.get("status", {}).get("phase", "Unknown")
        vol_name = item.get("spec", {}).get("volumeName", "")
        sc_name = item.get("spec", {}).get("storageClassName", "")
        req_storage = (
            item.get("spec", {}).get("resources", {}).get("requests", {}).get("storage", "N/A")
        )
        pvc_info = {
            "namespace": ns,
            "name": name,
            "phase": phase,
            "volume": vol_name or "(unbound)",
            "storage_class": sc_name or "(default)",
            "capacity": req_storage,
        }
        pvc_list.append(pvc_info)
        annotations = item.get("metadata", {}).get("annotations", {}) or {}
        effective_sc = sc_name or (default_sc if default_sc != "None" else "")
        if (
            phase == "Pending"
            and binding_modes.get(effective_sc) == WAIT_FOR_FIRST_CONSUMER
            and SELECTED_NODE_ANNOTATION not in annotations
        ):
            pvc_info["phase"] = "Pending (waiting for first consumer)"
            waiting_pvcs.append(pvc_info)
        elif phase != "Bound":
            pending_pvcs.append(pvc_info)

    # 3. Parse PVs
    pv_list = []
    unhealthy_pvs = []
    for item in pv_items:
        name = item.get("metadata", {}).get("name", "")
        phase = item.get("status", {}).get("phase", "Unknown")
        capacity = item.get("spec", {}).get("capacity", {}).get("storage", "N/A")
        reclaim = item.get("spec", {}).get("persistentVolumeReclaimPolicy", "Delete")
        claim_ref = item.get("spec", {}).get("claimRef", {}) or {}
        claim_str = (
            f"{claim_ref.get('namespace', '')}/{claim_ref.get('name', '')}"
            if claim_ref.get("name")
            else "None"
        )
        pv_info = {
            "name": name,
            "phase": phase,
            "capacity": capacity,
            "reclaim_policy": reclaim,
            "claim": claim_str,
        }
        pv_list.append(pv_info)
        if phase in ("Failed", "Released"):
            unhealthy_pvs.append(pv_info)

    # 4. Parse VolumeAttachments
    va_list = []
    attachment_errors = []
    for item in va_items:
        name = item.get("metadata", {}).get("name", "")
        node = item.get("spec", {}).get("nodeName", "unknown")
        pv_source = item.get("spec", {}).get("source", {}).get("persistentVolumeName", "")
        attached = item.get("status", {}).get("attached", False)
        attach_err = item.get("status", {}).get("attachError", {}).get("message", "")
        detach_err = item.get("status", {}).get("detachError", {}).get("message", "")
        err_msg = attach_err or detach_err
        va_info = {
            "name": name,
            "node": node,
            "pv": pv_source,
            "attached": attached,
            "error": err_msg,
        }
        va_list.append(va_info)
        if err_msg or not attached:
            attachment_errors.append(va_info)

    # Summary Statistics
    total_pvcs = len(pvc_list)
    bound_pvcs = sum(1 for p in pvc_list if p["phase"] == "Bound")
    total_pvs = len(pv_list)
    bound_pvs = sum(1 for p in pv_list if p["phase"] == "Bound")
    available_pvs = sum(1 for p in pv_list if p["phase"] == "Available")
    total_scs = len(sc_list)
    total_vas = len(va_list)

    def counted(key: str, text: str) -> str:
        # A kind whose listing failed has no count; printing the empty fallback's 0 would read as measured.
        return UNREAD_COUNT_TEXT if key in unread else text

    print("## Kubernetes Storage Fleet Audit\n")
    print("- **PersistentVolumeClaims (PVCs):** " + counted("pvcs", f"{total_pvcs} total ({bound_pvcs} Bound, {len(waiting_pvcs)} waiting for first consumer, {len(pending_pvcs)} Pending/Unbound)"))
    print("- **PersistentVolumes (PVs):** " + counted("pvs", f"{total_pvs} total ({bound_pvs} Bound, {available_pvs} Available, {len(unhealthy_pvs)} Failed/Released)"))
    print("- **StorageClasses:** " + counted("scs", f"{total_scs} available (Default: `{default_sc}`)"))
    print("- **VolumeAttachments:** " + counted("attachments", f"{total_vas} total ({len(attachment_errors)} errors or unattached)") + "\n")

    # Stop-on-Clean Rule Status
    is_clean = len(pending_pvcs) == 0 and len(unhealthy_pvs) == 0 and len(attachment_errors) == 0
    if unread:
        print(f"**Storage Audit Verdict: Incomplete** (could not read: {', '.join(sorted(unread))}).")
        print("CLEAN_AUDIT_STOP: NO — Report the unread kinds below as unread, not healthy.\n")
        for key, reason in sorted(unread.items()):
            print(f"- `{key}`: {reason}")
        print("")
    elif is_clean:
        print("**Storage Audit Verdict: Clean & Fully Healthy** (All PVCs Bound, 0 Failed PVs, 0 VolumeAttachment Errors).")
        print("CLEAN_AUDIT_STOP: YES — All storage resources are healthy; call kanban_complete immediately.\n")
    else:
        print(f"**Storage Audit Verdict: Degradation Detected** ({len(pending_pvcs)} pending PVCs, {len(unhealthy_pvs)} failed PVs, {len(attachment_errors)} attachment errors).")
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
        print(f"{'NAMESPACE':<22} {'NAME':<32} {'STATUS':<12} {'CAPACITY':<10} {'STORAGECLASS':<20} {'VOLUME'}")
        print("-" * 115)
        for p in sorted(pvc_list, key=lambda x: (x["phase"] == "Bound", x["namespace"], x["name"])):
            print(f"{p['namespace']:<22} {p['name']:<32} {p['phase']:<12} {p['capacity']:<10} {p['storage_class']:<20} {p['volume']}")
        print("")
    elif "pvcs" not in unread:
        print("No PersistentVolumeClaims found on this cluster.\n")

    if pv_list:
        print("### PersistentVolumes (PVs)")
        print(f"{'NAME':<38} {'STATUS':<12} {'CAPACITY':<10} {'RECLAIM':<12} {'CLAIM'}")
        print("-" * 95)
        for p in sorted(pv_list, key=lambda x: (x["phase"] == "Bound", x["name"])):
            print(f"{p['name']:<38} {p['phase']:<12} {p['capacity']:<10} {p['reclaim_policy']:<12} {p['claim']}")
        print("")
    elif "pvs" not in unread:
        print("No PersistentVolumes found on this cluster.\n")

    if attachment_errors:
        print("### VolumeAttachment Issues")
        print(f"{'NAME':<35} {'NODE':<30} {'PV':<30} {'ATTACHED':<10} {'ERROR'}")
        print("-" * 115)
        for va in attachment_errors:
            print(f"{va['name']:<35} {va['node']:<30} {va['pv']:<30} {str(va['attached']):<10} {va['error']}")
        print("")

    return 0


def main():
    parser = argparse.ArgumentParser(description="Audit Kubernetes storage resources.")
    parser.add_argument("--kubeconfig", default=None, help="Path to kubeconfig file")
    args = parser.parse_args()
    sys.exit(audit_storage(args.kubeconfig))


if __name__ == "__main__":
    main()
