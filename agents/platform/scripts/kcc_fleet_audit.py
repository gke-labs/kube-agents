#!/usr/bin/env python3
"""kcc_fleet_audit.py - Batched parallel Config Connector (KCC) custom resource auditor.

Discovers installed `*.cnrm.cloud.google.com` CustomResourceDefinitions in a single
compact `kubectl get crd -o custom-columns=... --no-headers` call (avoiding OpenAPI
schema bloat over the credential proxy's 8 MiB per-stream output cap, 4 MiB when
`CREDENTIAL_PROXY_MAX_OUTPUT_BYTES` is unset), partitions served established CRDs by
scope (`Cluster` vs `Namespaced`), and queries active custom resources in parallel
chunked `kubectl get` batches (`BATCH_CHUNK_SIZE = 35`, `MAX_PARALLEL_WORKERS = 4`)
instead of issuing up to 233 sequential per-CRD RPCs. Automatically falls back to
per-CRD reads if a batch fails or hits credential-proxy output truncation, fails closed
(`CLEAN_AUDIT_STOP: NO`) whenever any CRD cannot be read, and summarizes KCC operator
health (`ConfigConnector`, `ConfigConnectorContext`) and managed resource synchronization.
Part of kube-agents #3142.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parent))

from untrusted_text import strip_unsafe  # noqa: E402

KCC_DOMAIN_LABELS: tuple[str, str, str, str] = ("cnrm", "cloud", "google", "com")
MIN_KCC_CRD_LABELS = 5
CORE_KCC_OPERATOR_KINDS: frozenset[str] = frozenset({"ConfigConnector", "ConfigConnectorContext"})
CONFIG_CONNECTOR_CRD = "configconnectors.core.cnrm.cloud.google.com"
CONFIG_CONNECTOR_CONTEXT_CRD = "configconnectorcontexts.core.cnrm.cloud.google.com"
CRD_SCOPE_CLUSTER = "Cluster"
CRD_SCOPE_NAMESPACED = "Namespaced"
# Compact custom-columns projection (~20 KB for 300 CRDs vs 15+ MiB of OpenAPI v3 schemas
# with `-o json`, which would exceed CREDENTIAL_PROXY_MAX_OUTPUT_BYTES: 8 MiB as deployed
# by the operator, 4 MiB unset fallback).
CRD_DISCOVERY_CMD: tuple[str, ...] = (
    "kubectl",
    "get",
    "crd",
    "-o",
    "custom-columns=NAME:.metadata.name,SCOPE:.spec.scope,SERVED:.spec.versions[*].served,ESTABLISHED:.status.conditions[?(@.type==\"Established\")].status",
    "--no-headers",
)
DISCOVERY_MIN_COLS_SCOPE = 2
DISCOVERY_MIN_COLS_SERVED = 3
DISCOVERY_MIN_COLS_ESTABLISHED = 4

# Batching tuning: 35 CRDs per `kubectl get crd1,crd2,...` reduces ~233 KCC CRDs to 8 RPCs
# across 4 worker threads (~3-5s total). MAX_PARALLEL_WORKERS matches collect.py's 4-slot
# cap under the credential proxy's default 704 MiB child memory budget
# (docs/designs/credential-proxy-child-memory-budget.md §2.2).
BATCH_CHUNK_SIZE = 35
MAX_PARALLEL_WORKERS = 4

# Hermes bounds a terminal tool call with no explicit `timeout` at TERMINAL_TIMEOUT = 180 s
# (mirrored in cluster_preflight.sh and checked by deploy/shared/terminal_env_pin.py); leaving
# 10 s margin for Python startup and output formatting gives this collector's discovery and
# batch phases one shared 170 s wall-clock budget so it prints CLEAN_AUDIT_STOP: NO instead
# of being killed silent at 180 s.
COLLECTOR_BUDGET_SECONDS = 170
# 60s broker slot admission + 60s kubectl run cap + 5s margin (matching cluster_preflight.sh KUBECTL_CAP_SECONDS).
KUBECTL_TIMEOUT_SECONDS = 125
BUDGET_EXHAUSTED_ERROR = (
    f"collector {COLLECTOR_BUDGET_SECONDS}s wall-clock budget exhausted before kubectl completed"
)

# Sandbox kubectl wrapper path that forwards calls through the credential proxy.
PROXY_WRAPPER_BIN = "/opt/credential-proxy/bin"
PROXY_TRUNCATED_MARKER = "credential proxy output truncated"

UNREAD_REASON_MAX_CHARS = 200
MAX_CELL_CHARS = 200
MAX_SAMPLE_CRDS_IN_SUMMARY = 8
# Cap healthy rows rendered in the object table so total stdout stays well below Hermes's
# 50 KB terminal output cap (~105 bytes/row * 200 = ~21 KB); all unhealthy rows always print.
MAX_RENDERED_READY_ROWS = 200
COL_NAMESPACE_WIDTH = 24
COL_KIND_WIDTH = 30
COL_NAME_WIDTH = 32
COL_READY_WIDTH = 10
TABLE_DIVIDER_WIDTH = 115

REDACTED_STOP_MARKER = "[REDACTED_STOP_MARKER]"
STOP_MARKER_RE = re.compile(r"clean[\W_]*audit[\W_]*stop", re.IGNORECASE)
RFC1123_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
UNREAD_COUNT_TEXT = "unread (listing failed; see the verdict below)"


def sanitize_cell(value: object, limit: int = MAX_CELL_CHARS) -> str:
    """Fold untrusted cluster text to one bounded line and neutralize CLEAN_AUDIT_STOP markers."""
    raw = unicodedata.normalize("NFKC", "" if value is None else str(value))
    flat = " ".join(strip_unsafe(raw).split())
    safe = STOP_MARKER_RE.sub(REDACTED_STOP_MARKER, flat)
    return safe[:limit] if limit else safe


def validate_namespace(namespace: str | None) -> str | None:
    """Validate optional `--namespace` against RFC 1123 DNS label syntax."""
    if namespace is None:
        return None
    if not RFC1123_DNS_LABEL_RE.match(namespace):
        raise ValueError(
            f"invalid Kubernetes namespace {namespace!r}: expected an RFC 1123 DNS label"
        )
    return namespace


def is_kcc_crd_name(crd_name: str) -> bool:
    """Check whether `crd_name` belongs to `*.cnrm.cloud.google.com` via exact label tuple equality."""
    labels = crd_name.strip().lower().split(".")
    return len(labels) >= MIN_KCC_CRD_LABELS and tuple(labels[-4:]) == KCC_DOMAIN_LABELS


def _chunk_list(items: list[str], chunk_size: int) -> list[list[str]]:
    return [items[idx : idx + chunk_size] for idx in range(0, len(items), chunk_size)]


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
    except Exception as exc:
        return 1, str(exc)


def run_kubectl_json(
    args: list[str],
    env: dict[str, str],
    timeout: float = KUBECTL_TIMEOUT_SECONDS,
) -> tuple[int, dict]:
    if timeout <= 0:
        return 1, {"error": BUDGET_EXHAUSTED_ERROR}
    cmd = ["kubectl", *args, "-o", "json"]
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
                parsed = json.loads(res.stdout)
                if isinstance(parsed, dict):
                    return 0, parsed
                return 1, {"error": "unexpected non-object JSON output from kubectl"}
            except Exception as exc:
                err_text = f"{stderr_msg} ({exc})" if stderr_msg else str(exc)
                return 1, {"error": err_text}
        return res.returncode or 1, {
            "stderr": res.stderr.strip() or f"kubectl exit {res.returncode} with no output"
        }
    except Exception as exc:
        return 1, {"error": str(exc)}


def _run_bounded_kubectl_json(
    args: list[str],
    env: dict[str, str],
    deadline: float,
    monotonic_fn: Callable[[], float],
) -> tuple[int, dict]:
    """Compute remaining wall-clock budget at worker execution start rather than queue submission."""
    timeout = min(float(KUBECTL_TIMEOUT_SECONDS), deadline - monotonic_fn())
    return run_kubectl_json(args, env, timeout=timeout)


def discover_kcc_crds(
    env: dict[str, str],
    timeout: float = KUBECTL_TIMEOUT_SECONDS,
) -> tuple[dict[str, str] | None, list[str], str]:
    """Return `(served_crd_scopes, unserved_crds, error)` from one compact `kubectl get crd` call.

    `served_crd_scopes` maps each served, established KCC CRD name to its scope
    (`"Cluster"` or `"Namespaced"`). `unserved_crds` records KCC CRDs present on the
    API server that have no served version or `Established!=True` (including `<none>`)
    so they are surfaced rather than queried in a batch (which would fail on the API server).
    """
    code, out = run_cmd(list(CRD_DISCOVERY_CMD), env=env, timeout=timeout)
    if code != 0:
        return None, [], sanitize_cell(out, UNREAD_REASON_MAX_CHARS)

    served_scopes: dict[str, str] = {}
    unserved: list[str] = []
    for raw_line in out.splitlines():
        cols = raw_line.split()
        if not cols:
            continue
        crd_name = cols[0].strip()
        if not is_kcc_crd_name(crd_name):
            continue
        scope_col = cols[1].strip() if len(cols) >= DISCOVERY_MIN_COLS_SCOPE else CRD_SCOPE_NAMESPACED
        served_col = cols[2].strip().lower() if len(cols) >= DISCOVERY_MIN_COLS_SERVED else ""
        established_col = cols[3].strip() if len(cols) >= DISCOVERY_MIN_COLS_ESTABLISHED else ""

        scope = CRD_SCOPE_CLUSTER if scope_col == CRD_SCOPE_CLUSTER else CRD_SCOPE_NAMESPACED
        served_flags = [tok.strip() for tok in served_col.split(",") if tok.strip()]
        has_served_version = "true" in served_flags
        is_established = established_col == "True"

        if has_served_version and is_established:
            served_scopes[crd_name] = scope
        else:
            unserved.append(crd_name)
    return served_scopes, sorted(unserved), ""


def _extract_items(payload: dict) -> list[dict]:
    if "items" in payload and isinstance(payload["items"], list):
        return [item for item in payload["items"] if isinstance(item, dict)]
    if payload.get("kind") and isinstance(payload.get("metadata"), dict):
        return [payload]
    return []


def _build_get_args(crd_names: list[str], scope: str, namespace: str | None) -> list[str]:
    joined = ",".join(crd_names)
    if scope == CRD_SCOPE_CLUSTER:
        return ["get", joined]
    if namespace is not None:
        return ["get", joined, f"--namespace={namespace}"]
    return ["get", joined, "-A"]


def collect_kcc_objects(
    crd_scopes: dict[str, str],
    env: dict[str, str],
    *,
    namespace: str | None,
    deadline: float,
    monotonic_fn: Callable[[], float],
    chunk_size: int = BATCH_CHUNK_SIZE,
) -> tuple[list[dict], dict[str, str]]:
    """Query all served KCC CRDs in parallel batches, retrying failed batches per-CRD."""
    cluster_crds = sorted(c for c, s in crd_scopes.items() if s == CRD_SCOPE_CLUSTER)
    namespaced_crds = sorted(c for c, s in crd_scopes.items() if s != CRD_SCOPE_CLUSTER)

    batches: list[tuple[str, list[str]]] = [
        *((CRD_SCOPE_CLUSTER, chunk) for chunk in _chunk_list(cluster_crds, chunk_size)),
        *((CRD_SCOPE_NAMESPACED, chunk) for chunk in _chunk_list(namespaced_crds, chunk_size)),
    ]
    if not batches:
        return [], {}

    items: list[dict] = []
    failed_singletons: list[tuple[str, str]] = []
    retry_candidates: list[tuple[str, str]] = []

    workers = min(MAX_PARALLEL_WORKERS, len(batches))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(
                _run_bounded_kubectl_json,
                _build_get_args(chunk, scope, namespace),
                env,
                deadline,
                monotonic_fn,
            ): (scope, chunk)
            for scope, chunk in batches
        }
        for future, (scope, chunk) in future_map.items():
            try:
                code, data = future.result()
            except Exception as exc:
                code, data = 1, {"error": str(exc)}
            if code == 0:
                items.extend(_extract_items(data))
            elif len(chunk) == 1:
                reason = data.get("stderr") or data.get("error") or "kubectl get failed"
                failed_singletons.append((chunk[0], sanitize_cell(reason, UNREAD_REASON_MAX_CHARS)))
            else:
                for crd_name in chunk:
                    retry_candidates.append((scope, crd_name))

    unread_crds: dict[str, str] = dict(failed_singletons)
    if retry_candidates:
        retry_workers = min(MAX_PARALLEL_WORKERS, len(retry_candidates))
        with ThreadPoolExecutor(max_workers=retry_workers) as executor:
            retry_futures = {
                executor.submit(
                    _run_bounded_kubectl_json,
                    _build_get_args([crd_name], scope, namespace),
                    env,
                    deadline,
                    monotonic_fn,
                ): crd_name
                for scope, crd_name in retry_candidates
            }
            for fut, crd_name in retry_futures.items():
                try:
                    code, data = fut.result()
                except Exception as exc:
                    code, data = 1, {"error": str(exc)}
                if code == 0:
                    items.extend(_extract_items(data))
                else:
                    reason = data.get("stderr") or data.get("error") or "kubectl get failed"
                    unread_crds[crd_name] = sanitize_cell(reason, UNREAD_REASON_MAX_CHARS)

    return items, unread_crds


def evaluate_kcc_item(item: dict) -> dict[str, object]:
    """Classify one KCC operator CR or managed resource CR into Ready/Healthy vs Unhealthy."""
    kind = sanitize_cell(item.get("kind") or "Unknown")
    meta = item.get("metadata") or {}
    ns_raw = meta.get("namespace")
    namespace = sanitize_cell(ns_raw) if ns_raw else "<cluster>"
    name = sanitize_cell(meta.get("name") or "")
    generation = meta.get("generation")

    status = item.get("status") or {}
    observed_gen = status.get("observedGeneration")
    conditions = [c for c in (status.get("conditions") or []) if isinstance(c, dict)]
    ready_cond = next((c for c in conditions if c.get("type") == "Ready"), None)

    is_operator_cr = kind in CORE_KCC_OPERATOR_KINDS
    if is_operator_cr:
        healthy_flag = status.get("healthy")
        errors_list = [str(e) for e in (status.get("errors") or []) if e]
        spec_mode = sanitize_cell((item.get("spec") or {}).get("mode") or "")
        if healthy_flag is True and not errors_list:
            is_ready = True
            reason = "Healthy"
            message = f"mode={spec_mode}" if spec_mode else ""
        elif healthy_flag is None and ready_cond is not None and ready_cond.get("status") == "True":
            is_ready = True
            reason = sanitize_cell(ready_cond.get("reason") or "Ready")
            message = sanitize_cell(ready_cond.get("message") or "")
        else:
            is_ready = False
            reason = "OperatorUnhealthy" if healthy_flag is False else "StatusPending"
            raw_msg = "; ".join(errors_list) if errors_list else "status.healthy is not true"
            message = sanitize_cell(raw_msg)
    else:
        if ready_cond is None:
            is_ready = False
            reason = "NotYetReconciled"
            message = "status.conditions has no Ready condition"
        elif ready_cond.get("status") != "True":
            is_ready = False
            reason = sanitize_cell(ready_cond.get("reason") or "NotReady")
            message = sanitize_cell(ready_cond.get("message") or "")
        elif (
            isinstance(generation, int)
            and isinstance(observed_gen, int)
            and observed_gen < generation
        ):
            is_ready = False
            reason = "StaleGeneration"
            message = sanitize_cell(
                f"status.observedGeneration ({observed_gen}) lags metadata.generation ({generation})"
            )
        else:
            is_ready = True
            reason = sanitize_cell(ready_cond.get("reason") or "UpToDate")
            message = sanitize_cell(ready_cond.get("message") or "")

    return {
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "is_operator_cr": is_operator_cr,
        "ready": is_ready,
        "reason": sanitize_cell(reason),
        "message": sanitize_cell(message),
    }


def audit_kcc(
    kubeconfig: str | None = None,
    *,
    namespace: str | None = None,
    monotonic_fn: Callable[[], float] = time.monotonic,
    chunk_size: int = BATCH_CHUNK_SIZE,
) -> int:
    validated_ns = validate_namespace(namespace)
    deadline = monotonic_fn() + COLLECTOR_BUDGET_SECONDS
    env = os.environ.copy()
    if PROXY_WRAPPER_BIN not in env.get("PATH", ""):
        env["PATH"] = f"{PROXY_WRAPPER_BIN}:" + env.get("PATH", "")
    if kubeconfig:
        env["KUBECONFIG"] = kubeconfig

    phase1_timeout = min(float(KUBECTL_TIMEOUT_SECONDS), deadline - monotonic_fn())
    served_crd_scopes, unserved_crds, crd_error = discover_kcc_crds(env, timeout=phase1_timeout)
    if served_crd_scopes is None:
        print("## Config Connector (KCC) Fleet Audit\n")
        print(f"- **Config Connector Status:** UNKNOWN (CRD listing failed: {crd_error})\n")
        print("**Config Connector Audit Verdict: Incomplete** (could not read: customresourcedefinitions).")
        print("CLEAN_AUDIT_STOP: NO — Report Config Connector state as unread, not healthy.\n")
        return 0

    total_kcc_crds = len(served_crd_scopes) + len(unserved_crds)
    if total_kcc_crds == 0:
        print("## Config Connector (KCC) Fleet Audit\n")
        print("- **kcc_installed:** `false`")
        print(
            "- **Config Connector Status:** NOT INSTALLED on this cluster "
            "(0 `*.cnrm.cloud.google.com` CustomResourceDefinitions found).\n"
        )
        print(
            "**Config Connector Audit Verdict: Clean / Not Installed** "
            "(Config Connector is not active on this cluster; 0 managed resources)."
        )
        print(
            "CLEAN_AUDIT_STOP: YES — Config Connector is not installed on this cluster; "
            "call kanban_complete immediately.\n"
        )
        return 0

    raw_items, unread_crds = collect_kcc_objects(
        served_crd_scopes,
        env,
        namespace=validated_ns,
        deadline=deadline,
        monotonic_fn=monotonic_fn,
        chunk_size=chunk_size,
    )

    evaluated = [evaluate_kcc_item(item) for item in raw_items]
    evaluated.sort(key=lambda r: (str(r["namespace"]), str(r["kind"]), str(r["name"])))

    operator_items = [r for r in evaluated if r["is_operator_cr"]]
    managed_items = [r for r in evaluated if not r["is_operator_cr"]]
    unhealthy_operator = [r for r in operator_items if not r["ready"]]
    unhealthy_managed = [r for r in managed_items if not r["ready"]]

    kind_counts: dict[str, int] = {}
    for row in managed_items:
        k = str(row["kind"])
        kind_counts[k] = kind_counts.get(k, 0) + 1

    scope_label = (
        f"namespace `{sanitize_cell(validated_ns)}`" if validated_ns is not None else "all namespaces"
    )
    sample_crds = [sanitize_cell(c) for c in sorted(served_crd_scopes)[:MAX_SAMPLE_CRDS_IN_SUMMARY]]
    crd_preview = ", ".join(sample_crds)
    if len(served_crd_scopes) > MAX_SAMPLE_CRDS_IN_SUMMARY:
        crd_preview += f", ... (+{len(served_crd_scopes) - MAX_SAMPLE_CRDS_IN_SUMMARY} more)"

    has_unread = bool(unread_crds) or bool(unserved_crds)
    managed_summary = (
        UNREAD_COUNT_TEXT
        if unread_crds and not managed_items
        else (
            f"{len(managed_items)} total across {scope_label} "
            f"({len(managed_items) - len(unhealthy_managed)} Ready/Synchronized, "
            f"{len(unhealthy_managed)} Unhealthy)"
            + (f"; {len(unread_crds)} CRD(s) unread" if unread_crds else "")
        )
    )
    operator_summary = (
        UNREAD_COUNT_TEXT
        if (
            (CONFIG_CONNECTOR_CRD in unread_crds or CONFIG_CONNECTOR_CONTEXT_CRD in unread_crds)
            and not operator_items
        )
        else (
            f"{len(operator_items)} total "
            f"({len(operator_items) - len(unhealthy_operator)} Healthy, "
            f"{len(unhealthy_operator)} Unhealthy)"
        )
    )

    print("## Config Connector (KCC) Fleet Audit\n")
    print("- **kcc_installed:** `true`")
    print(
        f"- **Config Connector CRDs:** {total_kcc_crds} `*.cnrm.cloud.google.com` CRDs "
        f"({len(served_crd_scopes)} served, {len(unserved_crds)} unserved/unestablished"
        + (f": {crd_preview}" if crd_preview else "")
        + ")"
    )
    print(f"- **Operator Objects (`ConfigConnector` / `ConfigConnectorContext`):** {operator_summary}")
    print(f"- **Managed KCC Resources:** {managed_summary}")
    if kind_counts:
        breakdown = ", ".join(f"{k}: {n}" for k, n in sorted(kind_counts.items()))
        print(f"- **Active Resource Kinds:** {breakdown}")
    print("")

    for crd_name in unserved_crds:
        print(f"- Unserved or unestablished KCC CRD `{sanitize_cell(crd_name)}`")
    for crd_name, reason in sorted(unread_crds.items()):
        print(f"- Could not read `{sanitize_cell(crd_name)}`: {reason}")

    is_clean = not has_unread and not unhealthy_operator and not unhealthy_managed
    if is_clean:
        print(
            "**Config Connector Audit Verdict: Clean & Synchronized** "
            f"({len(managed_items)} managed resources Ready, {len(operator_items)} operator objects Healthy, 0 errors)."
        )
        print(
            "CLEAN_AUDIT_STOP: YES — All Config Connector CRDs and custom resources were read "
            "and are synchronized; call kanban_complete immediately.\n"
        )
    elif has_unread:
        print(
            "**Config Connector Audit Verdict: Incomplete or Degraded** "
            f"({len(unread_crds)} unread CRDs, {len(unserved_crds)} unserved CRDs, "
            f"{len(unhealthy_operator) + len(unhealthy_managed)} unhealthy objects)."
        )
        print(
            "CLEAN_AUDIT_STOP: NO — Report unread CRDs as unread (never as 0 resources) "
            "and inspect any unhealthy Config Connector objects listed below.\n"
        )
    else:
        print(
            "**Config Connector Audit Verdict: Unhealthy Resources Detected** "
            f"({len(unhealthy_managed)} managed resources unready, "
            f"{len(unhealthy_operator)} operator objects unhealthy)."
        )
        print("CLEAN_AUDIT_STOP: NO — Inspect failed Config Connector resources listed below.\n")

    # Print synchronization failures BEFORE the object table so Hermes's 50 KB terminal
    # output cap never truncates error details on large KCC clusters.
    all_unhealthy = [*unhealthy_operator, *unhealthy_managed]
    if all_unhealthy:
        print("### Synchronization Failures & Error Messages")
        for row in all_unhealthy:
            detail = row["message"] or row["reason"]
            print(f"- `{row['namespace']}/{row['kind']}/{row['name']}`: {detail}")
        print("")

    if evaluated:
        unhealthy_rows = [row for row in evaluated if not row["ready"]]
        ready_rows = [row for row in evaluated if row["ready"]]
        rendered_rows = [*unhealthy_rows, *ready_rows[:MAX_RENDERED_READY_ROWS]]
        omitted_ready = max(0, len(ready_rows) - MAX_RENDERED_READY_ROWS)

        print("### Config Connector Objects")
        print(
            f"{'NAMESPACE':<{COL_NAMESPACE_WIDTH}} {'KIND':<{COL_KIND_WIDTH}} "
            f"{'NAME':<{COL_NAME_WIDTH}} {'READY':<{COL_READY_WIDTH}} {'REASON'}"
        )
        print("-" * TABLE_DIVIDER_WIDTH)
        for row in rendered_rows:
            ready_str = "True" if row["ready"] else "FALSE"
            print(
                f"{str(row['namespace']):<{COL_NAMESPACE_WIDTH}} "
                f"{str(row['kind']):<{COL_KIND_WIDTH}} "
                f"{str(row['name']):<{COL_NAME_WIDTH}} "
                f"{ready_str:<{COL_READY_WIDTH}} {row['reason']}"
            )
        if omitted_ready > 0:
            print(
                f"... (+{omitted_ready} Ready resources omitted to stay within terminal output cap)"
            )
        print("")
    elif not has_unread:
        print(f"No Config Connector custom resource instances declared across {scope_label}.\n")

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Batched parallel Config Connector (KCC) custom resource auditor.",
        allow_abbrev=False,
    )
    parser.add_argument("--kubeconfig", default=None, help="Path to kubeconfig file")
    scope_group = parser.add_mutually_exclusive_group()
    scope_group.add_argument(
        "-A",
        "--all-namespaces",
        action="store_true",
        help="Audit KCC custom resources across all namespaces (default when -n is omitted)",
    )
    scope_group.add_argument(
        "-n",
        "--namespace",
        default=None,
        help="Audit namespaced KCC custom resources in a single namespace",
    )
    args = parser.parse_args()
    try:
        validated_ns = validate_namespace(args.namespace)
    except ValueError as exc:
        parser.error(str(exc))
    sys.exit(audit_kcc(args.kubeconfig, namespace=validated_ns))


if __name__ == "__main__":
    main()
