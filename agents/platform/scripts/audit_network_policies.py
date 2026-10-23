#!/usr/bin/env python3
"""audit_network_policies.py - Fast single-pass auditor for Kubernetes NetworkPolicies.

Discovers all NetworkPolicies and Namespaces concurrently, matches
policies against namespaces, and prints a structured Markdown audit table with a
Stop-on-Clean directive. CLEAN_AUDIT_STOP is YES only when both listings were read, every
non-system namespace has a policy selecting every pod for Ingress, and no ingress rule admits
all sources. It does not check that the cluster's network plugin enforces the policies.
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
# Per-call kubectl deadline: 60 s broker admission wait + 60 s broker kubectl run cap + 5 s margin,
# matching cluster_preflight.sh so queued calls on the shared 8-slot sandbox pool do not time out locally.
KUBECTL_TIMEOUT_SECONDS = 125
# The sandbox's kubectl wrapper that forwards calls through the credential proxy.
CREDENTIAL_PROXY_BIN = "/opt/credential-proxy/bin"
# Marker emitted on stderr when the credential proxy cuts output at CREDENTIAL_PROXY_MAX_OUTPUT_BYTES.
PROXY_TRUNCATED_MARKER = "credential proxy output truncated"


# Namespaces Kubernetes and GKE create and manage; a missing policy there is listed but not a verdict gap.
SYSTEM_NAMESPACE_PREFIXES = ("kube-", "gke-", "gmp-")
# CIDRs that cover every IPv4 or IPv6 source.
ALL_ADDRESSES_CIDRS = frozenset({"0.0.0.0/0", "::/0"})


def peer_is_every_source(peer: dict) -> bool:
    """True for a `from` peer that admits every pod in every namespace, or every address."""
    if set(peer) == {"namespaceSelector"} and not peer["namespaceSelector"]:
        return True
    block = peer.get("ipBlock") or {}
    return block.get("cidr") in ALL_ADDRESSES_CIDRS and not block.get("except")


def rule_admits_all_sources(rule: dict) -> bool:
    """True for an ingress rule with no `from` (all sources, on its ports if any) or an every-source peer."""
    peers = (rule or {}).get("from") or []
    return not peers or any(peer_is_every_source(p or {}) for p in peers)


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


def audit_network_policies(kubeconfig: str | None = None) -> int:
    env = os.environ.copy()
    if CREDENTIAL_PROXY_BIN not in env.get("PATH", ""):
        env["PATH"] = f"{CREDENTIAL_PROXY_BIN}:" + env.get("PATH", "")
    if kubeconfig:
        env["KUBECONFIG"] = kubeconfig

    tasks = {
        "namespaces": ["get", "ns"],
        "networkpolicies": ["get", "networkpolicies", "-A"],
    }

    results: dict[str, tuple[int, dict]] = {}
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        future_to_key = {
            executor.submit(run_kubectl_json, args, env): key
            for key, args in tasks.items()
        }
        for future in future_to_key:
            key = future_to_key[future]
            try:
                results[key] = future.result()
            except Exception as e:
                results[key] = (1, {"error": str(e)})

    # A kind whose listing failed (timeout, Forbidden, empty or unparseable reply) is unread, never empty.
    unread = {
        key: str(data.get("error", ""))[:UNREAD_REASON_MAX_CHARS]
        for key, (code, data) in results.items()
        if code != 0
    }
    if unread:
        print("## NetworkPolicy Audit Summary\n")
        print(f"**NetworkPolicy Audit Verdict: Incomplete** (could not read: {', '.join(sorted(unread))}).")
        print("CLEAN_AUDIT_STOP: NO — Report the unread kinds below as unread, not healthy.\n")
        for key, reason in sorted(unread.items()):
            print(f"- `{key}`: {reason}")
        return 0

    ns_data = results["namespaces"][1]
    np_data = results["networkpolicies"][1]

    all_ns = sorted(
        item.get("metadata", {}).get("name", "")
        for item in ns_data.get("items", [])
        if item.get("metadata", {}).get("name")
    )
    np_items = np_data.get("items", [])

    np_by_ns: dict[str, list[dict]] = {}
    for item in np_items:
        meta = item.get("metadata", {}) or {}
        ns = meta.get("namespace", "default")
        name = meta.get("name", "")
        spec = item.get("spec", {}) or {}
        # The whole selector (matchLabels and matchExpressions); only a literal {} selects every pod.
        pod_sel = spec.get("podSelector", {}) or {}
        policy_types = spec.get("policyTypes", [])
        has_ingress = "Ingress" in policy_types or "ingress" in spec
        has_egress = "Egress" in policy_types or "egress" in spec

        np_by_ns.setdefault(ns, []).append({
            "name": name,
            "podSelector": pod_sel or "All Pods ({})",
            "policyTypes": policy_types,
            "has_ingress": has_ingress,
            "has_egress": has_egress,
            "selects_all_pods": not pod_sel,
            "allows_all_ingress": has_ingress and any(rule_admits_all_sources(r) for r in spec.get("ingress", []) or []),
        })

    def ingress_isolated(ns: str) -> bool:
        # Ingress isolation for the whole namespace needs a policy selecting every pod for Ingress.
        return any(p["has_ingress"] and p["selects_all_pods"] for p in np_by_ns.get(ns, []))

    protected_ns = sorted(np_by_ns.keys())
    system_ns = [ns for ns in all_ns if ns.startswith(SYSTEM_NAMESPACE_PREFIXES)]
    user_ns = [ns for ns in all_ns if not ns.startswith(SYSTEM_NAMESPACE_PREFIXES)]
    unprotected_ns = [ns for ns in user_ns if ns not in np_by_ns]
    # Has a policy, but none selects every pod for Ingress (egress-only, or a partial podSelector).
    partial_ns = [ns for ns in user_ns if ns in np_by_ns and not ingress_isolated(ns)]
    allow_all = [(ns, p["name"]) for ns in protected_ns for p in np_by_ns[ns] if p["allows_all_ingress"]]

    print("## NetworkPolicy Audit Summary\n")
    print(f"- **Total Namespaces:** {len(all_ns)} ({len(system_ns)} system namespaces, listed apart and not part of the verdict)")
    print(f"- **Namespaces Protected by NetworkPolicies:** {len(protected_ns)}")
    print(f"- **Namespaces Without NetworkPolicies:** {len(unprotected_ns)} (excluding system namespaces)")
    print(f"- **Namespaces With Policies but No Full Ingress Isolation:** {len(partial_ns)}")
    print(f"- **Total Active NetworkPolicies:** {len(np_items)} ({len(allow_all)} admit ingress from all sources)\n")
    if unprotected_ns or partial_ns or allow_all:
        print(f"**NetworkPolicy Audit Verdict: Isolation Gaps Found** ({len(unprotected_ns)} namespaces without a NetworkPolicy, {len(partial_ns)} without full ingress isolation, {len(allow_all)} allow-all-ingress policies).")
        print("CLEAN_AUDIT_STOP: NO — Report the namespaces and allow-all policies below as gaps; this audit does not check that the network plugin enforces policies.\n")
    else:
        print("**NetworkPolicy Audit Verdict: Every Non-System Namespace Is Ingress-Isolated** (no allow-all-ingress policy found; enforcement by the network plugin is not checked).")
        print("CLEAN_AUDIT_STOP: YES — Every non-system namespace has a policy selecting all pods for Ingress and none admits all sources; report this summary, say that enforcement was not checked, and call kanban_complete immediately.\n")

    if np_items:
        print("### Active NetworkPolicies")
        print(f"{'NAMESPACE':<25} {'POLICY NAME':<35} {'POLICY TYPES':<20} {'POD SELECTOR'}")
        print("-" * 105)
        for ns in protected_ns:
            for p in np_by_ns[ns]:
                types_str = ",".join(p["policyTypes"]) if p["policyTypes"] else "Default"
                if p["allows_all_ingress"]:
                    types_str += " (ALLOW-ALL INGRESS)"
                sel_str = str(p["podSelector"])
                print(f"{ns:<25} {p['name']:<35} {types_str:<20} {sel_str}")
        print("")
    else:
        print("No NetworkPolicies exist on this cluster.\n")

    if unprotected_ns:
        print(f"### Unprotected Namespaces ({len(unprotected_ns)})")
        print(", ".join(unprotected_ns) + "\n")
    if partial_ns:
        print(f"### Namespaces Without Full Ingress Isolation ({len(partial_ns)})")
        print(", ".join(partial_ns) + "\n")
    unprotected_system = [ns for ns in system_ns if ns not in np_by_ns]
    if unprotected_system:
        print(f"### System Namespaces Without NetworkPolicies ({len(unprotected_system)}; not part of the verdict)")
        print(", ".join(unprotected_system))

    return 0


def main():
    parser = argparse.ArgumentParser(description="Audit Kubernetes NetworkPolicies across namespaces.")
    parser.add_argument("--kubeconfig", default=None, help="Path to kubeconfig file")
    args = parser.parse_args()
    sys.exit(audit_network_policies(args.kubeconfig))


if __name__ == "__main__":
    main()
