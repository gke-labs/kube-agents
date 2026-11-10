#!/usr/bin/env python3
"""kcc_fleet_audit.py - Batched parallel Config Connector (KCC) custom resource auditor.

Discovers installed `*.cnrm.cloud.google.com` CustomResourceDefinitions in a single
compact `kubectl get crd -o custom-columns=... --no-headers` call (avoiding OpenAPI
schema bloat over the credential proxy's 8 MiB per-stream output cap, 4 MiB when
`CREDENTIAL_PROXY_MAX_OUTPUT_BYTES` is unset), partitions served established CRDs by
scope (`Cluster` vs `Namespaced`), and queries active custom resources in parallel
chunked `kubectl get ... -o jsonpath=...` batches (`BATCH_CHUNK_SIZE = 35`,
`MAX_PARALLEL_WORKERS = 4`) instead of issuing up to 233 sequential per-CRD RPCs or
streaming full `.spec` / `managedFields` payloads across the proxy. Automatically falls
back to per-CRD reads if a batch fails or hits credential-proxy output truncation, fails
closed (`CLEAN_AUDIT_STOP: NO`) whenever any CRD cannot be read, and summarizes KCC
operator health (`ConfigConnector`, `ConfigConnectorContext`) and managed resource
synchronization. Part of kube-agents #3142.
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

KCC_RECORD_SEP = "\x1e"
KCC_FIELD_SEP = "\x1f"
KCC_PROJECTED_FIELD_COUNT = 6
# Project only the 6 fields `evaluate_kcc_item` inspects (`.kind`, `.metadata.namespace`,
# `.metadata.name`, `.metadata.generation`, `.status`, `.spec.mode`) instead of `-o json`
# so `kubectl` drops `.spec`, `.metadata.managedFields`, and `last-applied-configuration`
# payloads before crossing the credential proxy's 8 MiB per-stream cap (and satisfies
# `scripts/check_cluster_reads.py` without allow-list entries). `kubectl`'s JSONPath printer
# marshals map/slice values (such as `.status`) as compact single-line JSON with control
# characters escaped (`\u001e` / `\u001f`), so ASCII RS/US framing stays lossless even when
# `.status` messages or `.spec.mode` values contain tabs, spaces, or newlines.
KCC_OBJECT_JSONPATH = (
    'jsonpath={range .items[*]}{.kind}{"\\x1f"}{.metadata.namespace}{"\\x1f"}'
    '{.metadata.name}{"\\x1f"}{.metadata.generation}{"\\x1f"}{.status}{"\\x1f"}'
    '{.spec.mode}{"\\x1e"}{end}'
)
NAMESPACE_CHECK_JSONPATH = "jsonpath={.metadata.name}"

# Batching tuning: 35 CRDs per `kubectl get crd1,crd2,...` reduces ~233 KCC CRDs to 8 RPCs
# across 4 worker threads (~3-5s total). MAX_PARALLEL_WORKERS matches collect.py's 4-slot
# cap under the credential proxy's default 832 MiB child memory budget
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
MAX_ID_CHARS = 80
MAX_REASON_CHARS = 64
MAX_SAMPLE_CRDS_IN_SUMMARY = 8
MAX_KUBECONFIG_BYTES = 1024 * 1024
# Bound each variable-length section so worst-case stdout stays well below Hermes's 50 KB
# terminal output cap even on large or all-unhealthy KCC clusters (~5 KB CRD issues +
# ~15 KB failure bullets + ~12 KB Ready object rows + ~2 KB summary < 50 KB). Unhealthy
# objects are rendered once in `### Synchronization Failures & Error Messages` and are not
# repeated in `### Config Connector Objects`.
MAX_RENDERED_CRD_ISSUES = 15
MAX_RENDERED_FAILURES = 50
MAX_RENDERED_READY_ROWS = 100
COL_NAMESPACE_WIDTH = 24
COL_KIND_WIDTH = 30
COL_NAME_WIDTH = 32
COL_READY_WIDTH = 10
TABLE_DIVIDER_WIDTH = 115

REDACTED_STOP_MARKER = "[REDACTED_STOP_MARKER]"
STOP_MARKER_RE = re.compile(r"clean[\W_]*audit[\W_]*stop", re.IGNORECASE)
RFC1123_DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
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
    if not RFC1123_DNS_LABEL_RE.fullmatch(namespace):
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
    except subprocess.TimeoutExpired:
        return 1, f"kubectl timed out after {timeout:.0f}s"
    except Exception as exc:
        return 1, str(exc)


def _run_bounded_cmd(
    args: list[str],
    env: dict[str, str],
    deadline: float,
    monotonic_fn: Callable[[], float],
) -> tuple[int, str]:
    """Compute remaining wall-clock budget at execution start rather than submission."""
    timeout = min(float(KUBECTL_TIMEOUT_SECONDS), deadline - monotonic_fn())
    return run_cmd(args, env, timeout=timeout)


def parse_projected_kcc_items(stdout: str) -> tuple[int, list[dict], str]:
    """Parse `KCC_OBJECT_JSONPATH` output into reconstructed item dicts.

    Note: never call unrestricted `str.strip()` on `stdout`, because Python classifies
    ASCII RS (`\\x1e`) and US (`\\x1f`) as whitespace (`str.isspace() == True`).
    """
    raw = stdout.strip("\r\n")
    if not raw:
        return 0, [], ""
    if not raw.endswith(KCC_RECORD_SEP):
        return 1, [], "incomplete projected kubectl output"

    items: list[dict] = []
    for record in raw[:-1].split(KCC_RECORD_SEP):
        fields = record.split(KCC_FIELD_SEP, KCC_PROJECTED_FIELD_COUNT - 1)
        if len(fields) != KCC_PROJECTED_FIELD_COUNT:
            return 1, [], "malformed projected kubectl output"
        kind_raw, ns_raw, name_raw, gen_raw, status_raw, spec_mode_raw = fields
        if not kind_raw or kind_raw == "null" or not name_raw or name_raw == "null":
            return 1, [], "projected kubectl item missing kind or metadata.name"

        metadata: dict[str, object] = {"name": name_raw}
        if ns_raw and ns_raw != "null":
            metadata["namespace"] = ns_raw
        if gen_raw and gen_raw != "null":
            try:
                metadata["generation"] = int(gen_raw)
            except ValueError:
                return 1, [], f"invalid metadata.generation {gen_raw!r} from kubectl"

        status_obj: dict[str, object] = {}
        if status_raw and status_raw != "null":
            try:
                parsed_status = json.loads(status_raw)
            except Exception as exc:
                return 1, [], f"invalid .status JSON from kubectl ({exc})"
            if parsed_status is not None:
                if not isinstance(parsed_status, dict):
                    return 1, [], "unexpected non-object .status JSON from kubectl"
                status_obj = parsed_status

        item: dict[str, object] = {
            "kind": kind_raw,
            "metadata": metadata,
            "status": status_obj,
        }
        if spec_mode_raw and spec_mode_raw != "null":
            item["spec"] = {"mode": spec_mode_raw}
        items.append(item)

    return 0, items, ""


def run_kubectl_projected(
    cmd: list[str],
    env: dict[str, str],
    timeout: float = KUBECTL_TIMEOUT_SECONDS,
) -> tuple[int, list[dict], str]:
    if timeout <= 0:
        return 1, [], BUDGET_EXHAUSTED_ERROR
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
        if res.returncode == 0:
            stderr_msg = res.stderr.strip()
            if PROXY_TRUNCATED_MARKER in stderr_msg.lower():
                return 1, [], stderr_msg
            parse_code, parsed_items, parse_err = parse_projected_kcc_items(res.stdout)
            if parse_code == 0:
                return 0, parsed_items, ""
            err_text = f"{stderr_msg} ({parse_err})" if stderr_msg else parse_err
            return 1, [], err_text
        return (
            res.returncode,
            [],
            res.stderr.strip() or f"kubectl exit {res.returncode} with no output",
        )
    except subprocess.TimeoutExpired:
        return 1, [], f"kubectl timed out after {timeout:.0f}s"
    except Exception as exc:
        return 1, [], str(exc)


def _run_bounded_kubectl_projected(
    cmd: list[str],
    env: dict[str, str],
    deadline: float,
    monotonic_fn: Callable[[], float],
) -> tuple[int, list[dict], str]:
    """Compute remaining wall-clock budget at worker execution start rather than queue submission."""
    timeout = min(float(KUBECTL_TIMEOUT_SECONDS), deadline - monotonic_fn())
    return run_kubectl_projected(cmd, env, timeout=timeout)


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


def _build_get_cmd(crd_names: list[str], scope: str, namespace: str | None) -> list[str]:
    joined = ",".join(crd_names)
    if scope == CRD_SCOPE_CLUSTER:
        return ["kubectl", "get", joined, "-o", KCC_OBJECT_JSONPATH]
    if namespace is not None:
        return ["kubectl", "get", joined, f"--namespace={namespace}", "-o", KCC_OBJECT_JSONPATH]
    return ["kubectl", "get", joined, "-A", "-o", KCC_OBJECT_JSONPATH]


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
                _run_bounded_kubectl_projected,
                _build_get_cmd(chunk, scope, namespace),
                env,
                deadline,
                monotonic_fn,
            ): (scope, chunk)
            for scope, chunk in batches
        }
        for future, (scope, chunk) in future_map.items():
            try:
                code, batch_items, err = future.result()
            except Exception as exc:
                code, batch_items, err = 1, [], str(exc)
            if code == 0:
                items.extend(batch_items)
            elif len(chunk) == 1:
                failed_singletons.append(
                    (chunk[0], sanitize_cell(err or "kubectl get failed", UNREAD_REASON_MAX_CHARS))
                )
            else:
                for crd_name in chunk:
                    retry_candidates.append((scope, crd_name))

    unread_crds: dict[str, str] = dict(failed_singletons)
    if retry_candidates:
        retry_workers = min(MAX_PARALLEL_WORKERS, len(retry_candidates))
        with ThreadPoolExecutor(max_workers=retry_workers) as executor:
            retry_futures = {
                executor.submit(
                    _run_bounded_kubectl_projected,
                    _build_get_cmd([crd_name], scope, namespace),
                    env,
                    deadline,
                    monotonic_fn,
                ): crd_name
                for scope, crd_name in retry_candidates
            }
            for fut, crd_name in retry_futures.items():
                try:
                    code, single_items, err = fut.result()
                except Exception as exc:
                    code, single_items, err = 1, [], str(exc)
                if code == 0:
                    items.extend(single_items)
                else:
                    unread_crds[crd_name] = sanitize_cell(
                        err or "kubectl get failed", UNREAD_REASON_MAX_CHARS
                    )

    return items, unread_crds


def evaluate_kcc_item(item: dict) -> dict[str, object]:
    """Classify one KCC operator CR or managed resource CR into Ready/Healthy vs Unhealthy."""
    kind = sanitize_cell(item.get("kind") or "Unknown", MAX_ID_CHARS)
    meta = item.get("metadata") or {}
    ns_raw = meta.get("namespace")
    namespace = sanitize_cell(ns_raw, MAX_ID_CHARS) if ns_raw else "<cluster>"
    name = sanitize_cell(meta.get("name") or "", MAX_ID_CHARS)
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
        elif (
            healthy_flag is None
            and not errors_list
            and ready_cond is not None
            and ready_cond.get("status") == "True"
        ):
            is_ready = True
            reason = sanitize_cell(ready_cond.get("reason") or "Ready")
            message = sanitize_cell(ready_cond.get("message") or "")
        else:
            is_ready = False
            reason = (
                "OperatorUnhealthy"
                if healthy_flag is False
                else ("OperatorErrors" if errors_list else "StatusPending")
            )
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
        "reason": sanitize_cell(reason, MAX_REASON_CHARS),
        "message": sanitize_cell(message, MAX_CELL_CHARS),
    }


def _read_kubeconfig_current_context(env: dict[str, str]) -> str | None:
    """Read `current-context` from the active kubeconfig file if present on disk."""
    kc_env = env.get("KUBECONFIG") or ""
    candidates = [entry.strip() for entry in kc_env.split(os.pathsep) if entry.strip()]
    if not candidates:
        candidates = ["/opt/data/.kubeconfig"]
    for raw_path in candidates:
        path = Path(raw_path)
        try:
            if not path.is_file() or path.stat().st_size > MAX_KUBECONFIG_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("current-context:"):
                val = stripped.partition(":")[2].strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1].strip()
                if val:
                    return sanitize_cell(val, MAX_CELL_CHARS)
    return None


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
    target_context = _read_kubeconfig_current_context(env)

    phase1_timeout = min(float(KUBECTL_TIMEOUT_SECONDS), deadline - monotonic_fn())
    served_crd_scopes, unserved_crds, crd_error = discover_kcc_crds(env, timeout=phase1_timeout)
    if served_crd_scopes is None:
        print("## Config Connector (KCC) Fleet Audit\n")
        if target_context:
            print(f"- **Target Context:** `{target_context}`")
        print(f"- **Config Connector Status:** UNKNOWN (CRD listing failed: {crd_error})\n")
        print("**Config Connector Audit Verdict: Incomplete** (could not read: customresourcedefinitions).")
        print("CLEAN_AUDIT_STOP: NO — Report Config Connector state as unread, not healthy.\n")
        return 0

    total_kcc_crds = len(served_crd_scopes) + len(unserved_crds)
    if total_kcc_crds == 0:
        print("## Config Connector (KCC) Fleet Audit\n")
        if target_context:
            print(f"- **Target Context:** `{target_context}`")
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

    # `kubectl get <crds> --namespace=<ns>` returns an empty List with exit 0 even when `<ns>`
    # does not exist on the cluster. When `-n` was passed and zero namespaced objects came back,
    # verify the namespace exists so a mistyped or deleted namespace never prints CLEAN_AUDIT_STOP: YES.
    namespace_error = ""
    if validated_ns is not None and not any(
        isinstance(item.get("metadata"), dict) and item["metadata"].get("namespace")
        for item in raw_items
    ):
        ns_code, ns_out = _run_bounded_cmd(
            ["kubectl", "get", "namespace", validated_ns, "-o", NAMESPACE_CHECK_JSONPATH],
            env,
            deadline,
            monotonic_fn,
        )
        if ns_code != 0:
            namespace_error = sanitize_cell(
                ns_out or "kubectl get namespace failed", UNREAD_REASON_MAX_CHARS
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

    has_unread = bool(unread_crds) or bool(unserved_crds) or bool(namespace_error)
    if namespace_error:
        managed_summary = (
            f"unread (namespace `{sanitize_cell(validated_ns)}` not found or unreadable; "
            "see the verdict below)"
        )
    elif unread_crds and not managed_items:
        managed_summary = UNREAD_COUNT_TEXT
    else:
        managed_summary = (
            f"{len(managed_items)} total across {scope_label} "
            f"({len(managed_items) - len(unhealthy_managed)} Ready/Synchronized, "
            f"{len(unhealthy_managed)} Unhealthy)"
            + (f"; {len(unread_crds)} CRD(s) unread" if unread_crds else "")
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
    if target_context:
        print(f"- **Target Context:** `{target_context}`")
    print("- **kcc_installed:** `true`")
    crd_counts_clause = (
        f"{len(served_crd_scopes)} served: {crd_preview}; {len(unserved_crds)} unserved/unestablished"
        if crd_preview
        else f"{len(served_crd_scopes)} served, {len(unserved_crds)} unserved/unestablished"
    )
    print(
        f"- **Config Connector CRDs:** {total_kcc_crds} `*.cnrm.cloud.google.com` CRDs "
        f"({crd_counts_clause})"
    )
    print(f"- **Operator Objects (`ConfigConnector` / `ConfigConnectorContext`):** {operator_summary}")
    print(f"- **Managed KCC Resources:** {managed_summary}")
    if kind_counts:
        breakdown = ", ".join(f"{k}: {n}" for k, n in sorted(kind_counts.items()))
        print(f"- **Active Resource Kinds:** {breakdown}")
    print("")

    if namespace_error:
        print(f"- Could not verify namespace `{sanitize_cell(validated_ns)}`: {namespace_error}")
    for crd_name in unserved_crds[:MAX_RENDERED_CRD_ISSUES]:
        print(f"- Unserved or unestablished KCC CRD `{sanitize_cell(crd_name)}`")
    if len(unserved_crds) > MAX_RENDERED_CRD_ISSUES:
        print(
            f"... (+{len(unserved_crds) - MAX_RENDERED_CRD_ISSUES} unserved/unestablished KCC CRDs omitted)"
        )
    sorted_unread = sorted(unread_crds.items())
    for crd_name, reason in sorted_unread[:MAX_RENDERED_CRD_ISSUES]:
        print(f"- Could not read `{sanitize_cell(crd_name)}`: {reason}")
    if len(sorted_unread) > MAX_RENDERED_CRD_ISSUES:
        print(f"... (+{len(sorted_unread) - MAX_RENDERED_CRD_ISSUES} unread KCC CRDs omitted)")

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
        ns_clause = (
            f"namespace `{sanitize_cell(validated_ns)}` unverified, " if namespace_error else ""
        )
        print(
            "**Config Connector Audit Verdict: Incomplete or Degraded** "
            f"({ns_clause}{len(unread_crds)} unread CRDs, {len(unserved_crds)} unserved CRDs, "
            f"{len(unhealthy_operator) + len(unhealthy_managed)} unhealthy objects)."
        )
        print(
            "CLEAN_AUDIT_STOP: NO — Report any unverified namespace, unread CRDs, or "
            "unserved/unestablished CRDs (never as 0 resources or healthy) and inspect "
            "any unhealthy Config Connector objects listed below.\n"
        )
    else:
        print(
            "**Config Connector Audit Verdict: Unhealthy Resources Detected** "
            f"({len(unhealthy_managed)} managed resources unready, "
            f"{len(unhealthy_operator)} operator objects unhealthy)."
        )
        print("CLEAN_AUDIT_STOP: NO — Inspect failed Config Connector resources listed below.\n")

    # Print bounded synchronization failures BEFORE the Ready-only object table so Hermes's
    # 50 KB terminal output cap is never exceeded even on large or all-unhealthy KCC clusters.
    all_unhealthy = [*unhealthy_operator, *unhealthy_managed]
    if all_unhealthy:
        print("### Synchronization Failures & Error Messages")
        for row in all_unhealthy[:MAX_RENDERED_FAILURES]:
            reason = str(row["reason"])
            msg = str(row["message"])
            detail = f"{reason} — {msg}" if msg and reason and reason not in msg else (msg or reason)
            print(f"- `{row['namespace']}/{row['kind']}/{row['name']}`: {detail}")
        omitted_failures = max(0, len(all_unhealthy) - MAX_RENDERED_FAILURES)
        if omitted_failures > 0:
            print(
                f"... (+{omitted_failures} unhealthy resources omitted to stay within terminal output cap; "
                "re-run with -n <namespace> to inspect a single namespace)"
            )
        print("")

    if evaluated:
        ready_rows = [row for row in evaluated if row["ready"]]
        rendered_rows = ready_rows[:MAX_RENDERED_READY_ROWS]
        omitted_ready = max(0, len(ready_rows) - MAX_RENDERED_READY_ROWS)

        print("### Config Connector Objects")
        if rendered_rows:
            print(
                f"{'NAMESPACE':<{COL_NAMESPACE_WIDTH}} {'KIND':<{COL_KIND_WIDTH}} "
                f"{'NAME':<{COL_NAME_WIDTH}} {'READY':<{COL_READY_WIDTH}} {'REASON'}"
            )
            print("-" * TABLE_DIVIDER_WIDTH)
            for row in rendered_rows:
                print(
                    f"{str(row['namespace']):<{COL_NAMESPACE_WIDTH}} "
                    f"{str(row['kind']):<{COL_KIND_WIDTH}} "
                    f"{str(row['name']):<{COL_NAME_WIDTH}} "
                    f"{'True':<{COL_READY_WIDTH}} {row['reason']}"
                )
            if omitted_ready > 0:
                print(
                    f"... (+{omitted_ready} Ready resources omitted to stay within terminal output cap)"
                )
        else:
            print("(0 Ready resources; all unhealthy resources are listed above)")
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
