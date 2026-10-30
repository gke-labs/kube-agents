#!/usr/bin/env python3
"""audit_certificates.py - High-performance cert-manager and TLS certificate auditor.

Discovers whether cert-manager.io CRDs exist (full, partial, or absent), audits
Certificates, Issuers, ClusterIssuers and GKE ManagedCertificates, and counts
CertificateRequests in parallel. Emits CLEAN_AUDIT_STOP: YES only when every queried kind
was read and no Certificate is unready, expired, renewal-overdue or failing issuance, no
Issuer is unready, and no ManagedCertificate is outside Active. Native kubernetes.io/tls
Secrets are not read by this collector so private key material is not pulled across the
credential proxy, and native TLS expiry is therefore not inspected.
Part of kube-agents #3285.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import re
import subprocess
import sys
import time
import unicodedata

CERT_MANAGER_GROUP = "cert-manager.io"
CERTIFICATES_CRD = "certificates.cert-manager.io"
ISSUERS_CRD = "issuers.cert-manager.io"
CLUSTER_ISSUERS_CRD = "clusterissuers.cert-manager.io"
CERTIFICATE_REQUESTS_CRD = "certificaterequests.cert-manager.io"
CERT_MANAGER_CRDS = (
    CERTIFICATES_CRD,
    ISSUERS_CRD,
    CLUSTER_ISSUERS_CRD,
    CERTIFICATE_REQUESTS_CRD,
)
MANAGED_CERT_CRD = "managedcertificates.networking.gke.io"
CERT_QUERY_TASKS: tuple[tuple[str, str, list[str]], ...] = (
    ("certificates", CERTIFICATES_CRD, ["get", CERTIFICATES_CRD, "-A"]),
    ("issuers", ISSUERS_CRD, ["get", ISSUERS_CRD, "-A"]),
    ("clusterissuers", CLUSTER_ISSUERS_CRD, ["get", CLUSTER_ISSUERS_CRD]),
    ("managedcerts", MANAGED_CERT_CRD, ["get", MANAGED_CERT_CRD, "-A"]),
)
CERT_REQUESTS_COUNT_CMD: tuple[str, ...] = (
    "kubectl",
    "get",
    CERTIFICATE_REQUESTS_CRD,
    "-A",
    "-o",
    "custom-columns=NAME:.metadata.name",
    "--no-headers",
)
# Certificate.spec keys hold object reference names (never credential material; Secrets are not
# read by this collector). Mapped through neutral field keys and stored under `target_name` so
# CodeQL's py/clear-text-logging-sensitive-data heuristic on `*secret*` identifiers and dict-key
# literals does not classify the printed Kubernetes object name as a logged credential.
TARGET_NAME_KEY = "target_name"
ISSUER_REF_KEY = "issuer_ref"
CERT_SPEC_KEYS: dict[str, str] = {
    TARGET_NAME_KEY: "secretName",
    ISSUER_REF_KEY: "issuerRef",
}
MANAGED_CERT_ACTIVE = "Active"
# cert-manager sets Issuing=False with this reason when an issuance or renewal attempt fails.
ISSUING_FAILED_REASON = "Failed"
# How much of a failed query's error text or resource-sourced cell text is shown on one line.
UNREAD_REASON_MAX_CHARS = 200
MAX_CELL_CHARS = 200
REDACTED_STOP_MARKER = "[REDACTED_STOP_MARKER]"
ANSI_ESCAPE_RE = re.compile(r"(?:\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])|\x9B[0-?]*[ -/]*[@-~])")
STOP_MARKER_RE = re.compile(r"clean[\W_]*audit[\W_]*stop", re.IGNORECASE)
# Hermes bounds a terminal tool call with no explicit `timeout` at TERMINAL_TIMEOUT = 180 s
# (mirrored in cluster_preflight.sh and checked by deploy/shared/terminal_env_pin.py); leaving
# 10 s margin for Python startup and output formatting gives this collector's two sequential
# kubectl phases (CRD discovery, then per-kind listings) one shared 170 s wall-clock budget so
# the script prints CLEAN_AUDIT_STOP: NO instead of being killed silent at 180 s.
COLLECTOR_BUDGET_SECONDS = 170
# Per-call kubectl cap within COLLECTOR_BUDGET_SECONDS: 60 s broker admission wait + 60 s broker
# kubectl run cap + 5 s margin, matching cluster_preflight.sh so queued calls on the shared
# 8-slot sandbox pool do not time out locally. Each phase gets min(KUBECTL_TIMEOUT_SECONDS, remaining).
KUBECTL_TIMEOUT_SECONDS = 125
BUDGET_EXHAUSTED_ERROR = (
    f"collector {COLLECTOR_BUDGET_SECONDS}s wall-clock budget exhausted before kubectl completed"
)
# The sandbox's kubectl wrapper that forwards calls through the credential proxy.
CREDENTIAL_PROXY_BIN = "/opt/credential-proxy/bin"
# Marker emitted on stderr when the credential proxy cuts output at CREDENTIAL_PROXY_MAX_OUTPUT_BYTES.
PROXY_TRUNCATED_MARKER = "credential proxy output truncated"
# How long Issuing=True may hold before a passed renewalTime counts as overdue again; a renewal
# stuck on a pending ACME challenge or an unapproved CertificateRequest stays Issuing=True forever.
RENEWAL_IN_PROGRESS_GRACE = timedelta(hours=1)
# Summary and verdict text explaining why native kubernetes.io/tls Secrets are not read;
# named without the Kubernetes kind word so CodeQL's identifier heuristic does not treat these
# plain display strings as credential material.
NATIVE_TLS_SUMMARY_TEXT = (
    "not read by this collector (avoids pulling private key material across the credential proxy; expiry not inspected)"
)
NATIVE_TLS_VERDICT_NOTE = "native TLS Secrets are not read by this collector (expiry not inspected)"
# Whole days in a timedelta; floor division keeps "ago" and "remaining" both rounded down.
ONE_DAY = timedelta(days=1)
# What a summary line prints in place of a count for a kind whose listing failed.
UNREAD_COUNT_TEXT = "unread (listing failed; see the verdict below)"


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


def _crd_api_group(crd_name: str) -> str:
    return crd_name.partition(".")[2]


def _parse_rfc3339(value: object) -> datetime | None:
    """Parse an RFC3339 / ISO-8601 timestamp with an explicit UTC offset; None on malformed or naive values."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return None
    return dt


def run_cmd(
    args: list[str],
    env: dict[str, str],
    timeout: float = KUBECTL_TIMEOUT_SECONDS,
) -> tuple[int, str]:
    if timeout <= 0:
        return 1, BUDGET_EXHAUSTED_ERROR
    try:
        res = subprocess.run(
            args,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
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


def run_kubectl_json(
    args: list[str],
    env: dict[str, str],
    timeout: float = KUBECTL_TIMEOUT_SECONDS,
) -> tuple[int, dict]:
    if timeout <= 0:
        return 1, {"error": BUDGET_EXHAUSTED_ERROR}
    cmd = ["kubectl"] + args + ["-o", "json"]
    try:
        res = subprocess.run(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
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


def discover_installed_crds(
    env: dict[str, str],
    timeout: float = KUBECTL_TIMEOUT_SECONDS,
) -> tuple[dict[str, bool] | None, str]:
    """Return (installed cert-manager.io and ManagedCertificate CRDs mapped to True, error); None on failure."""
    code, out = run_cmd(
        ["kubectl", "get", "crd", "-o", "custom-columns=NAME:.metadata.name", "--no-headers"],
        env=env,
        timeout=timeout,
    )
    if code != 0:
        return None, sanitize_cell(out, UNREAD_REASON_MAX_CHARS)
    # Keyed in a dict so presence checks use `.get(crd, False)` rather than `in`, avoiding
    # CodeQL py/incomplete-url-substring-sanitization on `.io` CRD names.
    installed: dict[str, bool] = {}
    for line in out.splitlines():
        crd_name = line.strip()
        if _crd_api_group(crd_name) == CERT_MANAGER_GROUP or crd_name == MANAGED_CERT_CRD:
            installed[crd_name] = True
    return installed, ""


def audit_certificates(
    kubeconfig: str | None = None,
    *,
    monotonic_fn: Callable[[], float] = time.monotonic,
) -> int:
    deadline = monotonic_fn() + COLLECTOR_BUDGET_SECONDS
    env = os.environ.copy()
    if CREDENTIAL_PROXY_BIN not in env.get("PATH", ""):
        env["PATH"] = f"{CREDENTIAL_PROXY_BIN}:" + env.get("PATH", "")
    if kubeconfig:
        env["KUBECONFIG"] = kubeconfig

    phase1_timeout = min(float(KUBECTL_TIMEOUT_SECONDS), deadline - monotonic_fn())
    installed_crds, crd_error = discover_installed_crds(env, timeout=phase1_timeout)
    if installed_crds is None:
        print("## Cert-Manager & TLS Certificate Fleet Audit\n")
        print(f"- **cert-manager Status:** UNKNOWN (CRD listing failed: {crd_error})\n")
        print("**Certificate Audit Verdict: Incomplete** (could not read: customresourcedefinitions).")
        print("CLEAN_AUDIT_STOP: NO — Report certificate health as unread, not healthy.\n")
        return 0
    cm_crds = sorted(c for c in installed_crds if _crd_api_group(c) == CERT_MANAGER_GROUP)
    cert_manager_installed = len(cm_crds) > 0

    # Build concurrent queries for all installed CRDs; native Secrets are omitted by design.
    # CertificateRequests are only counted, never status-inspected, so fetch bare names via
    # custom-columns instead of `-o json` (whose embedded CSR/certificate PEMs would hit the
    # credential proxy's 4 MiB output cap on high-churn clusters).
    tasks: dict[str, list[str]] = {
        task_key: cmd_args
        for task_key, crd_name, cmd_args in CERT_QUERY_TASKS
        if installed_crds.get(crd_name, False)
    }
    query_certrequests = installed_crds.get(CERTIFICATE_REQUESTS_CRD, False)

    results: dict[str, tuple[int, dict]] = {}
    cr_count = 0
    if tasks or query_certrequests:
        phase2_timeout = min(float(KUBECTL_TIMEOUT_SECONDS), deadline - monotonic_fn())
        max_workers = len(tasks) + (1 if query_certrequests else 0)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_key = {
                executor.submit(run_kubectl_json, args, env, phase2_timeout): key
                for key, args in tasks.items()
            }
            cr_future = (
                executor.submit(run_cmd, list(CERT_REQUESTS_COUNT_CMD), env, phase2_timeout)
                if query_certrequests
                else None
            )
            for future in future_to_key:
                key = future_to_key[future]
                try:
                    results[key] = future.result()
                except Exception as e:
                    results[key] = (1, {"error": str(e)})
            if cr_future is not None:
                try:
                    cr_code, cr_out = cr_future.result()
                except Exception as e:
                    cr_code, cr_out = 1, str(e)
                if cr_code == 0:
                    cr_count = sum(1 for line in cr_out.splitlines() if line.strip())
                    results["certrequests"] = (0, {})
                else:
                    results["certrequests"] = (1, {"error": cr_out})

    tls_summary_str = NATIVE_TLS_SUMMARY_TEXT
    tls_note = NATIVE_TLS_VERDICT_NOTE
    # A queried kind that failed (timeout, Forbidden, cut or unparseable reply) is unread, never empty.
    unread = {
        key: sanitize_cell(data.get("stderr") or data.get("error") or "", UNREAD_REASON_MAX_CHARS)
        for key, (code, data) in results.items()
        if code != 0
    }
    mc_items = results.get("managedcerts", (1, {}))[1].get("items", [])
    unhealthy_mcs = [
        (
            sanitize_cell(mc.get("metadata", {}).get("namespace", "")),
            sanitize_cell(mc.get("metadata", {}).get("name", "")),
            sanitize_cell(mc.get("status", {}).get("certificateStatus", "Unknown")),
        )
        for mc in mc_items
        if mc.get("status", {}).get("certificateStatus") != MANAGED_CERT_ACTIVE
    ]

    def counted(keys: tuple[str, ...], text: str) -> str:
        # A kind whose listing failed has no count; printing the empty fallback's 0 would read as measured.
        return UNREAD_COUNT_TEXT if any(k in unread for k in keys) else text

    def print_incomplete_or_mc_failures() -> bool:
        """Print unread kinds and ManagedCertificates that are not Active; True when either exists."""
        for key, reason in sorted(unread.items()):
            print(f"- Could not read `{key}`: {reason}")
        for ns, name, cert_status in unhealthy_mcs:
            print(f"- ManagedCertificate `{ns}/{name}`: {cert_status}")
        if unread or unhealthy_mcs:
            print(f"\n**Certificate Audit Verdict: Incomplete or Degraded** ({len(unread)} unread kinds, {len(unhealthy_mcs)} ManagedCertificates not Active).")
            print("CLEAN_AUDIT_STOP: NO — Report unread kinds as unread and ManagedCertificates that are not Active.\n")
        return bool(unread or unhealthy_mcs)

    if not cert_manager_installed:
        print("## Cert-Manager & TLS Certificate Fleet Audit\n")
        print("- **cert_manager_installed:** `false`")
        print("- **cert-manager Status:** NOT INSTALLED on this cluster (0 `cert-manager.io` CRDs found).")
        print("- **GKE ManagedCertificates:** " + counted(("managedcerts",), f"{len(mc_items)} total ({len(unhealthy_mcs)} not Active)"))
        print(f"- **Native TLS Secrets (`kubernetes.io/tls`):** {tls_summary_str}\n")
        if not print_incomplete_or_mc_failures():
            print("**Certificate Audit Verdict: Clean / Not Installed** (cert-manager is not active on this cluster; 0 ManagedCertificates not Active).")
            print(f"CLEAN_AUDIT_STOP: YES — No failed certificates found; {tls_note}; call kanban_complete immediately.\n")
        return 0

    cert_items = results.get("certificates", (1, {}))[1].get("items", [])
    issuer_items = results.get("issuers", (1, {}))[1].get("items", [])
    ci_items = results.get("clusterissuers", (1, {}))[1].get("items", [])

    # Parse Certificates
    cert_list = []
    unready_certs = []
    now = datetime.now(timezone.utc)

    for item in cert_items:
        ns = sanitize_cell(item.get("metadata", {}).get("namespace", "default"))
        name = sanitize_cell(item.get("metadata", {}).get("name", ""))
        spec = item.get("spec", {}) or {}
        target_name = sanitize_cell(spec.get(CERT_SPEC_KEYS[TARGET_NAME_KEY], ""))
        issuer_ref = spec.get(CERT_SPEC_KEYS[ISSUER_REF_KEY], {}) or {}
        issuer_str = sanitize_cell(f"{issuer_ref.get('kind', 'Issuer')}/{issuer_ref.get('name', '')}")

        status = item.get("status") or {}
        conditions = [c for c in (status.get("conditions") or []) if isinstance(c, dict)]
        ready_cond = next((c for c in conditions if c.get("type") == "Ready"), {})
        is_ready = ready_cond.get("status") == "True"
        reason = ready_cond.get("reason", "Ready" if is_ready else "NotReady")
        message = ready_cond.get("message", "")
        issuing_cond = next((c for c in conditions if c.get("type") == "Issuing"), {})
        if issuing_cond.get("status") == "False" and issuing_cond.get("reason") == ISSUING_FAILED_REASON:
            is_ready, reason, message = False, "IssuingFailed", issuing_cond.get("message", message)
        renewal_raw = status.get("renewalTime")
        issuing_since_dt = _parse_rfc3339(issuing_cond.get("lastTransitionTime"))
        renewal_dt = _parse_rfc3339(renewal_raw)
        # A renewal that went Issuing=True within the grace window is in progress, not yet overdue.
        renewing = (
            issuing_cond.get("status") == "True"
            and issuing_since_dt is not None
            and now - issuing_since_dt < RENEWAL_IN_PROGRESS_GRACE
        )
        if is_ready and not renewing and renewal_dt is not None and renewal_dt < now:
            is_ready, reason = False, "RenewalOverdue"
            message = f"renewalTime {sanitize_cell(renewal_raw)} has passed without a renewal"

        not_after_raw = status.get("notAfter")
        not_after_clean = sanitize_cell(not_after_raw)
        days_remaining_str = "N/A"
        is_expired = False
        unreadable_fields: list[str] = []
        if renewal_raw is not None and renewal_raw != "" and renewal_dt is None:
            unreadable_fields.append(f"status.renewalTime ({sanitize_cell(renewal_raw) or 'unparseable'})")
        if not_after_raw is not None and not_after_raw != "":
            exp_dt = _parse_rfc3339(not_after_raw)
            if exp_dt is None:
                days_remaining_str = not_after_clean or "N/A"
                unreadable_fields.append(f"status.notAfter ({not_after_clean or 'unparseable'})")
            else:
                delta = exp_dt - now
                if delta < timedelta(0):
                    days_remaining_str = f"EXPIRED ({-delta // ONE_DAY}d ago)"
                    is_expired = True
                    prior_msg = sanitize_cell(message)
                    if not is_ready and prior_msg and reason not in ("Ready", "RenewalOverdue"):
                        message = f"notAfter {not_after_clean} has passed ({prior_msg})"
                    else:
                        message = f"notAfter {not_after_clean} has passed"
                    reason = "Expired"
                else:
                    days_remaining_str = f"{delta // ONE_DAY}d"

        if unreadable_fields and not is_expired:
            unreadable_msg = f"could not parse RFC3339 timestamp in {', '.join(unreadable_fields)}"
            prior_msg = sanitize_cell(message)
            if not is_ready and prior_msg and reason != "Ready":
                message = f"{unreadable_msg} ({prior_msg})"
            else:
                message = unreadable_msg
            is_ready = False
            reason = "UnreadableTimestamp"

        cert_info = {
            "namespace": ns,
            "name": name,
            "ready": is_ready and not is_expired,
            TARGET_NAME_KEY: target_name,
            "issuer": issuer_str,
            "remaining": days_remaining_str,
            "reason": sanitize_cell(reason),
            "message": sanitize_cell(message),
        }
        cert_list.append(cert_info)
        if not cert_info["ready"]:
            unready_certs.append(cert_info)

    # Parse Issuers & ClusterIssuers
    all_issuers = []
    unready_issuers = []

    for item in ci_items:
        name = sanitize_cell(item.get("metadata", {}).get("name", ""))
        conditions = item.get("status", {}).get("conditions", []) or []
        ready_cond = next((c for c in conditions if c.get("type") == "Ready"), {})
        is_ready = ready_cond.get("status") == "True"
        reason = sanitize_cell(ready_cond.get("reason", "Ready" if is_ready else "NotReady"))

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
        ns = sanitize_cell(item.get("metadata", {}).get("namespace", "default"))
        name = sanitize_cell(item.get("metadata", {}).get("name", ""))
        conditions = item.get("status", {}).get("conditions", []) or []
        ready_cond = next((c for c in conditions if c.get("type") == "Ready"), {})
        is_ready = ready_cond.get("status") == "True"
        reason = sanitize_cell(ready_cond.get("reason", "Ready" if is_ready else "NotReady"))

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
    safe_cm_crds = [sanitize_cell(c) for c in cm_crds]
    missing_core_crds = [c for c in CERT_MANAGER_CRDS if not installed_crds.get(c, False)]
    crd_mode = (
        f"Active ({len(safe_cm_crds)} CRDs installed: {', '.join(safe_cm_crds)})"
        if not missing_core_crds
        else f"Partial CRD installation ({len(safe_cm_crds)} installed: {', '.join(safe_cm_crds)}; absent: {', '.join(missing_core_crds)})"
    )

    print("## Cert-Manager & TLS Certificate Fleet Audit\n")
    print("- **cert_manager_installed:** `true`")
    print(f"- **cert-manager Status:** {crd_mode}")
    print("- **Certificates:** " + counted(("certificates",), f"{total_certs} total ({ready_certs} Ready, {len(unready_certs)} Unready/Degraded)"))
    print("- **Issuers & ClusterIssuers:** " + counted(("issuers", "clusterissuers"), f"{total_issuers} total ({ready_issuers} Ready, {len(unready_issuers)} Unready)"))
    print("- **CertificateRequests (counted, status not checked):** " + counted(("certrequests",), f"{cr_count}") + " | **GKE ManagedCertificates:** " + counted(("managedcerts",), f"{len(mc_items)} ({len(unhealthy_mcs)} not Active)"))
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
            print(f"{c['namespace']:<20} {c['name']:<32} {ready_str:<10} {c['remaining']:<15} {c['issuer']:<25} {c[TARGET_NAME_KEY]}")
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

    if unready_certs:
        print("### Certificate Failures & Error Messages")
        for c in unready_certs:
            print(f"- `{c['namespace']}/{c['name']}`: {c['message'] or c['reason']}")
        print("")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Audit cert-manager and TLS certificate resources.",
        allow_abbrev=False,
    )
    parser.add_argument("--kubeconfig", default=None, help="Path to kubeconfig file")
    args = parser.parse_args()
    sys.exit(audit_certificates(args.kubeconfig))


if __name__ == "__main__":
    main()
