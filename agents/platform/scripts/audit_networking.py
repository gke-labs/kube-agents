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
import subprocess
import sys

# How much of a failed query's error text the unread list shows per kind.
UNREAD_REASON_MAX_CHARS = 200
# Per-call kubectl deadline; a listing that exceeds it is reported as unread.
KUBECTL_TIMEOUT_SECONDS = 15
# The sandbox's kubectl wrapper that forwards calls through the credential proxy.
CREDENTIAL_PROXY_BIN = "/opt/credential-proxy/bin"
# Optional CRD kinds: kubectl's "no such resource type" error for these means the kind is not installed.
OPTIONAL_CRD_KEYS = frozenset({"gateways", "httproutes", "backendconfigs", "healthcheckpolicies"})
MISSING_RESOURCE_TYPE_MARKER = "doesn't have a resource type"


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
            return 0, json.loads(res.stdout)
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
                unread[key] = error[:UNREAD_REASON_MAX_CHARS]

    svc_items = results.get("services", {}).get("items", [])
    ing_items = results.get("ingresses", {}).get("items", [])
    gw_items = results.get("gateways", {}).get("items", [])
    route_items = results.get("httproutes", {}).get("items", [])
    bc_items = results.get("backendconfigs", {}).get("items", [])
    hcp_items = results.get("healthcheckpolicies", {}).get("items", [])

    # 1. Parse Ingresses
    ingress_list = []
    unassigned_ingresses = []
    for item in ing_items:
        ns = item.get("metadata", {}).get("namespace", "default")
        name = item.get("metadata", {}).get("name", "")
        ing_class = (
            item.get("spec", {}).get("ingressClassName")
            or item.get("metadata", {}).get("annotations", {}).get("kubernetes.io/ingress.class")
            or "default"
        )
        lb_status = item.get("status", {}).get("loadBalancer", {}).get("ingress", []) or []
        ips = [i.get("ip") or i.get("hostname", "") for i in lb_status if (i.get("ip") or i.get("hostname"))]
        ip_str = ", ".join(ips) if ips else "(pending / none)"

        rules = item.get("spec", {}).get("rules", []) or []
        hosts = [r.get("host", "*") for r in rules if r.get("host")] or ["*"]
        host_str = ", ".join(hosts)

        ing_info = {
            "namespace": ns,
            "name": name,
            "class": ing_class,
            "hosts": host_str,
            "external_ip": ip_str,
            "ready": bool(ips),
        }
        ingress_list.append(ing_info)
        if not ips:
            unassigned_ingresses.append(ing_info)

    # 2. Parse Gateways
    gateway_list = []
    unaccepted_gateways = []
    for item in gw_items:
        ns = item.get("metadata", {}).get("namespace", "default")
        name = item.get("metadata", {}).get("name", "")
        gw_class = item.get("spec", {}).get("gatewayClassName", "unknown")
        addresses = item.get("status", {}).get("addresses", []) or []
        addr_strs = [a.get("value", "") for a in addresses if a.get("value")]
        addr_str = ", ".join(addr_strs) if addr_strs else "(pending)"

        conditions = item.get("status", {}).get("conditions", []) or []
        prog_cond = next((c for c in conditions if c.get("type") in ("Programmed", "Ready")), {})
        acc_cond = next((c for c in conditions if c.get("type") == "Accepted"), {})
        # Programmed/Ready decides when present; Accepted alone only speaks for a Gateway that reports neither.
        is_programmed = (prog_cond or acc_cond).get("status") == "True"
        reason = prog_cond.get("reason") or acc_cond.get("reason") or "Accepted"

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
        ns = item.get("metadata", {}).get("namespace", "default")
        name = item.get("metadata", {}).get("name", "")
        hostnames = item.get("spec", {}).get("hostnames", []) or ["*"]
        parents = item.get("spec", {}).get("parentRefs", []) or []
        parent_names = [p.get("name", "") for p in parents if p.get("name")]

        conditions = []
        route_failed = False
        status_parents = item.get("status", {}).get("parents", []) or []
        for p in status_parents:
            for c in p.get("conditions", []) or []:
                if c.get("type") in ("Accepted", "ResolvedRefs"):
                    conditions.append(f"{c.get('type')}={c.get('status')}")
                    route_failed = route_failed or c.get("status") == "False"
        # A parentRef that no Gateway controller has reported on leaves the route unattached there.
        not_attached = len(status_parents) < len(parents)
        route_failed = route_failed or not_attached

        route_info = {
            "namespace": ns,
            "name": name,
            "hostnames": ", ".join(hostnames),
            "parents": ", ".join(parent_names) or "None",
            "status": ", ".join(conditions) or ("NotAttached" if not_attached else "Attached" if parents else "NoParentRefs"),
        }
        route_list.append(route_info)
        if route_failed:
            degraded_routes.append(route_info)

    # 4. Parse Services (Focus on LoadBalancer & NodePort)
    lb_services = []
    np_services = []
    cluster_ip_count = 0
    pending_lb = []

    for item in svc_items:
        ns = item.get("metadata", {}).get("namespace", "default")
        name = item.get("metadata", {}).get("name", "")
        spec = item.get("spec", {})
        svc_type = spec.get("type", "ClusterIP")
        cluster_ip = spec.get("clusterIP", "")
        ports = spec.get("ports", []) or []
        port_strs = [f"{p.get('port')}:{p.get('targetPort')}/{p.get('protocol', 'TCP')}" for p in ports]

        if svc_type == "LoadBalancer":
            lb_status = item.get("status", {}).get("loadBalancer", {}).get("ingress", []) or []
            ext_ips = [i.get("ip") or i.get("hostname", "") for i in lb_status if (i.get("ip") or i.get("hostname"))]
            ext_ip_str = ", ".join(ext_ips) if ext_ips else "(pending)"
            svc_info = {
                "namespace": ns,
                "name": name,
                "type": svc_type,
                "cluster_ip": cluster_ip,
                "external_ip": ext_ip_str,
                "ports": ", ".join(port_strs),
            }
            lb_services.append(svc_info)
            if not ext_ips:
                pending_lb.append(svc_info)
        elif svc_type == "NodePort":
            node_port_strs = [f"{p.get('port')}:{p.get('nodePort')}/{p.get('protocol', 'TCP')}" for p in ports]
            np_services.append({
                "namespace": ns,
                "name": name,
                "type": svc_type,
                "cluster_ip": cluster_ip,
                "ports": ", ".join(node_port_strs),
            })
        else:
            cluster_ip_count += 1

    # 5. Parse BackendConfigs & HealthCheckPolicies
    bc_list = [
        {"namespace": b.get("metadata", {}).get("namespace", ""), "name": b.get("metadata", {}).get("name", "")}
        for b in bc_items
    ]
    hcp_list = [
        {"namespace": h.get("metadata", {}).get("namespace", ""), "name": h.get("metadata", {}).get("name", "")}
        for h in hcp_items
    ]

    total_ingresses = len(ingress_list)
    total_gateways = len(gateway_list)
    total_routes = len(route_list)
    total_lbs = len(lb_services)
    total_nps = len(np_services)

    print("## Kubernetes Ingress & Gateway Fleet Audit\n")
    print(f"- **Ingress Resources:** {total_ingresses} total ({total_ingresses - len(unassigned_ingresses)} Assigned IP, {len(unassigned_ingresses)} Pending/Unallocated)")
    print(f"- **Gateway API (Gateways):** {total_gateways} total ({total_gateways - len(unaccepted_gateways)} Programmed with an address, {len(unaccepted_gateways)} Not Programmed or no address)")
    print(f"- **Gateway API (HTTPRoutes):** {total_routes} total ({len(degraded_routes)} with Accepted=False, ResolvedRefs=False, or not attached)")
    print(f"- **LoadBalancer Services:** {total_lbs} total ({total_lbs - len(pending_lb)} Active VIP, {len(pending_lb)} Pending VIP)")
    print(f"- **NodePort Services:** {total_nps} total | **Internal ClusterIP Services:** {cluster_ip_count}")
    print(f"- **GKE Extensions:** {len(bc_list)} BackendConfigs, {len(hcp_list)} HealthCheckPolicies\n")

    # Stop-on-Clean Rule Status
    is_clean = not (unassigned_ingresses or unaccepted_gateways or pending_lb or degraded_routes)
    if unread:
        print(f"**Networking Audit Verdict: Incomplete** (could not read: {', '.join(sorted(unread))}).")
        print("CLEAN_AUDIT_STOP: NO — Report the unread kinds below as unread, not healthy.\n")
        for key, reason in sorted(unread.items()):
            print(f"- `{key}`: {reason}")
        print("")
    elif is_clean:
        print("**Networking Audit Verdict: Clean & Fully Programmed** (All Ingresses, Gateways, and LoadBalancers have healthy allocated IPs and valid routing).")
        print("CLEAN_AUDIT_STOP: YES — All Ingress, Gateway, and LoadBalancer resources are healthy; call kanban_complete immediately.\n")
    else:
        print(f"**Networking Audit Verdict: Routing / VIP Degradation Detected** ({len(unassigned_ingresses)} unassigned Ingresses, {len(unaccepted_gateways)} unaccepted Gateways, {len(degraded_routes)} failing HTTPRoutes, {len(pending_lb)} pending LoadBalancers).")
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
        print(f"{'NAMESPACE':<22} {'NAME':<30} {'CLASS':<20} {'HOSTS':<30} {'EXTERNAL IP'}")
        print("-" * 125)
        for i in ingress_list:
            print(f"{i['namespace']:<22} {i['name']:<30} {i['class']:<20} {i['hosts']:<30} {i['external_ip']}")
        print("")
    else:
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
    parser = argparse.ArgumentParser(description="Audit Kubernetes ingress, gateway, and networking services.")
    parser.add_argument("--kubeconfig", default=None, help="Path to kubeconfig file")
    args = parser.parse_args()
    sys.exit(audit_networking(args.kubeconfig))


if __name__ == "__main__":
    main()
