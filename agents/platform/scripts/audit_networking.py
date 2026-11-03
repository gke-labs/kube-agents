#!/usr/bin/env python3
"""audit_networking.py - High-performance Kubernetes ingress, gateway, and service auditor.

Single-pass concurrent collector for Services (LoadBalancer/NodePort), Ingresses,
Gateway API (Gateways, HTTPRoutes), and GKE Network Extensions (BackendConfig,
HealthCheckPolicy). Discovers and audits networking infrastructure in one pass.
Part of kube-agents #3285.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
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
# Per-call kubectl deadline: 60 s broker admission wait + 60 s broker kubectl run cap + 5 s margin,
# matching cluster_preflight.sh so queued calls on the shared 8-slot sandbox pool do not time out locally.
KUBECTL_TIMEOUT_SECONDS = 125
# The sandbox's kubectl wrapper that forwards calls through the credential proxy.
CREDENTIAL_PROXY_BIN = "/opt/credential-proxy/bin"
# Marker emitted on stderr when the credential proxy cuts output at CREDENTIAL_PROXY_MAX_OUTPUT_BYTES.
PROXY_TRUNCATED_MARKER = "credential proxy output truncated"
# Optional CRD kinds: kubectl's "no such resource type" error for these means the kind is not installed.
OPTIONAL_CRD_KEYS = frozenset({"gateways", "httproutes", "backendconfigs", "healthcheckpolicies"})
MISSING_RESOURCE_TYPE_MARKER = "doesn't have a resource type"
# The parentRef kind the Gateway API defaults to when spec.parentRefs omits it.
DEFAULT_PARENT_KIND = "Gateway"
# Fallback reason when an unprogrammed Gateway reports no reason on Programmed/Ready/Accepted.
DEFAULT_UNPROGRAMMED_REASON = "no status conditions reported"
# Gateway listener conditions whose False status means that listener does not serve traffic.
LISTENER_HEALTH_CONDITIONS = ("Accepted", "Programmed", "ResolvedRefs")
ACCEPTED_CONDITION = "Accepted"
RESOLVED_REFS_CONDITION = "ResolvedRefs"
PARTIALLY_INVALID_CONDITION = "PartiallyInvalid"
ROUTE_HEALTH_CONDITIONS = (ACCEPTED_CONDITION, RESOLVED_REFS_CONDITION)
CONDITION_TRUE = "True"
CONDITION_FALSE = "False"
MISSING_ACCEPTED_STATUS = "Accepted=missing"
# Annotation the GKE Ingress controller writes with each backend service's health-check state (JSON map).
GKE_BACKENDS_ANNOTATION = "ingress.kubernetes.io/backends"
# The only backend state the GKE Ingress controller reports for a serving backend.
HEALTHY_BACKEND_STATE = "HEALTHY"
KNOWN_NON_HEALTHY_BACKEND_STATES = frozenset({"UNHEALTHY", "Unknown"})
UNRECOGNIZED_BACKEND_STATE = "unrecognised state"
MAX_BACKEND_NAME_CHARS = 80
# What a summary line prints in place of a count for a kind whose listing failed.
UNREAD_COUNT_TEXT = "unread (listing failed; see the verdict below)"
# Ingress object keys and default class name.
METADATA_KEY = "metadata"
SPEC_KEY = "spec"
ANNOTATIONS_KEY = "annotations"
INGRESS_CLASS_KEY = "ingressClassName"
LEGACY_INGRESS_CLASS_ANNOTATION = "kubernetes.io/ingress.class"
DEFAULT_INGRESS_CLASS = "default"
# Clean-stop line naming the exact signals checked (Gateway and non-GKE Ingress backend health are not read).
CLEAN_VERDICT_STOP_LINE = (
    "CLEAN_AUDIT_STOP: YES — All Ingresses, Gateways, and LoadBalancers have allocated addresses and valid "
    "HTTPRoute routing, and no GKE Ingress backend reports unhealthy (GRPCRoute/TCPRoute/TLSRoute, Gateway and "
    "non-GKE Ingress backend health not read); call kanban_complete immediately.\n"
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


def unhealthy_backends(annotations: dict) -> list[str]:
    """Backends the GKE Ingress controller reports as not HEALTHY; an unparseable annotation is one entry."""
    raw = (annotations or {}).get(GKE_BACKENDS_ANNOTATION)
    if not raw:
        return []
    try:
        states = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return [f"{GKE_BACKENDS_ANNOTATION} unparseable"]
    if not isinstance(states, dict):
        return [f"{GKE_BACKENDS_ANNOTATION} unparseable"]
    entries = []
    for backend, state in states.items():
        if state == HEALTHY_BACKEND_STATE:
            continue
        backend_label = sanitize_cell(backend, MAX_BACKEND_NAME_CHARS)
        state_label = (
            state
            if isinstance(state, str) and state in KNOWN_NON_HEALTHY_BACKEND_STATES
            else UNRECOGNIZED_BACKEND_STATE
        )
        entries.append(f"{backend_label}={state_label}")
    return sorted(entries)


def parent_ref_key(ref: dict, route_namespace: str) -> tuple:
    """Identity of a parentRef, so status.parents[].parentRef is matched to spec.parentRefs, not counted."""
    return (
        ref.get("kind") or DEFAULT_PARENT_KIND,
        ref.get("namespace") or route_namespace,
        ref.get("name", ""),
        ref.get("sectionName"),
        ref.get("port"),
    )


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


def audit_networking(kubeconfig: str | None = None) -> int:
    env = os.environ.copy()
    if CREDENTIAL_PROXY_BIN not in env.get("PATH", ""):
        env["PATH"] = f"{CREDENTIAL_PROXY_BIN}:" + env.get("PATH", "")
    if kubeconfig:
        env["KUBECONFIG"] = kubeconfig

    tasks = {
        "services": ["get", "svc", "-A"],
        "ingresses": ["get", "ingress", "-A"],
        "gateways": ["get", "gateways.gateway.networking.k8s.io", "-A"],
        "httproutes": ["get", "httproutes.gateway.networking.k8s.io", "-A"],
        "backendconfigs": ["get", "backendconfigs.cloud.google.com", "-A"],
        "healthcheckpolicies": ["get", "healthcheckpolicies.networking.gke.io", "-A"],
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
            error = str(data.get("error", ""))
            if code != 0 and not (key in OPTIONAL_CRD_KEYS and MISSING_RESOURCE_TYPE_MARKER in error):
                unread[key] = sanitize_cell(error, UNREAD_REASON_MAX_CHARS)

    svc_items = results.get("services", {}).get("items", [])
    ing_items = results.get("ingresses", {}).get("items", [])
    gw_items = results.get("gateways", {}).get("items", [])
    route_items = results.get("httproutes", {}).get("items", [])
    bc_items = results.get("backendconfigs", {}).get("items", [])
    hcp_items = results.get("healthcheckpolicies", {}).get("items", [])

    # 1. Parse Ingresses
    ingress_list = []
    unassigned_ingresses = []
    unhealthy_ingresses = []
    for item in ing_items:
        ns = sanitize_cell(item.get("metadata", {}).get("namespace", "default"))
        name = sanitize_cell(item.get("metadata", {}).get("name", ""))
        annotations = (item.get(METADATA_KEY) or {}).get(ANNOTATIONS_KEY) or {}
        ing_class = sanitize_cell(
            (item.get(SPEC_KEY) or {}).get(INGRESS_CLASS_KEY)
            or annotations.get(LEGACY_INGRESS_CLASS_ANNOTATION)
            or DEFAULT_INGRESS_CLASS
        )
        lb_status = item.get("status", {}).get("loadBalancer", {}).get("ingress", []) or []
        ips = [
            ip
            for ip in (sanitize_cell(i.get("ip") or i.get("hostname", "")) for i in lb_status)
            if ip
        ]
        ip_str = ", ".join(ips) if ips else "(pending / none)"

        rules = item.get("spec", {}).get("rules", []) or []
        hosts = [h for h in (sanitize_cell(r.get("host", "")) for r in rules if r.get("host")) if h] or ["*"]
        host_str = sanitize_cell(", ".join(hosts))

        bad_backends = unhealthy_backends(annotations)

        ing_info = {
            "namespace": ns,
            "name": name,
            "class": ing_class,
            "hosts": host_str,
            "external_ip": ip_str,
            "backends": sanitize_cell(", ".join(bad_backends)) or "no unhealthy backend reported",
        }
        ingress_list.append(ing_info)
        if not ips:
            unassigned_ingresses.append(ing_info)
        if bad_backends:
            unhealthy_ingresses.append(ing_info)

    # 2. Parse Gateways
    gateway_list = []
    unaccepted_gateways = []
    for item in gw_items:
        ns = sanitize_cell(item.get("metadata", {}).get("namespace", "default"))
        name = sanitize_cell(item.get("metadata", {}).get("name", ""))
        gw_class = sanitize_cell(item.get("spec", {}).get("gatewayClassName", "unknown"))
        addresses = item.get("status", {}).get("addresses", []) or []
        addr_strs = [addr for addr in (sanitize_cell(a.get("value", "")) for a in addresses) if addr]
        addr_str = ", ".join(addr_strs) if addr_strs else "(pending)"

        conditions = item.get("status", {}).get("conditions", []) or []
        prog_cond = next((c for c in conditions if c.get("type") in ("Programmed", "Ready")), {})
        acc_cond = next((c for c in conditions if c.get("type") == "Accepted"), {})
        # Programmed/Ready decides when present; Accepted alone only speaks for a Gateway that reports neither.
        is_programmed = (prog_cond or acc_cond).get("status") == CONDITION_TRUE
        reason = sanitize_cell(
            prog_cond.get("reason")
            or acc_cond.get("reason")
            or ("Accepted" if is_programmed else DEFAULT_UNPROGRAMMED_REASON)
        )
        # A Gateway can stay Programmed=True while one listener is invalid; that listener serves nothing.
        listener_failures = [
            sanitize_cell(f"listener {listener.get('name', '')}: {c.get('type')}=False {c.get('reason', '')}".rstrip())
            for listener in item.get("status", {}).get("listeners", []) or []
            for c in listener.get("conditions", []) or []
            if c.get("type") in LISTENER_HEALTH_CONDITIONS and c.get("status") == CONDITION_FALSE
        ]
        if is_programmed and listener_failures:
            is_programmed = False
            reason = sanitize_cell("; ".join(listener_failures))

        gw_info = {
            "namespace": ns,
            "name": name,
            "class": gw_class,
            "addresses": addr_str,
            "programmed": is_programmed,
            "reason": reason,
        }
        gateway_list.append(gw_info)
        if not is_programmed or not addr_strs:
            unaccepted_gateways.append(gw_info)

    # 3. Parse HTTPRoutes
    route_list = []
    degraded_routes = []
    for item in route_items:
        raw_ns = item.get("metadata", {}).get("namespace", "default")
        ns = sanitize_cell(raw_ns)
        name = sanitize_cell(item.get("metadata", {}).get("name", ""))
        hostnames = [sanitize_cell(h) for h in (item.get("spec", {}).get("hostnames", []) or ["*"])]
        parents = item.get("spec", {}).get("parentRefs", []) or []
        parent_names = [sanitize_cell(p.get("name", "")) for p in parents if p.get("name")]

        conditions = []
        route_failed = False
        status_parents = (item.get("status") or {}).get("parents") or []
        for p in status_parents:
            if not isinstance(p, dict):
                route_failed = True
                continue
            parent_conds = [c for c in (p.get("conditions") or []) if isinstance(c, dict)]
            accepted_statuses = []
            resolved_not_true = False
            partially_invalid = False
            for c in parent_conds:
                ctype = c.get("type")
                cstatus = c.get("status")
                if ctype in ROUTE_HEALTH_CONDITIONS:
                    conditions.append(sanitize_cell(f"{ctype}={cstatus}"))
                elif ctype == PARTIALLY_INVALID_CONDITION and cstatus != CONDITION_FALSE:
                    conditions.append(sanitize_cell(f"{ctype}={cstatus}"))
                if ctype == ACCEPTED_CONDITION:
                    accepted_statuses.append(cstatus)
                elif ctype == RESOLVED_REFS_CONDITION and cstatus != CONDITION_TRUE:
                    resolved_not_true = True
                elif ctype == PARTIALLY_INVALID_CONDITION and cstatus != CONDITION_FALSE:
                    partially_invalid = True
            parent_accepted = bool(accepted_statuses) and all(s == CONDITION_TRUE for s in accepted_statuses)
            if not accepted_statuses:
                conditions.append(MISSING_ACCEPTED_STATUS)
            route_failed = route_failed or not parent_accepted or resolved_not_true or partially_invalid
        # A route with no spec.parentRefs or a parentRef that no Gateway controller has reported on is unattached.
        no_parents = not parents
        reported_keys = {parent_ref_key(p.get("parentRef") or {}, raw_ns) for p in status_parents if isinstance(p, dict)}
        not_attached = no_parents or any(
            not isinstance(p, dict) or parent_ref_key(p, raw_ns) not in reported_keys for p in parents
        )
        route_failed = route_failed or not_attached
        if not_attached and not no_parents and conditions:
            # Some parents reported conditions; the row must still show that at least one parentRef did not attach.
            conditions.append("NotAttached")

        route_info = {
            "namespace": ns,
            "name": name,
            "hostnames": sanitize_cell(", ".join(hostnames)),
            "parents": sanitize_cell(", ".join(parent_names)) or "None",
            "status": sanitize_cell(", ".join(conditions))
            or ("NoParentRefs" if no_parents else "NotAttached" if not_attached else "Attached"),
        }
        route_list.append(route_info)
        if route_failed:
            degraded_routes.append(route_info)

    # 4. Parse Services (Focus on LoadBalancer & NodePort)
    lb_services = []
    np_services = []
    cluster_ip_count = 0
    headless_count = 0
    external_name_count = 0
    pending_lb = []

    for item in svc_items:
        ns = sanitize_cell(item.get("metadata", {}).get("namespace", "default"))
        name = sanitize_cell(item.get("metadata", {}).get("name", ""))
        spec = item.get("spec", {}) or {}
        svc_type = spec.get("type", "ClusterIP")
        raw_cluster_ip = spec.get("clusterIP")
        cluster_ip = sanitize_cell(raw_cluster_ip or "")
        ports = spec.get("ports", []) or []
        port_strs = [
            sanitize_cell(f"{p.get('port')}:{p.get('targetPort')}/{p.get('protocol', 'TCP')}")
            for p in ports
        ]

        if svc_type == "LoadBalancer":
            lb_status = item.get("status", {}).get("loadBalancer", {}).get("ingress", []) or []
            ext_ips = [
                ip
                for ip in (sanitize_cell(i.get("ip") or i.get("hostname", "")) for i in lb_status)
                if ip
            ]
            ext_ip_str = ", ".join(ext_ips) if ext_ips else "(pending)"
            svc_info = {
                "namespace": ns,
                "name": name,
                "cluster_ip": cluster_ip,
                "external_ip": sanitize_cell(ext_ip_str),
                "ports": sanitize_cell(", ".join(port_strs)),
            }
            lb_services.append(svc_info)
            if not ext_ips:
                pending_lb.append(svc_info)
        elif svc_type == "NodePort":
            node_port_strs = [
                sanitize_cell(f"{p.get('port')}:{p.get('nodePort')}/{p.get('protocol', 'TCP')}")
                for p in ports
            ]
            np_services.append({
                "namespace": ns,
                "name": name,
                "cluster_ip": cluster_ip,
                "ports": sanitize_cell(", ".join(node_port_strs)),
            })
        elif svc_type == "ExternalName":
            external_name_count += 1
        elif raw_cluster_ip in (None, "") or (isinstance(raw_cluster_ip, str) and raw_cluster_ip.strip() == "None"):
            headless_count += 1
        else:
            cluster_ip_count += 1

    # 5. Parse BackendConfigs & HealthCheckPolicies
    bc_list = [
        {
            "namespace": sanitize_cell(b.get("metadata", {}).get("namespace", "")),
            "name": sanitize_cell(b.get("metadata", {}).get("name", "")),
        }
        for b in bc_items
    ]
    hcp_list = [
        {
            "namespace": sanitize_cell(h.get("metadata", {}).get("namespace", "")),
            "name": sanitize_cell(h.get("metadata", {}).get("name", "")),
        }
        for h in hcp_items
    ]

    total_ingresses = len(ingress_list)
    total_gateways = len(gateway_list)
    total_routes = len(route_list)
    total_lbs = len(lb_services)
    total_nps = len(np_services)

    def counted(key: str, text: str) -> str:
        # A kind whose listing failed has no count; printing the empty fallback's 0 would read as measured.
        return UNREAD_COUNT_TEXT if key in unread else text

    print("## Kubernetes Ingress & Gateway Fleet Audit\n")
    print("- **Ingress Resources:** " + counted("ingresses", f"{total_ingresses} total ({total_ingresses - len(unassigned_ingresses)} Assigned IP, {len(unassigned_ingresses)} Pending/Unallocated, {len(unhealthy_ingresses)} with a backend not HEALTHY)"))
    print("- **Gateway API (Gateways):** " + counted("gateways", f"{total_gateways} total ({total_gateways - len(unaccepted_gateways)} Programmed with an address, {len(unaccepted_gateways)} Not Programmed or no address)"))
    print("- **Gateway API (HTTPRoutes):** " + counted("httproutes", f"{total_routes} total ({len(degraded_routes)} not Accepted=True, ResolvedRefs not True, PartiallyInvalid=True, or not attached)"))
    print("- **LoadBalancer Services:** " + counted("services", f"{total_lbs} total ({total_lbs - len(pending_lb)} Active VIP, {len(pending_lb)} Pending VIP)"))
    print(
        "- **NodePort Services:** "
        + counted(
            "services",
            f"{total_nps} total | **Internal ClusterIP Services:** {cluster_ip_count}"
            f" | **Headless Services:** {headless_count} | **ExternalName Services:** {external_name_count}",
        )
    )
    print("- **GKE Extensions:** " + counted("backendconfigs", f"{len(bc_list)} BackendConfigs") + ", " + counted("healthcheckpolicies", f"{len(hcp_list)} HealthCheckPolicies") + " (listed by name; their health-check results are not read)\n")

    # Stop-on-Clean Rule Status
    is_clean = not (unassigned_ingresses or unhealthy_ingresses or unaccepted_gateways or pending_lb or degraded_routes)
    if unread:
        degraded_suffix = (
            ""
            if is_clean
            else (
                f"; degraded among read kinds: {len(unassigned_ingresses)} unassigned Ingresses, "
                f"{len(unhealthy_ingresses)} Ingresses with unhealthy backends, "
                f"{len(unaccepted_gateways)} unaccepted Gateways, {len(degraded_routes)} failing HTTPRoutes, "
                f"{len(pending_lb)} pending LoadBalancers"
            )
        )
        stop_detail = (
            "Report the unread kinds below as unread, not healthy."
            if is_clean
            else "Report the unread kinds below as unread, not healthy, and inspect degraded networking resources below."
        )
        print(f"**Networking Audit Verdict: Incomplete** (could not read: {', '.join(sorted(unread))}{degraded_suffix}).")
        print(f"CLEAN_AUDIT_STOP: NO — {stop_detail}\n")
        for key, reason in sorted(unread.items()):
            print(f"- `{key}`: {reason}")
        print("")
    elif is_clean:
        print("**Networking Audit Verdict: Clean & Fully Programmed** (All Ingresses, Gateways, and LoadBalancers have allocated IPs and valid HTTPRoute routing, and no Ingress backend the GKE Ingress controller reports on is unhealthy).")
        print(CLEAN_VERDICT_STOP_LINE)
    else:
        print(f"**Networking Audit Verdict: Routing / VIP Degradation Detected** ({len(unassigned_ingresses)} unassigned Ingresses, {len(unhealthy_ingresses)} Ingresses with unhealthy backends, {len(unaccepted_gateways)} unaccepted Gateways, {len(degraded_routes)} failing HTTPRoutes, {len(pending_lb)} pending LoadBalancers).")
        print("CLEAN_AUDIT_STOP: NO — Inspect degraded networking resources below.\n")

    if gateway_list:
        print("### Gateway API Gateways")
        print(f"{'NAMESPACE':<22} {'NAME':<30} {'CLASS':<25} {'ADDRESS':<20} {'STATUS'}")
        print("-" * 115)
        for g in gateway_list:
            status_str = "Programmed" if g["programmed"] else f"NOT PROGRAMMED ({g['reason']})"
            print(f"{g['namespace']:<22} {g['name']:<30} {g['class']:<25} {g['addresses']:<20} {status_str}")
        print("")

    if route_list:
        print("### Gateway API HTTPRoutes")
        print(f"{'NAMESPACE':<22} {'NAME':<30} {'HOSTNAMES':<30} {'PARENTS':<25} {'STATUS'}")
        print("-" * 125)
        for r in route_list:
            print(f"{r['namespace']:<22} {r['name']:<30} {r['hostnames']:<30} {r['parents']:<25} {r['status']}")
        print("")

    if ingress_list:
        print("### Ingresses")
        print(f"{'NAMESPACE':<22} {'NAME':<30} {'CLASS':<20} {'HOSTS':<30} {'EXTERNAL IP':<22} {'BACKENDS'}")
        print("-" * 150)
        for i in ingress_list:
            print(f"{i['namespace']:<22} {i['name']:<30} {i['class']:<20} {i['hosts']:<30} {i['external_ip']:<22} {i['backends']}")
        print("")
    elif "ingresses" not in unread:
        print("No standard Ingress resources configured on this cluster.\n")

    if lb_services:
        print("### LoadBalancer Services")
        print(f"{'NAMESPACE':<22} {'NAME':<30} {'EXTERNAL IP':<22} {'CLUSTER IP':<18} {'PORTS'}")
        print("-" * 115)
        for s in lb_services:
            print(f"{s['namespace']:<22} {s['name']:<30} {s['external_ip']:<22} {s['cluster_ip']:<18} {s['ports']}")
        print("")

    if np_services:
        print("### NodePort Services")
        print(f"{'NAMESPACE':<22} {'NAME':<30} {'CLUSTER IP':<18} {'PORTS'}")
        print("-" * 95)
        for s in np_services:
            print(f"{s['namespace']:<22} {s['name']:<30} {s['cluster_ip']:<18} {s['ports']}")
        print("")

    if bc_list or hcp_list:
        print("### GKE Ingress Policies")
        for b in bc_list:
            print(f"- BackendConfig: `{b['namespace']}/{b['name']}`")
        for h in hcp_list:
            print(f"- HealthCheckPolicy: `{h['namespace']}/{h['name']}`")
        print("")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Audit Kubernetes ingress, gateway, and networking services.",
        allow_abbrev=False,
    )
    parser.add_argument("--kubeconfig", default=None, help="Path to kubeconfig file")
    args = parser.parse_args()
    sys.exit(audit_networking(args.kubeconfig))


if __name__ == "__main__":
    main()
