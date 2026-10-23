#!/usr/bin/env python3
"""audit_certificates.py - High-performance cert-manager and TLS certificate auditor.

Discovers whether cert-manager.io CRDs exist (full, partial, or absent), audits
Certificates, Issuers, ClusterIssuers and GKE ManagedCertificates, and counts
CertificateRequests and native kubernetes.io/tls Secrets (count & namespaces), in parallel.
Emits CLEAN_AUDIT_STOP: YES only when every queried kind was read and no Certificate is
unready, expired, renewal-overdue or failing issuance, no Issuer is unready, and no
ManagedCertificate is outside Active. Native TLS Secrets are counted, not checked for expiry; a
Forbidden Secret listing leaves them uncounted, and any other listing failure blocks YES.
Part of kube-agents #3285.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import sys

CERT_MANAGER_CRDS = (
    "certificates.cert-manager.io",
    "issuers.cert-manager.io",
    "clusterissuers.cert-manager.io",
    "certificaterequests.cert-manager.io",
)
MANAGED_CERT_CRD = "managedcertificates.networking.gke.io"
MANAGED_CERT_ACTIVE = "Active"
# cert-manager sets Issuing=False with this reason when an issuance or renewal attempt fails.
ISSUING_FAILED_REASON = "Failed"
# How much of a failed query's error text the unread list shows per kind.
UNREAD_REASON_MAX_CHARS = 200
# Per-call kubectl deadline: 60 s broker admission wait + 60 s broker kubectl run cap + 5 s margin,
# matching cluster_preflight.sh so queued calls on the shared 8-slot sandbox pool do not time out locally.
KUBECTL_TIMEOUT_SECONDS = 125
# The sandbox's kubectl wrapper that forwards calls through the credential proxy.
CREDENTIAL_PROXY_BIN = "/opt/credential-proxy/bin"
# Marker emitted on stderr when the credential proxy cuts output at CREDENTIAL_PROXY_MAX_OUTPUT_BYTES.
PROXY_TRUNCATED_MARKER = "credential proxy output truncated"
# kubectl's RBAC denial text; a Forbidden Secret listing is reported as uncounted, not as unread.
FORBIDDEN_MARKER = "forbidden"
# How long Issuing=True may hold before a passed renewalTime counts as overdue again; a renewal
# stuck on a pending ACME challenge or an unapproved CertificateRequest stays Issuing=True forever.
RENEWAL_IN_PROGRESS_GRACE = timedelta(hours=1)
# Metadata-only Secret listing, so no tls.key or tls.crt payload leaves the credential proxy.
TLS_SECRET_COLUMNS = "custom-columns=NS:.metadata.namespace,NAME:.metadata.name,CREATED:.metadata.creationTimestamp"
# Whole days in a timedelta; floor division keeps "ago" and "remaining" both rounded down.
ONE_DAY = timedelta(days=1)
# What a summary line prints in place of a count for a kind whose listing failed.
UNREAD_COUNT_TEXT = "unread (listing failed; see the verdict below)"


def run_cmd(args: list[str], env: dict[str, str]) -> tuple[int, str]:
    try:
        res = subprocess.run(
            args,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=KUBECTL_TIMEOUT_SECONDS,
            check=False,
        )
        if res.returncode == 0:
            stderr_msg = res.stderr.strip()
            if PROXY_TRUNCATED_MARKER in stderr_msg.lower():
                return 1, stderr_msg
            return 0, res.stdout.strip()
        return res.returncode, res.stderr.strip() or f"kubectl exit {res.returncode} with no output"
    except Exception as e:
        return 1, str(e)


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
        return res.returncode or 1, {"stderr": res.stderr.strip() or f"kubectl exit {res.returncode} with no output"}
    except Exception as e:
        return 1, {"error": str(e)}


def summarize_tls_secrets(sec_code: int, sec_out: str) -> tuple[list[dict], str]:
    """Summarize kubernetes.io/tls secrets (count & namespaces) from a metadata-only listing."""
    if sec_code != 0:
        if FORBIDDEN_MARKER in sec_out.lower():
            return [], "not counted (RBAC forbids cluster-wide Secret listing)"
        return [], f"could not be listed ({sec_out[:UNREAD_REASON_MAX_CHARS]})"

    tls_secrets: list[dict] = []
    ns_counts: Counter[str] = Counter()
    for line in sec_out.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        ns, name = fields[0], fields[1]
        created = fields[2] if len(fields) > 2 else "N/A"
        tls_secrets.append({"namespace": ns, "name": name, "created": created})
        ns_counts[ns] += 1

    if not tls_secrets:
        return [], "0 secret(s) found across all namespaces"

    ns_summary = ", ".join(f"{ns} ({cnt})" for ns, cnt in sorted(ns_counts.items()))
    return tls_secrets, f"{len(tls_secrets)} secret(s) across {len(ns_counts)} namespace(s) [{ns_summary}]"


def discover_installed_crds(env: dict[str, str]) -> tuple[set[str] | None, str]:
    """Return (installed cert-manager.io and ManagedCertificate CRDs, error); None when the listing failed."""
    code, out = run_cmd(
        ["kubectl", "get", "crd", "-o", "custom-columns=NAME:.metadata.name", "--no-headers"],
        env=env,
    )
    if code != 0:
        return None, out[:UNREAD_REASON_MAX_CHARS]
    return {
        line.strip()
        for line in out.splitlines()
        if line.strip().endswith(".cert-manager.io") or line.strip() == MANAGED_CERT_CRD
    }, ""


def audit_certificates(kubeconfig: str | None = None) -> int:
    env = os.environ.copy()
    if CREDENTIAL_PROXY_BIN not in env.get("PATH", ""):
        env["PATH"] = f"{CREDENTIAL_PROXY_BIN}:" + env.get("PATH", "")
    if kubeconfig:
        env["KUBECONFIG"] = kubeconfig

    installed_crds, crd_error = discover_installed_crds(env)
    if installed_crds is None:
        print("## Cert-Manager & TLS Certificate Fleet Audit\n")
        print(f"- **cert-manager Status:** UNKNOWN (CRD listing failed: {crd_error})\n")
        print("**Certificate Audit Verdict: Incomplete** (could not read: customresourcedefinitions).")
        print("CLEAN_AUDIT_STOP: NO — Report certificate health as unread, not healthy.\n")
        return 0
    cm_crds = sorted(c for c in installed_crds if c.endswith(".cert-manager.io"))
    cert_manager_installed = len(cm_crds) > 0

    # Build concurrent queries for all installed CRDs + native kubernetes.io/tls secrets
    tasks: dict[str, list[str]] = {}
    if "certificates.cert-manager.io" in installed_crds:
        tasks["certificates"] = ["get", "certificates.cert-manager.io", "-A"]
    if "issuers.cert-manager.io" in installed_crds:
        tasks["issuers"] = ["get", "issuers.cert-manager.io", "-A"]
    if "clusterissuers.cert-manager.io" in installed_crds:
        tasks["clusterissuers"] = ["get", "clusterissuers.cert-manager.io"]
    if "certificaterequests.cert-manager.io" in installed_crds:
        tasks["certrequests"] = ["get", "certificaterequests.cert-manager.io", "-A"]
    if MANAGED_CERT_CRD in installed_crds:
        tasks["managedcerts"] = ["get", MANAGED_CERT_CRD, "-A"]

    results: dict[str, tuple[int, dict]] = {}
    # One worker per CRD listing plus one for the TLS Secret listing.
    with ThreadPoolExecutor(max_workers=len(tasks) + 1) as executor:
        sec_future = executor.submit(
            run_cmd,
            ["kubectl", "get", "secrets", "-A", "--field-selector", "type=kubernetes.io/tls", "-o", TLS_SECRET_COLUMNS, "--no-headers"],
            env,
        )
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
        sec_code, sec_out = sec_future.result()

    tls_secrets, tls_summary_str = summarize_tls_secrets(sec_code, sec_out)
    secrets_forbidden = sec_code != 0 and FORBIDDEN_MARKER in sec_out.lower()
    if sec_code == 0:
        tls_summary_str += " (count only; expiry not inspected)"
    tls_note = (
        "native TLS Secrets were not counted (RBAC forbids Secret listing)"
        if secrets_forbidden
        else "native TLS Secrets were counted, not checked for expiry"
    )
    # A queried kind that failed (timeout, Forbidden, cut or unparseable reply) is unread, never empty.
    unread = {
        key: str(data.get("stderr") or data.get("error") or "")[:UNREAD_REASON_MAX_CHARS]
        for key, (code, data) in results.items()
        if code != 0
    }
    if sec_code != 0 and not secrets_forbidden:
        unread["tls_secrets"] = sec_out[:UNREAD_REASON_MAX_CHARS]
    mc_items = results.get("managedcerts", (1, {}))[1].get("items", [])
    unhealthy_mcs = [
        (mc.get("metadata", {}).get("namespace", ""), mc.get("metadata", {}).get("name", ""), mc.get("status", {}).get("certificateStatus", "Unknown"))
        for mc in mc_items
        if mc.get("status", {}).get("certificateStatus") != MANAGED_CERT_ACTIVE
    ]

    def counted(keys: tuple[str, ...], text: str) -> str:
        # A kind whose listing failed has no count; printing the empty fallback's 0 would read as measured.
        return UNREAD_COUNT_TEXT if any(k in unread for k in keys) else text

    def print_incomplete_or_mc_failures() -> bool:
        """Print the unread kinds and failing ManagedCertificates; True when either exists."""
        for key, reason in sorted(unread.items()):
            print(f"- Could not read `{key}`: {reason}")
        for ns, name, cert_status in unhealthy_mcs:
            print(f"- ManagedCertificate `{ns}/{name}`: {cert_status}")
        if unread or unhealthy_mcs:
            print(f"\n**Certificate Audit Verdict: Incomplete or Degraded** ({len(unread)} unread kinds, {len(unhealthy_mcs)} ManagedCertificates not Active).")
            print("CLEAN_AUDIT_STOP: NO — Report unread kinds as unread and failing ManagedCertificates as degraded.\n")
        return bool(unread or unhealthy_mcs)

    if not cert_manager_installed:
        print("## Cert-Manager & TLS Certificate Fleet Audit\n")
        print("- **cert_manager_installed:** `false`")
        print("- **cert-manager Status:** NOT INSTALLED on this cluster (0 `cert-manager.io` CRDs found).")
        print("- **GKE ManagedCertificates:** " + counted(("managedcerts",), f"{len(mc_items)} total ({len(unhealthy_mcs)} not Active)"))
        print(f"- **Native TLS Secrets (`kubernetes.io/tls`):** {tls_summary_str}\n")
        if not print_incomplete_or_mc_failures():
            print("**Certificate Audit Verdict: Clean / Not Installed** (cert-manager is not active on this cluster; 0 failing ManagedCertificates).")
            print(f"CLEAN_AUDIT_STOP: YES — No failed certificates found; {tls_note}; call kanban_complete immediately.\n")

        if tls_secrets:
            print("### Active TLS Secrets")
            print(f"{'NAMESPACE':<25} {'SECRET NAME':<40} {'CREATED AT'}")
            print("-" * 85)
            for s in tls_secrets:
                print(f"{s['namespace']:<25} {s['name']:<40} {s['created']}")
            print("")
        return 0

    cert_items = results.get("certificates", (1, {}))[1].get("items", [])
    issuer_items = results.get("issuers", (1, {}))[1].get("items", [])
    ci_items = results.get("clusterissuers", (1, {}))[1].get("items", [])
    cr_items = results.get("certrequests", (1, {}))[1].get("items", [])

    # Parse Certificates
    cert_list = []
    unready_certs = []
    now = datetime.now(timezone.utc)

    for item in cert_items:
        ns = item.get("metadata", {}).get("namespace", "default")
        name = item.get("metadata", {}).get("name", "")
        secret_name = item.get("spec", {}).get("secretName", "")
        issuer_ref = item.get("spec", {}).get("issuerRef", {})
        issuer_str = f"{issuer_ref.get('kind', 'Issuer')}/{issuer_ref.get('name', '')}"

        status = item.get("status", {})
        conditions = status.get("conditions", []) or []
        ready_cond = next((c for c in conditions if c.get("type") == "Ready"), {})
        is_ready = ready_cond.get("status") == "True"
        reason = ready_cond.get("reason", "Ready" if is_ready else "NotReady")
        message = ready_cond.get("message", "")
        issuing_cond = next((c for c in conditions if c.get("type") == "Issuing"), {})
        if issuing_cond.get("status") == "False" and issuing_cond.get("reason") == ISSUING_FAILED_REASON:
            is_ready, reason, message = False, "IssuingFailed", issuing_cond.get("message", message)
        renewal_str = status.get("renewalTime", "")
        # A renewal that went Issuing=True within the grace window is in progress, not yet overdue.
        try:
            issuing_since = issuing_cond.get("lastTransitionTime", "")
            renewing = (
                issuing_cond.get("status") == "True"
                and bool(issuing_since)
                and now - datetime.fromisoformat(issuing_since.replace("Z", "+00:00")) < RENEWAL_IN_PROGRESS_GRACE
            )
            if is_ready and not renewing and renewal_str and datetime.fromisoformat(renewal_str.replace("Z", "+00:00")) < now:
                is_ready, reason = False, "RenewalOverdue"
                message = message or f"renewalTime {renewal_str} has passed without a renewal"
        except ValueError:
            pass

        not_after_str = status.get("notAfter", "")
        days_remaining_str = "N/A"
        is_expired = False
        if not_after_str:
            try:
                exp_dt = datetime.fromisoformat(not_after_str.replace("Z", "+00:00"))
                delta = exp_dt - now
                if delta < timedelta(0):
                    days_remaining_str = f"EXPIRED ({-delta // ONE_DAY}d ago)"
                    is_expired = True
                else:
                    days_remaining_str = f"{delta // ONE_DAY}d"
            except Exception:
                days_remaining_str = not_after_str

        cert_info = {
            "namespace": ns,
            "name": name,
            "ready": is_ready and not is_expired,
            "secret": secret_name,
            "issuer": issuer_str,
            "expires": not_after_str or "N/A",
            "remaining": days_remaining_str,
            "reason": reason,
            "message": message,
        }
        cert_list.append(cert_info)
        if not cert_info["ready"]:
            unready_certs.append(cert_info)

    # Parse Issuers & ClusterIssuers
    all_issuers = []
    unready_issuers = []

    for item in ci_items:
        name = item.get("metadata", {}).get("name", "")
        conditions = item.get("status", {}).get("conditions", []) or []
        ready_cond = next((c for c in conditions if c.get("type") == "Ready"), {})
        is_ready = ready_cond.get("status") == "True"
        reason = ready_cond.get("reason", "Ready" if is_ready else "NotReady")

        spec = item.get("spec", {})
        itype = next((k for k in ("acme", "ca", "vault", "selfSigned") if k in spec), "Unknown")
        info = {
            "scope": "ClusterIssuer",
            "name": name,
            "ready": is_ready,
            "type": itype,
            "reason": reason,
        }
        all_issuers.append(info)
        if not is_ready:
            unready_issuers.append(info)

    for item in issuer_items:
        ns = item.get("metadata", {}).get("namespace", "default")
        name = item.get("metadata", {}).get("name", "")
        conditions = item.get("status", {}).get("conditions", []) or []
        ready_cond = next((c for c in conditions if c.get("type") == "Ready"), {})
        is_ready = ready_cond.get("status") == "True"
        reason = ready_cond.get("reason", "Ready" if is_ready else "NotReady")

        spec = item.get("spec", {})
        itype = next((k for k in ("acme", "ca", "vault", "selfSigned") if k in spec), "Unknown")
        info = {
            "scope": f"Issuer ({ns})",
            "name": name,
            "ready": is_ready,
            "type": itype,
            "reason": reason,
        }
        all_issuers.append(info)
        if not is_ready:
            unready_issuers.append(info)

    # Summary
    total_certs = len(cert_list)
    ready_certs = sum(1 for c in cert_list if c["ready"])
    total_issuers = len(all_issuers)
    ready_issuers = sum(1 for i in all_issuers if i["ready"])
    missing_core_crds = [c for c in CERT_MANAGER_CRDS if c not in installed_crds]
    crd_mode = (
        f"Active ({len(cm_crds)} CRDs installed: {', '.join(cm_crds)})"
        if not missing_core_crds
        else f"Partial CRD installation ({len(cm_crds)} installed: {', '.join(cm_crds)}; absent: {', '.join(missing_core_crds)})"
    )

    print("## Cert-Manager & TLS Certificate Fleet Audit\n")
    print("- **cert_manager_installed:** `true`")
    print(f"- **cert-manager Status:** {crd_mode}")
    print("- **Certificates:** " + counted(("certificates",), f"{total_certs} total ({ready_certs} Ready, {len(unready_certs)} Unready/Degraded)"))
    print("- **Issuers & ClusterIssuers:** " + counted(("issuers", "clusterissuers"), f"{total_issuers} total ({ready_issuers} Ready, {len(unready_issuers)} Unready)"))
    print("- **CertificateRequests (counted, status not checked):** " + counted(("certrequests",), f"{len(cr_items)}") + " | **GKE ManagedCertificates:** " + counted(("managedcerts",), f"{len(mc_items)} ({len(unhealthy_mcs)} not Active)"))
    print(f"- **Native TLS Secrets (`kubernetes.io/tls`):** {tls_summary_str}\n")

    is_clean = len(unready_certs) == 0 and len(unready_issuers) == 0
    blocked = print_incomplete_or_mc_failures()
    if is_clean and not blocked:
        print("**Certificate Audit Verdict: Clean & Fully Valid** (All Certificates & Issuers are Ready, 0 renewal errors).")
        print(f"CLEAN_AUDIT_STOP: YES — No failed, expired, or renewal-overdue certificates/issuers found; {tls_note}; call kanban_complete immediately.\n")
    elif not is_clean:
        print(f"**Certificate Audit Verdict: Degraded Certificates Detected** ({len(unready_certs)} unready certs, {len(unready_issuers)} unready issuers).")
        if not blocked:
            print("CLEAN_AUDIT_STOP: NO — Inspect degraded certificates/issuers listed below.\n")

    if cert_list:
        print("### Certificates")
        print(f"{'NAMESPACE':<20} {'NAME':<32} {'READY':<10} {'REMAINING':<15} {'ISSUER':<25} {'SECRET'}")
        print("-" * 125)
        for c in cert_list:
            ready_str = "True" if c["ready"] else f"FALSE ({c['reason']})"
            print(f"{c['namespace']:<20} {c['name']:<32} {ready_str:<10} {c['remaining']:<15} {c['issuer']:<25} {c['secret']}")
        print("")
    elif "certificates" not in unread:
        print("No Certificate resources declared in this cluster.\n")

    if all_issuers:
        print("### Issuers & ClusterIssuers")
        print(f"{'SCOPE':<25} {'NAME':<32} {'READY':<10} {'TYPE':<15} {'REASON'}")
        print("-" * 105)
        for i in all_issuers:
            ready_str = "True" if i["ready"] else "FALSE"
            print(f"{i['scope']:<25} {i['name']:<32} {ready_str:<10} {i['type']:<15} {i['reason']}")
        print("")

    if tls_secrets:
        print("### Active TLS Secrets")
        print(f"{'NAMESPACE':<25} {'SECRET NAME':<40} {'CREATED AT'}")
        print("-" * 85)
        for s in tls_secrets:
            print(f"{s['namespace']:<25} {s['name']:<40} {s['created']}")
        print("")

    if unready_certs:
        print("### Certificate Failures & Error Messages")
        for c in unready_certs:
            print(f"- `{c['namespace']}/{c['name']}`: {c['message'] or c['reason']}")
        print("")

    return 0


def main():
    parser = argparse.ArgumentParser(description="Audit cert-manager and TLS certificate resources.")
    parser.add_argument("--kubeconfig", default=None, help="Path to kubeconfig file")
    args = parser.parse_args()
    sys.exit(audit_certificates(args.kubeconfig))


if __name__ == "__main__":
    main()
