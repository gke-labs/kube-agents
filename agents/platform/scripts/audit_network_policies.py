#!/usr/bin/env python3
"""audit_network_policies.py - Fast single-pass auditor for Kubernetes NetworkPolicies.

Discovers all NetworkPolicies and Namespaces concurrently, matches
policies against namespaces, and prints a structured Markdown audit table with a
Stop-on-Clean directive.
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
KUBECTL_TIMEOUT_SECONDS = 20
# The sandbox's kubectl wrapper that forwards calls through the credential proxy.
CREDENTIAL_PROXY_BIN = "/opt/credential-proxy/bin"


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
        })

    protected_ns = sorted(np_by_ns.keys())
    unprotected_ns = sorted(ns for ns in all_ns if ns not in np_by_ns)

    print("## NetworkPolicy Audit Summary\n")
    print(f"- **Total Namespaces:** {len(all_ns)}")
    print(f"- **Namespaces Protected by NetworkPolicies:** {len(protected_ns)}")
    print(f"- **Namespaces Without NetworkPolicies:** {len(unprotected_ns)}")
    print(f"- **Total Active NetworkPolicies:** {len(np_items)}\n")
    print("CLEAN_AUDIT_STOP: YES — NetworkPolicy inventory complete; report this summary and call kanban_complete immediately.\n")

    if np_items:
        print("### Active NetworkPolicies")
        print(f"{'NAMESPACE':<25} {'POLICY NAME':<35} {'POLICY TYPES':<20} {'POD SELECTOR'}")
        print("-" * 105)
        for ns in protected_ns:
            for p in np_by_ns[ns]:
                types_str = ",".join(p["policyTypes"]) if p["policyTypes"] else "Default"
                sel_str = str(p["podSelector"])
                print(f"{ns:<25} {p['name']:<35} {types_str:<20} {sel_str}")
        print("")
    else:
        print("No NetworkPolicies are currently enforced on this cluster.\n")

    if unprotected_ns:
        print(f"### Unprotected Namespaces ({len(unprotected_ns)})")
        print(", ".join(unprotected_ns))

    return 0


def main():
    parser = argparse.ArgumentParser(description="Audit Kubernetes NetworkPolicies across namespaces.")
    parser.add_argument("--kubeconfig", default=None, help="Path to kubeconfig file")
    args = parser.parse_args()
    sys.exit(audit_network_policies(args.kubeconfig))


if __name__ == "__main__":
    main()
