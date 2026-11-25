"""PR-level file triage, single-pass manifest classification, and blast-radius aggregation."""

from __future__ import annotations

import math
import posixpath
from typing import Any

from . import k8s_rules as _k8s_rules
from .k8s_rules import (
    SYSTEM_NAMESPACES,
    WORKLOAD_KINDS,
    _is_production_labels,
    _is_production_path,
    _scan_value_for_secrets,
    evaluate_resource,
    is_cluster_scoped_kind,
    parse_cpu_millicores,
    parse_memory_bytes,
)
from .parser import MAX_FILE_BYTES, MAX_TOTAL_BYTES, ParseOutcome, ParsedResource, _strict_equal, parse_manifest_bytes
from .types import (
    ACTION_ADD,
    ACTION_DELETE,
    ACTION_MODIFY,
    BLAST_CRITICAL,
    BLAST_HIGH,
    BLAST_LOW,
    BLAST_MEDIUM,
    BLAST_UNKNOWN,
    CHANGE_ADDED,
    CHANGE_DELETED,
    CHANGE_RENAMED,
    DIM_BLAST_RADIUS,
    DIM_DISRUPTIVENESS,
    DIM_INPUT_INTEGRITY,
    DIM_REVERTABILITY,
    DIM_SECURITY,
    DISRUPT_DESTRUCTIVE,
    DISRUPT_DISRUPTIVE,
    DISRUPT_NONE,
    DISRUPT_UNKNOWN,
    REVERT_DIFFICULT,
    REVERT_EASY,
    REVERT_IRREVERSIBLE,
    REVERT_MODERATE,
    REVERT_UNKNOWN,
    RISK_CRITICAL,
    RISK_HIGH,
    RISK_LOW,
    RISK_MEDIUM,
    RISK_UNKNOWN,
    ClassificationResult,
    FileChange,
    FileVerdict,
    PRStats,
    Reason,
    ResourceVerdict,
    combine_blast_radius,
    combine_disruptiveness,
    combine_revertability,
    combine_risk,
    escalate_risk,
    unknown_result,
)

# PR-wide blast radius thresholds
MAX_LOW_OBJECTS = 5
MAX_LOW_FILES = 5
MAX_LOW_NAMESPACES = 1
HIGH_OBJECT_THRESHOLD = 50
CRITICAL_OBJECT_THRESHOLD = 100
HIGH_NAMESPACE_THRESHOLD = 5
MAX_LOW_COMPOUND_CPU_M = 4000.0
MAX_LOW_COMPOUND_MEM_B = 8 * 1024**3
HIGH_COMPOUND_CPU_M = 64000.0
HIGH_COMPOUND_MEM_B = 128 * 1024**3
CRITICAL_COMPOUND_CPU_M = 256000.0
CRITICAL_COMPOUND_MEM_B = 512 * 1024**3

# Unicode BiDi / Trojan-Source overrides and zero-width characters disallowed in file paths
_PATH_HOSTILE_UNICODE = frozenset(
    {
        "\ufeff",
        "\u200b",
        "\u200c",
        "\u200d",
        "\u200e",
        "\u200f",
        "\u061c",
        "\u202a",
        "\u202b",
        "\u202c",
        "\u202d",
        "\u202e",
        "\u2066",
        "\u2067",
        "\u2068",
        "\u2069",
    }
)

# Critical governance / CI / agent control-plane paths (all checked case-insensitively)
_CRITICAL_DIR_PREFIXES = (
    ".kube-agents/",
    ".git/",
    ".githooks/",
    ".husky/",
    ".github/workflows/",
    ".github/actions/",
    ".github/instructions/",
    ".github/agents/",
    ".github/prompts/",
    ".gitlab/",
    ".gitea/",
    ".forgejo/",
    ".woodpecker/",
    ".tekton/",
    ".circleci/",
    ".buildkite/",
    ".claude/",
    ".cursor/",
    ".gemini/",
    ".codex/",
    ".agents/",
    ".windsurf/",
    ".cline/",
)

_CRITICAL_EXACT_PATHS = frozenset(
    {
        ".github/dependabot.yml",
        ".github/dependabot.yaml",
        ".github/copilot-instructions.md",
        ".gitlab-ci.yml",
        ".gitlab-ci.yaml",
        ".woodpecker.yml",
        ".woodpecker.yaml",
        ".drone.yml",
        ".drone.yaml",
        ".travis.yml",
        ".travis.yaml",
        "azure-pipelines.yml",
        "azure-pipelines.yaml",
        "bitbucket-pipelines.yml",
        "bitbucket-pipelines.yaml",
    }
)

_CRITICAL_BASENAMES = frozenset(
    {
        "codeowners",
        "owners",
        "owners_aliases",
        "dependabot.yml",
        "dependabot.yaml",
        "copilot-instructions.md",
        ".sops.yaml",
        ".sops.yml",
        ".sops.json",
        ".gitmodules",
        ".gitattributes",
        ".pre-commit-config.yaml",
        ".pre-commit-config.yml",
        "lefthook.yml",
        "lefthook.yaml",
        ".lefthook.yml",
        ".lefthook.yaml",
        "renovate.json",
        "renovate.json5",
        ".renovaterc",
        ".renovaterc.json",
        ".renovaterc.json5",
        ".renovaterc.yml",
        ".renovaterc.yaml",
        "agents.md",
        "claude.md",
        "gemini.md",
        "skill.md",
        ".cursorrules",
        ".windsurfrules",
        ".clinerules",
        "cloudbuild.yaml",
        "cloudbuild.yml",
    }
)

# High-risk supply-chain / container / dependency build files
_SUPPLY_CHAIN_BASENAMES = frozenset(
    {
        "dockerfile",
        "containerfile",
        "makefile",
        "justfile",
        "tiltfile",
        "earthfile",
        "taskfile.yml",
        "taskfile.yaml",
        "skaffold.yaml",
        "skaffold.yml",
        "ko.yaml",
        ".ko.yaml",
        "goreleaser.yaml",
        "goreleaser.yml",
        ".goreleaser.yaml",
        ".goreleaser.yml",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yml",
        "compose.yaml",
        "go.mod",
        "go.sum",
        "go.work",
        "go.work.sum",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "pnpm-lock.yaml",
        "pnpm-workspace.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "requirements.txt",
        "constraints.txt",
        "pyproject.toml",
        "poetry.lock",
        "uv.lock",
        "pdm.lock",
        "pipfile",
        "pipfile.lock",
        "setup.py",
        "setup.cfg",
        "cargo.toml",
        "cargo.lock",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "settings.gradle",
        "settings.gradle.kts",
        "gradle.properties",
        "gemfile",
        "gemfile.lock",
        "composer.json",
        "composer.lock",
        "flake.nix",
        "flake.lock",
        "build.bazel",
        "workspace.bazel",
        "module.bazel",
    }
)

# Unrendered Helm / Kustomize / template packaging files -> `unknown` floor
_UNRENDERED_PACKAGE_BASENAMES = frozenset(
    {
        "kustomization.yaml",
        "kustomization.yml",
        "kustomization",
        "chart.yaml",
        "chart.yml",
        "chart.lock",
        "helmfile.yaml",
        "helmfile.yml",
        "helmfile.yaml.gotmpl",
        "kptfile",
        "values.yaml",
        "values.yml",
        "values.schema.json",
    }
)

_UNRENDERED_PACKAGE_EXTENSIONS = frozenset(
    {
        ".gotmpl",
        ".tmpl",
        ".tpl",
        ".j2",
        ".jinja",
        ".jinja2",
        ".jsonnet",
        ".libsonnet",
        ".cue",
    }
)

# GitOps sync directories where non-manifest files (like `.md`/`.txt`) could be
# consumed by Kustomize configMapGenerator/secretGenerator `files:`
_SYNC_ROOT_PREFIXES = (
    "clusters/",
    "fleet/",
    "gcp/",
    "policy/",
    "argocd/",
    "bootstrap/",
    "apps/",
    "manifests/",
    "infrastructure/",
    "infra/",
    "tenants/",
    "environments/",
    "envs/",
    "deploy/",
    "deployments/",
    "k8s/",
    "kubernetes/",
    "gitops/",
    "flux/",
    "configsync/",
    "namespaces/",
    "workloads/",
    "platform/",
    "bases/",
    "base/",
)

_DOC_EXTENSIONS = frozenset({".md", ".rst", ".adoc"})
_PURE_DOC_EXACT_BASENAMES = frozenset(
    {
        "license",
        "licence",
        "license.txt",
        "licence.txt",
        "license.md",
        "licence.md",
        "notice",
        "notice.txt",
        "notice.md",
        "copying",
        "copying.txt",
        "copying.md",
    }
)
_MANIFEST_EXTENSIONS = frozenset({".yaml", ".yml", ".json"})
_OVERLAY_SEGMENTS = frozenset({"patches", "overlays", "templates"})
_TERRAFORM_EXTENSIONS = frozenset({".tf", ".tfvars", ".hcl", ".tofu"})
_TERRAFORM_COMPOUND_SUFFIXES = (".tf.json", ".tofu.json", ".tfvars.json")

_MODE_SYMLINK = "120000"
_MODE_GITLINK = "160000"
_MODE_EXEC = "100755"
_KNOWN_REGULAR_MODES = frozenset({"", "100644", "100664", _MODE_EXEC})
_KNOWN_ALL_MODES = _KNOWN_REGULAR_MODES | {_MODE_SYMLINK, _MODE_GITLINK}


def _normalize_path(raw: str) -> tuple[str, bool]:
    """Normalize and strictly validate a repo-relative POSIX path; returns `(normalized, ok)`."""
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        return str(raw) if raw is not None else "", False
    if raw.startswith("/") or raw.endswith("/"):
        return raw, False
    for ch in raw:
        codepoint = ord(ch)
        if codepoint < 0x20 or codepoint == 0x7F or ch in _PATH_HOSTILE_UNICODE:
            return raw, False
    parts = raw.split("/")
    for seg in parts:
        if not seg or seg in (".", ".."):
            return raw, False
        low_seg = seg.lower()
        if low_seg == ".git" or low_seg.startswith("git~"):
            return raw, False
    norm = posixpath.normpath(raw)
    if norm in (".", "..") or norm.startswith("../"):
        return norm, False
    return norm, True


def _cluster_from_path(norm_path: str) -> str:
    parts = norm_path.split("/")
    if len(parts) >= 3 and parts[0].lower() == "clusters" and parts[1]:
        return parts[1]
    return ""


def _is_critical_governance_path(norm_path: str) -> bool:
    low = norm_path.replace("\\", "/").lower().strip("/")
    if not low:
        return False
    parts = [p for p in low.split("/") if p and p not in (".", "..")]
    if not parts:
        return False
    canonical_low = "/".join(parts)
    base = parts[-1]
    if canonical_low in _CRITICAL_EXACT_PATHS:
        return True
    if any(canonical_low.startswith(pfx) or f"/{pfx}" in canonical_low for pfx in _CRITICAL_DIR_PREFIXES):
        return True
    if any(p == ".git" or p.startswith("git~") for p in parts):
        return True
    if base in _CRITICAL_BASENAMES:
        return True
    if base.startswith(("jenkinsfile", "cloudbuild.", ".gitlab-ci.")):
        return True
    return False


def _is_supply_chain_path(norm_path: str) -> bool:
    base = posixpath.basename(norm_path.lower())
    if base in _SUPPLY_CHAIN_BASENAMES:
        return True
    if base.startswith(("dockerfile.", "containerfile.", "requirements-", "requirements_")):
        return True
    if base.endswith((".dockerfile", ".containerfile")):
        return True
    return False


def _is_unrendered_package_path(norm_path: str) -> bool:
    low = norm_path.lower()
    base = posixpath.basename(low)
    if base in _UNRENDERED_PACKAGE_BASENAMES:
        return True
    if base.startswith(("values.", "values-", "values_")) and base.endswith((".yaml", ".yml")):
        return True
    if base.endswith(("-values.yaml", "-values.yml", "_values.yaml", "_values.yml", ".values.yaml", ".values.yml")):
        return True
    _, ext = posixpath.splitext(base)
    if ext in _UNRENDERED_PACKAGE_EXTENSIONS:
        return True
    parts = low.split("/")[:-1]
    if any(seg in _OVERLAY_SEGMENTS for seg in parts):
        return True
    return False


def _is_terraform_path(norm_path: str) -> bool:
    low = norm_path.lower()
    if low.endswith(_TERRAFORM_COMPOUND_SUFFIXES):
        return True
    _, ext = posixpath.splitext(low)
    return ext in _TERRAFORM_EXTENSIONS


def _is_pure_doc_path(norm_path: str) -> bool:
    low = norm_path.lower()
    if any(low.startswith(pfx) for pfx in _SYNC_ROOT_PREFIXES):
        return False
    if _is_unrendered_package_path(norm_path):
        return False
    if low.startswith(".") or "/." in low:
        return False
    base = posixpath.basename(low)
    if base in _PURE_DOC_EXACT_BASENAMES:
        return True
    _, ext = posixpath.splitext(base)
    return ext in _DOC_EXTENSIONS


def _path_bucket(norm_path: str) -> str:
    if _is_critical_governance_path(norm_path):
        return "governance"
    if _is_supply_chain_path(norm_path):
        return "supply_chain"
    if _is_terraform_path(norm_path):
        return "terraform"
    if _is_unrendered_package_path(norm_path):
        return "unrendered_package"
    if _is_pure_doc_path(norm_path):
        return "documentation"
    _, ext = posixpath.splitext(norm_path.lower())
    if ext in _MANIFEST_EXTENSIONS:
        return "manifest"
    return "other"


def _sync_root(norm_path: str) -> str:
    """Return canonical directory sync-root identifier (excluding filename)."""
    low = norm_path.lower()
    for pfx in _SYNC_ROOT_PREFIXES:
        if low.startswith(pfx):
            dir_parts = low.split("/")[:-1]
            return "/".join(dir_parts[:2]) if len(dir_parts) >= 2 else pfx.rstrip("/")
    return ""


def _sync_scope_key(norm_path: str) -> str:
    """Return canonical reconciliation scope for cross-file duplicate GVKNN detection."""
    cluster = _cluster_from_path(norm_path)
    if cluster:
        return f"cluster:{cluster.lower()}"
    sr = _sync_root(norm_path)
    if sr:
        return f"sync:{sr}"
    return f"dir:{posixpath.dirname(norm_path.lower())}"


def _expand_cross_boundary_renames(changes: list[FileChange]) -> list[FileChange]:
    """Split renames crossing directory, category, cluster, sync-tree, invalid-path, or prod boundaries."""
    expanded: list[FileChange] = []
    for fc in changes:
        if not fc.old_path or fc.old_path == fc.path:
            expanded.append(fc)
            continue
        new_norm, new_ok = _normalize_path(fc.path)
        old_norm, old_ok = _normalize_path(fc.old_path)
        cross_boundary = not new_ok or not old_ok
        if not cross_boundary:
            old_sig = (
                posixpath.dirname(old_norm.lower()),
                _path_bucket(old_norm),
                _cluster_from_path(old_norm),
                _sync_root(old_norm),
                _is_production_path(old_norm),
            )
            new_sig = (
                posixpath.dirname(new_norm.lower()),
                _path_bucket(new_norm),
                _cluster_from_path(new_norm),
                _sync_root(new_norm),
                _is_production_path(new_norm),
            )
            cross_boundary = old_sig != new_sig
        if cross_boundary:
            expanded.append(
                FileChange(
                    path=fc.old_path,
                    change_type=CHANGE_DELETED,
                    before=fc.before,
                    after=None,
                    before_mode=fc.before_mode,
                    after_mode="",
                    is_binary=fc.is_binary,
                    oversized=fc.oversized,
                    integrity_error=fc.integrity_error,
                )
            )
            expanded.append(
                FileChange(
                    path=fc.path,
                    change_type=CHANGE_ADDED,
                    before=None,
                    after=fc.after,
                    before_mode="",
                    after_mode=fc.after_mode,
                    is_binary=fc.is_binary,
                    oversized=fc.oversized,
                    integrity_error=fc.integrity_error,
                )
            )
        else:
            expanded.append(fc)
    return expanded


def _merge_ns_labels_monotonic(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Commutatively merge Namespace labels so production protection is sticky and order-independent."""
    merged = dict(existing)
    for k, v in incoming.items():
        if k not in merged:
            merged[k] = v
            continue
        inc_prod = _is_production_labels({k: v})
        cur_prod = _is_production_labels({k: merged[k]})
        if inc_prod and not cur_prod:
            merged[k] = v
        elif inc_prod == cur_prod and str(v) > str(merged[k]):
            merged[k] = v
    return merged


def _has_explicit_metadata_name(res: ParsedResource) -> bool:
    meta = res.obj.get("metadata")
    if not isinstance(meta, dict):
        return False
    raw_name = meta.get("name")
    return isinstance(raw_name, str) and bool(raw_name.strip())


def _canonical_identity_key(res: ParsedResource) -> tuple[str, str, str, str]:
    """Return canonical `(group, kind, namespace, name)` with cluster-scope normalization."""
    ns = "" if is_cluster_scoped_kind(res.ref.group, res.ref.kind) else res.ref.namespace
    if not _has_explicit_metadata_name(res):
        return (res.ref.group, res.ref.kind, ns, f"generateName:{res.ref.name}")
    return (res.ref.group, res.ref.kind, ns, res.ref.name)


def _group_resource_lists(
    resources: tuple[ParsedResource, ...],
) -> tuple[dict[tuple[str, str, str, str], list[ParsedResource]], bool]:
    """Group resources by canonical identity key without dropping subsequent resources on duplicate."""
    out: dict[tuple[str, str, str, str], list[ParsedResource]] = {}
    had_dup = False
    for res in resources:
        key = _canonical_identity_key(res)
        if key in out:
            had_dup = True
            out[key].append(res)
        else:
            out[key] = [res]
    return out, had_dup


def _effective_container_requests(ctr: Any) -> tuple[float, float]:
    """Return effective `(cpu_millicores, memory_bytes)` pod scheduling request for one container.

    Follows Kubernetes `SetObjectDefaults_Pod`: when `requests.cpu` or `requests.memory`
    is omitted, Kubernetes defaults the scheduling request to the container's `limits`.
    """
    if not isinstance(ctr, dict):
        return 0.0, 0.0
    res = ctr.get("resources")
    if not isinstance(res, dict):
        return 0.0, 0.0
    req = res.get("requests") if isinstance(res.get("requests"), dict) else {}
    lim = res.get("limits") if isinstance(res.get("limits"), dict) else {}

    raw_cpu = req.get("cpu") if req.get("cpu") is not None else lim.get("cpu")
    raw_mem = req.get("memory") if req.get("memory") is not None else lim.get("memory")
    cpu = parse_cpu_millicores(raw_cpu)
    mem = parse_memory_bytes(raw_mem)
    return (cpu if cpu is not None and math.isfinite(cpu) else 0.0), (
        mem if mem is not None and math.isfinite(mem) else 0.0
    )


def _workload_pod_multiplier(kind: str, obj: dict[str, Any]) -> int:
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return 1
    if kind in ("Deployment", "StatefulSet", "ReplicaSet"):
        rep = spec.get("replicas", 1)
        if rep is None:
            return 1
        if isinstance(rep, int) and not isinstance(rep, bool) and rep >= 0:
            return rep
        return 1
    if kind == "Job":
        if spec.get("suspend") is True:
            return 0
        par = spec.get("parallelism", 1)
        if isinstance(par, int) and not isinstance(par, bool) and par >= 0:
            return par
        return 1
    if kind == "CronJob":
        if spec.get("suspend") is True:
            return 0
        jt = spec.get("jobTemplate")
        j_spec = jt.get("spec") if isinstance(jt, dict) else None
        par = j_spec.get("parallelism", 1) if isinstance(j_spec, dict) else 1
        if isinstance(par, int) and not isinstance(par, bool) and par >= 0:
            return par
        return 1
    return 1


def _extract_workload_pod_spec(kind: str, obj: dict[str, Any]) -> dict[str, Any] | None:
    spec = obj.get("spec")
    if not isinstance(spec, dict):
        return None
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        jt = spec.get("jobTemplate")
        spec = jt.get("spec") if isinstance(jt, dict) else None
        if not isinstance(spec, dict):
            return None
    tmpl = spec.get("template")
    if not isinstance(tmpl, dict):
        return None
    ps = tmpl.get("spec")
    return ps if isinstance(ps, dict) else None


def _workload_scheduling_totals(res: ParsedResource | None) -> tuple[float, float]:
    """Compute Kubernetes effective `(cpu_m, mem_bytes)` scheduled across replicas for a workload."""
    if res is None or (res.ref.group, res.ref.kind) not in WORKLOAD_KINDS:
        return 0.0, 0.0
    ps = _extract_workload_pod_spec(res.ref.kind, res.obj)
    if ps is None:
        return 0.0, 0.0
    mult = _workload_pod_multiplier(res.ref.kind, res.obj)
    if mult <= 0:
        return 0.0, 0.0

    app_cpu = 0.0
    app_mem = 0.0
    ctrs = ps.get("containers")
    if isinstance(ctrs, list):
        for c in ctrs:
            c_cpu, c_mem = _effective_container_requests(c)
            app_cpu += c_cpu
            app_mem += c_mem

    # Per K8s effectivePodRequest (including KEP-753 restartPolicy=Always sidecars):
    # sequential initContainers run one at a time alongside already-started sidecar
    # initContainers, while steady-state runs all sidecar initContainers + containers.
    sidecar_cpu = 0.0
    sidecar_mem = 0.0
    peak_init_cpu = 0.0
    peak_init_mem = 0.0
    inits = ps.get("initContainers")
    if isinstance(inits, list):
        for ic in inits:
            ic_cpu, ic_mem = _effective_container_requests(ic)
            if isinstance(ic, dict) and ic.get("restartPolicy") == "Always":
                sidecar_cpu += ic_cpu
                sidecar_mem += ic_mem
                peak_init_cpu = max(peak_init_cpu, sidecar_cpu)
                peak_init_mem = max(peak_init_mem, sidecar_mem)
            else:
                peak_init_cpu = max(peak_init_cpu, sidecar_cpu + ic_cpu)
                peak_init_mem = max(peak_init_mem, sidecar_mem + ic_mem)

    pod_cpu = max(peak_init_cpu, sidecar_cpu + app_cpu)
    pod_mem = max(peak_init_mem, sidecar_mem + app_mem)
    tot_cpu = pod_cpu * mult
    tot_mem = pod_mem * mult
    return tot_cpu, tot_mem


def _safe_add_delta(cur: float, delta: float) -> tuple[float, bool]:
    """Accumulate finite float delta, returning `(new_total, overflowed_positive)`."""
    if not math.isfinite(delta):
        return cur, delta > 0
    nxt = cur + delta
    if not math.isfinite(nxt):
        return (cur, nxt > 0 or delta > 0)
    return nxt, False


def _scan_raw_blob_secrets(raw: bytes | None, file_path: str, *, is_binary: bool) -> list[Reason]:
    """Scan non-binary raw file bytes for high-confidence leaked credentials."""
    if not raw or is_binary or len(raw) > MAX_FILE_BYTES:
        return []
    out: list[Reason] = []
    seen_labels: set[str] = set()
    for _, pat_name in _scan_value_for_secrets(raw):
        if pat_name not in seen_labels:
            seen_labels.add(pat_name)
            out.append(
                Reason(
                    code="PLAINTEXT_SECRET_LEAK",
                    dimension=DIM_SECURITY,
                    severity=RISK_CRITICAL,
                    message=f"plaintext {pat_name} detected in {file_path!r}",
                    file=file_path,
                )
            )
    return out


def _resource_meta_labels(res: ParsedResource) -> dict[str, Any]:
    meta = res.obj.get("metadata")
    if not isinstance(meta, dict):
        return {}
    lbls = meta.get("labels")
    return lbls if isinstance(lbls, dict) else {}


def _is_kube_agents_group(group: str) -> bool:
    """Match kube-agents CR API groups (`kubeagents.x-k8s.io`, `*.kube-agents.x-k8s.io`, etc.)."""
    fn = getattr(_k8s_rules, "is_kube_agents_group", None)
    if callable(fn):
        return bool(fn(group))
    low = group.lower()
    return (
        low in ("kubeagents.x-k8s.io", "kube-agents.x-k8s.io")
        or low.endswith((".kubeagents.x-k8s.io", ".kube-agents.x-k8s.io"))
    )


def classify_changes(changes: list[FileChange]) -> ClassificationResult:
    """Deterministically classify a list of changed PR files across all 4 risk axes."""
    if not changes:
        return unknown_result("EMPTY_PR_DIFF", "pull request contains no changed files")

    raw_file_count = len(changes)
    expanded = _expand_cross_boundary_renames(changes)

    # Sort changes deterministically by path
    ordered = sorted(expanded, key=lambda c: (c.path, c.old_path, c.change_type))

    # Pass 1: normalize paths, detect duplicate active file paths, enforce cumulative
    # MAX_TOTAL_BYTES without masking governance/delete/within-budget hazards, and
    # parse candidate manifests so Namespace labels and cross-file GVKNN duplicates
    # are available to Pass 2.
    norm_entries: list[tuple[FileChange, str, str, bool]] = []
    active_path_counts: dict[str, int] = {}
    candidate_parse_indices: list[tuple[int, int]] = []

    for idx, fc in enumerate(ordered):
        norm_path, path_ok = _normalize_path(fc.path)
        norm_old = ""
        if fc.old_path:
            norm_old, old_ok = _normalize_path(fc.old_path)
            path_ok = path_ok and old_ok
        norm_entries.append((fc, norm_path, norm_old, path_ok))
        if not path_ok:
            continue
        if fc.change_type != CHANGE_DELETED:
            active_path_counts[norm_path] = active_path_counts.get(norm_path, 0) + 1
        modes = {fc.before_mode, fc.after_mode}
        if modes - _KNOWN_ALL_MODES:
            continue
        if fc.is_binary or fc.oversized or fc.integrity_error:
            continue
        if _is_critical_governance_path(norm_path) or (norm_old and _is_critical_governance_path(norm_old)):
            continue
        if _is_supply_chain_path(norm_path) or _is_terraform_path(norm_path):
            continue
        b_regular = fc.before_mode not in (_MODE_SYMLINK, _MODE_GITLINK)
        a_regular = fc.after_mode not in (_MODE_SYMLINK, _MODE_GITLINK)
        b_raw = fc.before if b_regular else None
        a_raw = fc.after if a_regular else None
        if (_MODE_SYMLINK in modes or _MODE_GITLINK in modes) and b_raw is None and a_raw is None:
            continue
        _, ext = posixpath.splitext(norm_path.lower())
        if ext in _MANIFEST_EXTENSIONS:
            blob_sz = len(b_raw or b"") + len(a_raw or b"")
            candidate_parse_indices.append((blob_sz, idx))

    # Allocate MAX_TOTAL_BYTES budget smallest-files-first so large padding files
    # cannot starve small high/critical manifests alphabetically after them.
    parsed_cache: dict[int, tuple[ParseOutcome, ParseOutcome]] = {}
    budget_exceeded_indices: set[int] = set()
    cumulative_parsed_bytes = 0
    for blob_sz, idx in sorted(candidate_parse_indices):
        if cumulative_parsed_bytes + blob_sz > MAX_TOTAL_BYTES:
            budget_exceeded_indices.add(idx)
            continue
        cumulative_parsed_bytes += blob_sz
        fc, norm_path, _, _ = norm_entries[idx]
        b_raw = fc.before if fc.before_mode not in (_MODE_SYMLINK, _MODE_GITLINK) else None
        a_raw = fc.after if fc.after_mode not in (_MODE_SYMLINK, _MODE_GITLINK) else None
        is_json = norm_path.lower().endswith(".json")
        b_out = parse_manifest_bytes(b_raw, is_json=is_json)
        a_out = parse_manifest_bytes(a_raw, is_json=is_json)
        parsed_cache[idx] = (b_out, a_out)

    # Also enforce MAX_TOTAL_BYTES across non-manifest blobs without early-returning
    total_raw_bytes = sum(len(c.before or b"") + len(c.after or b"") for c in ordered)
    pr_payload_exceeded = total_raw_bytes > MAX_TOTAL_BYTES or bool(budget_exceeded_indices)

    # Build monotonic, cluster-scoped Namespace label cache and cross-file GVKNN index
    ns_labels_by_cluster: dict[str, dict[str, dict[str, Any]]] = {}
    before_scope_owners: dict[tuple[str, tuple[str, str, str, str]], list[int]] = {}
    after_scope_owners: dict[tuple[str, tuple[str, str, str, str]], list[int]] = {}

    for idx in sorted(parsed_cache.keys()):
        _, norm_path, _, _ = norm_entries[idx]
        b_out, a_out = parsed_cache[idx]
        c_key = _cluster_from_path(norm_path).lower()
        c_map = ns_labels_by_cluster.setdefault(c_key, {})
        for out in (b_out, a_out):
            for res in out.resources:
                if res.ref.group == "" and res.ref.kind == "Namespace":
                    meta = res.obj.get("metadata")
                    lbls = meta.get("labels") if isinstance(meta, dict) else None
                    if isinstance(lbls, dict):
                        c_map[res.ref.name] = _merge_ns_labels_monotonic(c_map.get(res.ref.name, {}), lbls)

        if not _is_unrendered_package_path(norm_path):
            scope = _sync_scope_key(norm_path)
            seen_b_in_file: set[tuple[str, str, str, str]] = set()
            for res in b_out.resources:
                rk = _canonical_identity_key(res)
                if rk not in seen_b_in_file:
                    seen_b_in_file.add(rk)
                    before_scope_owners.setdefault((scope, rk), []).append(idx)
            seen_a_in_file: set[tuple[str, str, str, str]] = set()
            for res in a_out.resources:
                rk = _canonical_identity_key(res)
                if rk not in seen_a_in_file:
                    seen_a_in_file.add(rk)
                    after_scope_owners.setdefault((scope, rk), []).append(idx)

    cross_file_dup_peers: dict[int, set[str]] = {}
    for owners in (*before_scope_owners.values(), *after_scope_owners.values()):
        if len(owners) > 1:
            paths_involved = [norm_entries[i][1] for i in owners]
            for i in owners:
                own_path = norm_entries[i][1]
                peers = cross_file_dup_peers.setdefault(i, set())
                for p in paths_involved:
                    if p != own_path:
                        peers.add(p)

    # Pass 2: evaluate every file cleanly
    overall_risk = RISK_LOW
    overall_blast = BLAST_LOW
    overall_revert = REVERT_EASY
    overall_disrupt = DISRUPT_NONE
    all_supported = True
    any_effective = False

    reasons: list[Reason] = []
    file_verdicts: list[FileVerdict] = []
    resource_verdicts: list[ResourceVerdict] = []

    res_added = 0
    res_modified = 0
    res_deleted = 0
    unsupported_files = 0
    unsupported_resources = 0
    parse_errors = 0
    namespaces_touched: set[str] = set()
    clusters_touched: set[str] = set()
    total_cpu_delta_m = 0.0
    total_mem_delta_b = 0.0
    cpu_delta_overflow = False
    mem_delta_overflow = False

    for idx, (fc, norm_path, norm_old, path_ok) in enumerate(norm_entries):
        if not path_ok:
            any_effective = True
            all_supported = False
            unsupported_files += 1
            bad_risk = RISK_HIGH
            bad_blast = BLAST_UNKNOWN
            bad_revert = REVERT_UNKNOWN
            bad_disrupt = DISRUPT_DESTRUCTIVE if fc.change_type == CHANGE_DELETED else DISRUPT_UNKNOWN
            if _is_critical_governance_path(fc.path) or (fc.old_path and _is_critical_governance_path(fc.old_path)):
                bad_risk = RISK_CRITICAL
                bad_blast = BLAST_CRITICAL
                reasons.append(
                    Reason(
                        code="CRITICAL_GOVERNANCE_OR_CI_FILE",
                        dimension=DIM_SECURITY,
                        severity=RISK_CRITICAL,
                        message=f"modifies governance, CI/CD, encryption, or agent-control file {fc.path!r}",
                        file=fc.path,
                    )
                )
            reasons.append(
                Reason(
                    code="INVALID_FILE_PATH",
                    dimension=DIM_INPUT_INTEGRITY,
                    severity=RISK_HIGH,
                    message=f"unsafe or non-normalized file path {fc.path!r}",
                    file=fc.path,
                )
            )
            file_verdicts.append(
                FileVerdict(
                    path=fc.path,
                    old_path=fc.old_path,
                    change_type=fc.change_type,
                    category="invalid",
                    risk_level=bad_risk,
                    blast_radius=bad_blast,
                    revertability=bad_revert,
                    disruptiveness=bad_disrupt,
                    supported=False,
                )
            )
            overall_risk = combine_risk(overall_risk, bad_risk)
            overall_blast = combine_blast_radius(overall_blast, bad_blast)
            overall_revert = combine_revertability(overall_revert, bad_revert)
            overall_disrupt = combine_disruptiveness(overall_disrupt, bad_disrupt)
            continue

        cluster = _cluster_from_path(norm_path)
        if cluster:
            clusters_touched.add(cluster)
        if norm_old:
            old_cluster = _cluster_from_path(norm_old)
            if old_cluster:
                clusters_touched.add(old_cluster)

        modes = {fc.before_mode, fc.after_mode}
        unknown_modes = modes - _KNOWN_ALL_MODES
        in_sync_tree = any(norm_path.lower().startswith(pfx) for pfx in _SYNC_ROOT_PREFIXES)
        is_gov = _is_critical_governance_path(norm_path) or (bool(norm_old) and _is_critical_governance_path(norm_old))
        has_dup_file_entry = fc.change_type != CHANGE_DELETED and active_path_counts.get(norm_path, 0) > 1

        # 2a. Critical governance / CI / agent control-plane files
        if is_gov:
            any_effective = True
            gov_supported = not (
                _MODE_SYMLINK in modes
                or _MODE_GITLINK in modes
                or bool(unknown_modes)
                or bool(fc.integrity_error)
                or fc.oversized
                or fc.is_binary
                or has_dup_file_entry
            )
            if not gov_supported:
                all_supported = False
                unsupported_files += 1
            disrupt = DISRUPT_DESTRUCTIVE if fc.change_type == CHANGE_DELETED else DISRUPT_NONE
            gov_rev = REVERT_DIFFICULT if gov_supported else REVERT_UNKNOWN
            sec_reasons = _scan_raw_blob_secrets(
                fc.after if fc.after_mode not in (_MODE_SYMLINK, _MODE_GITLINK) else None,
                norm_path,
                is_binary=fc.is_binary,
            )
            if sec_reasons:
                reasons.extend(sec_reasons)
                gov_rev = combine_revertability(gov_rev, REVERT_IRREVERSIBLE)
            reasons.append(
                Reason(
                    code="CRITICAL_GOVERNANCE_OR_CI_FILE",
                    dimension=DIM_SECURITY,
                    severity=RISK_CRITICAL,
                    message=f"modifies governance, CI/CD, encryption, or agent-control file {norm_path!r}",
                    file=norm_path,
                )
            )
            file_verdicts.append(
                FileVerdict(
                    path=norm_path,
                    old_path=norm_old,
                    change_type=fc.change_type,
                    category="governance",
                    risk_level=RISK_CRITICAL,
                    blast_radius=BLAST_CRITICAL,
                    revertability=gov_rev,
                    disruptiveness=disrupt,
                    supported=gov_supported,
                )
            )
            overall_risk = combine_risk(overall_risk, RISK_CRITICAL)
            overall_blast = combine_blast_radius(overall_blast, BLAST_CRITICAL)
            overall_revert = combine_revertability(overall_revert, gov_rev)
            overall_disrupt = combine_disruptiveness(overall_disrupt, disrupt)
            continue

        # 2b. Pure symlinks, git submodules, and unrecognized git entry modes
        # (Symlink/submodule typechanges on `.yaml`/`.yml`/`.json` enter `parsed_cache`
        # and are handled in 2h so the regular manifest side is ALSO evaluated!)
        if unknown_modes or ((_MODE_SYMLINK in modes or _MODE_GITLINK in modes) and idx not in parsed_cache):
            any_effective = True
            all_supported = False
            unsupported_files += 1
            if unknown_modes:
                code, cat, label = "UNSUPPORTED_FILE_MODE", "invalid", f"unsupported git mode {sorted(unknown_modes)[0]}"
                sev = RISK_HIGH
            elif _MODE_SYMLINK in modes:
                code, cat, label = "SYMLINK_CHANGE", "symlink", "symlink"
                sev = RISK_HIGH if in_sync_tree else RISK_UNKNOWN
            else:
                code, cat, label = "SUBMODULE_CHANGE", "submodule", "git submodule"
                sev = RISK_HIGH if in_sync_tree else RISK_UNKNOWN
            blast = BLAST_HIGH if sev == RISK_HIGH else BLAST_UNKNOWN
            rev = REVERT_UNKNOWN
            a_reg_bytes = fc.after if fc.after_mode not in (_MODE_SYMLINK, _MODE_GITLINK) else None
            sec_reasons = _scan_raw_blob_secrets(a_reg_bytes, norm_path, is_binary=fc.is_binary)
            if sec_reasons:
                reasons.extend(sec_reasons)
                sev = combine_risk(sev, RISK_CRITICAL)
                blast = combine_blast_radius(blast, BLAST_HIGH)
                rev = combine_revertability(rev, REVERT_IRREVERSIBLE)
            reasons.append(
                Reason(
                    code=code,
                    dimension=DIM_SECURITY if sev in (RISK_HIGH, RISK_CRITICAL) else DIM_INPUT_INTEGRITY,
                    severity=RISK_HIGH if code == "UNSUPPORTED_FILE_MODE" or in_sync_tree else RISK_UNKNOWN,
                    message=f"{label} change at {norm_path!r} cannot be verified offline",
                    file=norm_path,
                )
            )
            file_verdicts.append(
                FileVerdict(
                    path=norm_path,
                    old_path=norm_old,
                    change_type=fc.change_type,
                    category=cat,
                    risk_level=sev,
                    blast_radius=blast,
                    revertability=rev,
                    disruptiveness=DISRUPT_UNKNOWN,
                    supported=False,
                )
            )
            overall_risk = combine_risk(overall_risk, sev)
            overall_blast = combine_blast_radius(overall_blast, blast)
            overall_revert = combine_revertability(overall_revert, rev)
            overall_disrupt = combine_disruptiveness(overall_disrupt, DISRUPT_UNKNOWN)
            continue

        # 2b2. Git archive blob integrity error (e.g. .gitattributes export-ignore/export-subst)
        if fc.integrity_error:
            any_effective = True
            all_supported = False
            unsupported_files += 1
            reasons.append(
                Reason(
                    code="ARCHIVE_INTEGRITY_ERROR",
                    dimension=DIM_INPUT_INTEGRITY,
                    severity=RISK_HIGH,
                    message=f"could not read exact git blob for {norm_path!r}: {fc.integrity_error}",
                    file=norm_path,
                )
            )
            file_verdicts.append(
                FileVerdict(
                    path=norm_path,
                    old_path=norm_old,
                    change_type=fc.change_type,
                    category="integrity_error",
                    risk_level=RISK_HIGH,
                    blast_radius=BLAST_UNKNOWN,
                    revertability=REVERT_UNKNOWN,
                    disruptiveness=DISRUPT_UNKNOWN,
                    supported=False,
                )
            )
            overall_risk = combine_risk(overall_risk, RISK_HIGH)
            overall_blast = combine_blast_radius(overall_blast, BLAST_UNKNOWN)
            overall_revert = combine_revertability(overall_revert, REVERT_UNKNOWN)
            overall_disrupt = combine_disruptiveness(overall_disrupt, DISRUPT_UNKNOWN)
            continue

        # 2c. Supply-chain / container / dependency build files -> `high` (checked before
        # oversized/binary so e.g. binary `bun.lockb` or large `package-lock.json` stays HIGH)
        if _is_supply_chain_path(norm_path):
            any_effective = True
            sc_supported = not (fc.oversized or fc.is_binary or idx in budget_exceeded_indices or has_dup_file_entry)
            if not sc_supported:
                all_supported = False
                unsupported_files += 1
            sc_risk = RISK_HIGH
            sc_blast = BLAST_HIGH
            sc_rev = REVERT_EASY if sc_supported else REVERT_UNKNOWN
            sec_reasons = _scan_raw_blob_secrets(fc.after, norm_path, is_binary=fc.is_binary)
            if sec_reasons:
                reasons.extend(sec_reasons)
                sc_risk = combine_risk(sc_risk, RISK_CRITICAL)
                sc_rev = combine_revertability(sc_rev, REVERT_IRREVERSIBLE)
            reasons.append(
                Reason(
                    code="SUPPLY_CHAIN_BUILD_FILE",
                    dimension=DIM_SECURITY,
                    severity=RISK_HIGH,
                    message=f"modifies build/container/dependency supply-chain file {norm_path!r}",
                    file=norm_path,
                )
            )
            file_verdicts.append(
                FileVerdict(
                    path=norm_path,
                    old_path=norm_old,
                    change_type=fc.change_type,
                    category="supply_chain",
                    risk_level=sc_risk,
                    blast_radius=sc_blast,
                    revertability=sc_rev,
                    disruptiveness=DISRUPT_UNKNOWN,
                    supported=sc_supported,
                )
            )
            overall_risk = combine_risk(overall_risk, sc_risk)
            overall_blast = combine_blast_radius(overall_blast, sc_blast)
            overall_revert = combine_revertability(overall_revert, sc_rev)
            overall_disrupt = combine_disruptiveness(overall_disrupt, DISRUPT_UNKNOWN)
            continue

        # 2d. Terraform / OpenTofu / Terragrunt files (checked before oversized/manifest so
        # `.tf.json`/`.tofu.json` and oversized `.tf` deletions keep HIGH/CRITICAL destructive!)
        if _is_terraform_path(norm_path):
            any_effective = True
            all_supported = False
            unsupported_files += 1
            if fc.change_type == CHANGE_DELETED:
                tf_base_sev = RISK_CRITICAL if _is_production_path(norm_path) else RISK_HIGH
                disrupt = DISRUPT_DESTRUCTIVE
                msg = f"deletes Terraform/OpenTofu/HCL infrastructure file {norm_path!r} (can destroy cloud resources on apply)"
            else:
                tf_base_sev = RISK_UNKNOWN
                disrupt = DISRUPT_UNKNOWN
                msg = f"Terraform/OpenTofu/HCL file {norm_path!r} requires a verified plan to determine blast radius"
            sev = combine_risk(tf_base_sev, RISK_HIGH) if fc.after_mode == _MODE_EXEC else tf_base_sev
            blast = BLAST_HIGH if fc.change_type == CHANGE_DELETED else BLAST_UNKNOWN
            rev = REVERT_UNKNOWN
            sec_reasons = _scan_raw_blob_secrets(fc.after, norm_path, is_binary=fc.is_binary)
            if sec_reasons:
                reasons.extend(sec_reasons)
                sev = combine_risk(sev, RISK_CRITICAL)
                blast = combine_blast_radius(blast, BLAST_HIGH)
                rev = combine_revertability(rev, REVERT_IRREVERSIBLE)
            if fc.after_mode == _MODE_EXEC:
                reasons.append(
                    Reason(
                        code="EXECUTABLE_FILE",
                        dimension=DIM_SECURITY,
                        severity=RISK_HIGH,
                        message=f"infrastructure file {norm_path!r} has executable mode 100755",
                        file=norm_path,
                    )
                )
            reasons.append(
                Reason(
                    code="UNSUPPORTED_TERRAFORM_FILE",
                    dimension=DIM_INPUT_INTEGRITY,
                    severity=tf_base_sev,
                    message=msg,
                    file=norm_path,
                )
            )
            file_verdicts.append(
                FileVerdict(
                    path=norm_path,
                    old_path=norm_old,
                    change_type=fc.change_type,
                    category="terraform",
                    risk_level=sev,
                    blast_radius=blast,
                    revertability=rev,
                    disruptiveness=disrupt,
                    supported=False,
                )
            )
            overall_risk = combine_risk(overall_risk, sev)
            overall_blast = combine_blast_radius(overall_blast, blast)
            overall_revert = combine_revertability(overall_revert, rev)
            overall_disrupt = combine_disruptiveness(overall_disrupt, disrupt)
            continue

        # 2e. Oversized, PR-budget-exceeded, or binary files -> `unknown` (or `high` if executable)
        if fc.oversized or fc.is_binary or idx in budget_exceeded_indices:
            any_effective = True
            all_supported = False
            unsupported_files += 1
            if idx in budget_exceeded_indices and not fc.oversized and not fc.is_binary:
                code = "PR_PAYLOAD_TOO_LARGE"
                cat = "oversized"
                msg = f"file {norm_path!r} exceeds aggregate PR byte budget ({MAX_TOTAL_BYTES} bytes)"
            elif fc.oversized:
                code = "OVERSIZED_FILE"
                cat = "oversized"
                msg = f"file {norm_path!r} exceeds size limit and cannot be inspected"
            else:
                code = "BINARY_FILE"
                cat = "binary"
                msg = f"binary file {norm_path!r} cannot be inspected deterministically"
            sev = RISK_HIGH if fc.after_mode == _MODE_EXEC else RISK_UNKNOWN
            blast = BLAST_MEDIUM if fc.after_mode == _MODE_EXEC else BLAST_UNKNOWN
            if fc.after_mode == _MODE_EXEC:
                reasons.append(
                    Reason(
                        code="EXECUTABLE_FILE",
                        dimension=DIM_SECURITY,
                        severity=RISK_HIGH,
                        message=f"file {norm_path!r} has executable mode 100755",
                        file=norm_path,
                    )
                )
            reasons.append(
                Reason(
                    code=code,
                    dimension=DIM_INPUT_INTEGRITY,
                    severity=RISK_UNKNOWN,
                    message=msg,
                    file=norm_path,
                )
            )
            file_verdicts.append(
                FileVerdict(
                    path=norm_path,
                    old_path=norm_old,
                    change_type=fc.change_type,
                    category=cat,
                    risk_level=sev,
                    blast_radius=blast,
                    revertability=REVERT_UNKNOWN,
                    disruptiveness=DISRUPT_UNKNOWN,
                    supported=False,
                )
            )
            overall_risk = combine_risk(overall_risk, sev)
            overall_blast = combine_blast_radius(overall_blast, blast)
            overall_revert = combine_revertability(overall_revert, REVERT_UNKNOWN)
            overall_disrupt = combine_disruptiveness(overall_disrupt, DISRUPT_UNKNOWN)
            continue

        # 2f. Non-YAML/JSON unrendered Helm / Kustomize / template files -> `unknown`
        is_unrendered_pkg = _is_unrendered_package_path(norm_path)
        if is_unrendered_pkg and idx not in parsed_cache:
            any_effective = True
            all_supported = False
            unsupported_files += 1
            sev = RISK_HIGH if fc.after_mode == _MODE_EXEC else RISK_UNKNOWN
            blast = BLAST_MEDIUM if fc.after_mode == _MODE_EXEC else BLAST_UNKNOWN
            rev = REVERT_UNKNOWN
            sec_reasons = _scan_raw_blob_secrets(fc.after, norm_path, is_binary=fc.is_binary)
            if sec_reasons:
                reasons.extend(sec_reasons)
                sev = combine_risk(sev, RISK_CRITICAL)
                blast = combine_blast_radius(blast, BLAST_HIGH)
                rev = combine_revertability(rev, REVERT_IRREVERSIBLE)
            if fc.after_mode == _MODE_EXEC:
                reasons.append(
                    Reason(
                        code="EXECUTABLE_FILE",
                        dimension=DIM_SECURITY,
                        severity=RISK_HIGH,
                        message=f"file {norm_path!r} has executable mode 100755",
                        file=norm_path,
                    )
                )
            reasons.append(
                Reason(
                    code="UNRENDERED_PACKAGE_OR_OVERLAY",
                    dimension=DIM_INPUT_INTEGRITY,
                    severity=RISK_UNKNOWN,
                    message=f"unrendered Helm/Kustomize/overlay file {norm_path!r} cannot be evaluated without rendering",
                    file=norm_path,
                )
            )
            file_verdicts.append(
                FileVerdict(
                    path=norm_path,
                    old_path=norm_old,
                    change_type=fc.change_type,
                    category="unrendered_package",
                    risk_level=sev,
                    blast_radius=blast,
                    revertability=rev,
                    disruptiveness=DISRUPT_UNKNOWN,
                    supported=False,
                )
            )
            overall_risk = combine_risk(overall_risk, sev)
            overall_blast = combine_blast_radius(overall_blast, blast)
            overall_revert = combine_revertability(overall_revert, rev)
            overall_disrupt = combine_disruptiveness(overall_disrupt, DISRUPT_UNKNOWN)
            continue

        # 2g. Pure non-executable, non-agent documentation outside sync trees -> `low`
        # (Still scanned for leaked credentials so private keys/PATs in README.md are CRITICAL!)
        if _is_pure_doc_path(norm_path) and fc.after_mode != _MODE_EXEC and not has_dup_file_entry:
            sec_reasons = _scan_raw_blob_secrets(fc.after, norm_path, is_binary=fc.is_binary)
            if sec_reasons:
                any_effective = True
                reasons.extend(sec_reasons)
                file_verdicts.append(
                    FileVerdict(
                        path=norm_path,
                        old_path=norm_old,
                        change_type=fc.change_type,
                        category="documentation",
                        risk_level=RISK_CRITICAL,
                        blast_radius=BLAST_HIGH,
                        revertability=REVERT_IRREVERSIBLE,
                        disruptiveness=DISRUPT_NONE,
                        supported=True,
                    )
                )
                overall_risk = combine_risk(overall_risk, RISK_CRITICAL)
                overall_blast = combine_blast_radius(overall_blast, BLAST_HIGH)
                overall_revert = combine_revertability(overall_revert, REVERT_IRREVERSIBLE)
                continue
            reasons.append(
                Reason(
                    code="DOCUMENTATION_ONLY",
                    dimension=DIM_BLAST_RADIUS,
                    severity=RISK_LOW,
                    message=f"documentation file {norm_path!r} has no runtime cluster impact",
                    file=norm_path,
                )
            )
            file_verdicts.append(
                FileVerdict(
                    path=norm_path,
                    old_path=norm_old,
                    change_type=fc.change_type,
                    category="documentation",
                    risk_level=RISK_LOW,
                    blast_radius=BLAST_LOW,
                    revertability=REVERT_EASY,
                    disruptiveness=DISRUPT_NONE,
                    supported=True,
                )
            )
            continue

        # 2h. Structured YAML/JSON Kubernetes manifests (including `.yaml`/`.yml`/`.json`
        # under overlays/patches/templates and symlink typechanges so embedded hazards surface!)
        if idx in parsed_cache:
            b_out, a_out = parsed_cache[idx]

            f_risk = RISK_LOW
            f_blast = BLAST_LOW
            f_revert = REVERT_EASY
            f_disrupt = DISRUPT_NONE
            f_supported = True
            f_category = "manifest"
            f_effective_resources = 0
            file_marked_unsupported = False

            def _mark_file_unsupported(cat: str) -> None:
                nonlocal all_supported, f_supported, f_category, unsupported_files, file_marked_unsupported
                all_supported = False
                f_supported = False
                if not file_marked_unsupported:
                    file_marked_unsupported = True
                    unsupported_files += 1
                    f_category = cat

            # Symlink / gitlink typechange (`120000 <-> 100644`): record symlink/submodule
            # change AND continue evaluating the regular manifest side!
            if _MODE_SYMLINK in modes or _MODE_GITLINK in modes:
                any_effective = True
                is_link = _MODE_SYMLINK in modes
                _mark_file_unsupported("symlink" if is_link else "submodule")
                sym_sev = RISK_HIGH if in_sync_tree else RISK_UNKNOWN
                sym_blast = BLAST_HIGH if in_sync_tree else BLAST_UNKNOWN
                reasons.append(
                    Reason(
                        code="SYMLINK_CHANGE" if is_link else "SUBMODULE_CHANGE",
                        dimension=DIM_SECURITY if in_sync_tree else DIM_INPUT_INTEGRITY,
                        severity=sym_sev,
                        message=f"{'symlink' if is_link else 'git submodule'} change at {norm_path!r} cannot be verified offline",
                        file=norm_path,
                    )
                )
                f_risk = combine_risk(f_risk, sym_sev)
                f_blast = combine_blast_radius(f_blast, sym_blast)
                f_revert = combine_revertability(f_revert, REVERT_UNKNOWN)
                f_disrupt = combine_disruptiveness(f_disrupt, DISRUPT_UNKNOWN)

            if has_dup_file_entry:
                any_effective = True
                _mark_file_unsupported("invalid_manifest")
                parse_errors += 1
                reasons.append(
                    Reason(
                        code="DUPLICATE_FILE_PATH",
                        dimension=DIM_INPUT_INTEGRITY,
                        severity=RISK_UNKNOWN,
                        message=f"PR change list contains multiple entries targeting {norm_path!r}",
                        file=norm_path,
                    )
                )
                f_risk = combine_risk(f_risk, RISK_UNKNOWN)
                f_blast = combine_blast_radius(f_blast, BLAST_UNKNOWN)
                f_revert = combine_revertability(f_revert, REVERT_UNKNOWN)
                f_disrupt = combine_disruptiveness(f_disrupt, DISRUPT_UNKNOWN)

            # Executable manifest file mode: record HIGH risk, then CONTINUE evaluating
            # the manifest content so critical/destructive payloads are never masked.
            if fc.after_mode == _MODE_EXEC:
                any_effective = True
                reasons.append(
                    Reason(
                        code="EXECUTABLE_MANIFEST",
                        dimension=DIM_SECURITY,
                        severity=RISK_HIGH,
                        message=f"manifest file {norm_path!r} has executable mode 100755",
                        file=norm_path,
                    )
                )
                f_risk = combine_risk(f_risk, RISK_HIGH)
                f_blast = combine_blast_radius(f_blast, BLAST_MEDIUM)

            # Unrendered Helm/Kustomize/overlay path floor -> `unknown` (still evaluates
            # any parsed K8s objects below so privileged RBAC / PVC deletes escalate!)
            if is_unrendered_pkg:
                any_effective = True
                _mark_file_unsupported("unrendered_package")
                reasons.append(
                    Reason(
                        code="UNRENDERED_PACKAGE_OR_OVERLAY",
                        dimension=DIM_INPUT_INTEGRITY,
                        severity=RISK_UNKNOWN,
                        message=f"unrendered Helm/Kustomize/overlay file {norm_path!r} cannot be evaluated without rendering",
                        file=norm_path,
                    )
                )
                f_risk = combine_risk(f_risk, RISK_UNKNOWN)
                f_blast = combine_blast_radius(f_blast, BLAST_UNKNOWN)
                f_revert = combine_revertability(f_revert, REVERT_UNKNOWN)
                f_disrupt = combine_disruptiveness(f_disrupt, DISRUPT_UNKNOWN)

            # Parse errors -> `unknown` (record both sides if distinct; still evaluate any
            # resources parsed before the error so Doc 2 errors never mask Doc 1 hazards)
            if b_out.error or a_out.error:
                any_effective = True
                _mark_file_unsupported("invalid_manifest")
                f_category = "invalid_manifest"
                parse_errors += 1
                seen_err_codes: set[tuple[str, str]] = set()
                for side_out in (a_out, b_out):
                    if side_out.error:
                        ecode = side_out.error_code or "MANIFEST_PARSE_ERROR"
                        sig = (ecode, side_out.error)
                        if sig not in seen_err_codes:
                            seen_err_codes.add(sig)
                            reasons.append(
                                Reason(
                                    code=ecode,
                                    dimension=DIM_INPUT_INTEGRITY,
                                    severity=RISK_UNKNOWN,
                                    message=f"could not safely parse {norm_path!r}: {side_out.error}",
                                    file=norm_path,
                                )
                            )
                f_risk = combine_risk(f_risk, RISK_UNKNOWN)
                f_blast = combine_blast_radius(f_blast, BLAST_UNKNOWN)
                f_revert = combine_revertability(f_revert, REVERT_UNKNOWN)
                f_disrupt = combine_disruptiveness(f_disrupt, DISRUPT_UNKNOWN)

            # Unrendered templates / Kustomize files discovered by content:
            # always combine with any valid embedded resources so `${HARMLESS}` in an
            # annotation can NEVER downgrade a critical/high K8s change to `unknown`.
            is_tmpl = b_out.is_template or a_out.is_template
            is_kust = b_out.is_kustomize or a_out.is_kustomize
            if is_tmpl or is_kust:
                any_effective = True
                _mark_file_unsupported("unrendered_package")
                if is_tmpl:
                    reasons.append(
                        Reason(
                            code="UNRENDERED_TEMPLATE",
                            dimension=DIM_INPUT_INTEGRITY,
                            severity=RISK_UNKNOWN,
                            message=f"file {norm_path!r} contains unrendered template syntax",
                            file=norm_path,
                        )
                    )
                if is_kust:
                    reasons.append(
                        Reason(
                            code="UNRENDERED_KUSTOMIZE",
                            dimension=DIM_INPUT_INTEGRITY,
                            severity=RISK_UNKNOWN,
                            message=f"file {norm_path!r} contains unrendered Kustomize manifest",
                            file=norm_path,
                        )
                    )
                f_risk = combine_risk(f_risk, RISK_UNKNOWN)
                f_blast = combine_blast_radius(f_blast, BLAST_UNKNOWN)
                f_revert = combine_revertability(f_revert, REVERT_UNKNOWN)
                f_disrupt = combine_disruptiveness(f_disrupt, DISRUPT_UNKNOWN)

            # Valid non-Kubernetes YAML/JSON (e.g. `.golangci.yml`, `tsconfig.json`,
            # empty `[]`/`null`/comment-only files with zero K8s objects on both sides) -> `unknown`
            is_non_k8s = (
                not b_out.error
                and not a_out.error
                and not is_unrendered_pkg
                and not is_tmpl
                and not is_kust
                and (_MODE_SYMLINK not in modes and _MODE_GITLINK not in modes)
                and (
                    not b_out.is_k8s_manifest
                    or not a_out.is_k8s_manifest
                    or (not b_out.resources and not a_out.resources)
                )
            )
            if is_non_k8s:
                any_effective = True
                _mark_file_unsupported("non_k8s_config")
                reasons.append(
                    Reason(
                        code="NON_K8S_STRUCTURED_FILE",
                        dimension=DIM_INPUT_INTEGRITY,
                        severity=RISK_UNKNOWN,
                        message=f"structured file {norm_path!r} does not contain Kubernetes resources",
                        file=norm_path,
                    )
                )
                f_risk = combine_risk(f_risk, RISK_UNKNOWN)
                f_blast = combine_blast_radius(f_blast, BLAST_UNKNOWN)
                f_revert = combine_revertability(f_revert, REVERT_UNKNOWN)
                f_disrupt = combine_disruptiveness(f_disrupt, DISRUPT_UNKNOWN)

            # When `after` produced no K8s resources (e.g. `values.yaml`, `.golangci.yml`,
            # or unparseable YAML), still scan raw `after` bytes for leaked credentials!
            if not a_out.resources and fc.after_mode not in (_MODE_SYMLINK, _MODE_GITLINK):
                sec_reasons = _scan_raw_blob_secrets(fc.after, norm_path, is_binary=fc.is_binary)
                if sec_reasons:
                    any_effective = True
                    reasons.extend(sec_reasons)
                    f_risk = combine_risk(f_risk, RISK_CRITICAL)
                    f_blast = combine_blast_radius(f_blast, BLAST_HIGH)
                    f_revert = combine_revertability(f_revert, REVERT_IRREVERSIBLE)

            # Check both intra-file and cross-file duplicate resource identities, then
            # ALWAYS continue evaluating all parsed resources (never skip via `elif`!).
            b_lists, b_dup = _group_resource_lists(b_out.resources)
            a_lists, a_dup = _group_resource_lists(a_out.resources)
            cross_peers = cross_file_dup_peers.get(idx)
            if b_dup or a_dup or cross_peers:
                any_effective = True
                _mark_file_unsupported("invalid_manifest")
                f_category = "invalid_manifest"
                parse_errors += 1
                dup_msg = (
                    f"manifest {norm_path!r} defines duplicate Kubernetes resource identities"
                    if (b_dup or a_dup)
                    else f"manifest {norm_path!r} collides on Kubernetes resource identity with {sorted(cross_peers)[0]!r}"
                )
                reasons.append(
                    Reason(
                        code="DUPLICATE_RESOURCE_IDENTITY",
                        dimension=DIM_INPUT_INTEGRITY,
                        severity=RISK_UNKNOWN,
                        message=dup_msg,
                        file=norm_path,
                    )
                )
                f_risk = combine_risk(f_risk, RISK_UNKNOWN)
                f_blast = combine_blast_radius(f_blast, BLAST_UNKNOWN)
                f_revert = combine_revertability(f_revert, REVERT_UNKNOWN)
                f_disrupt = combine_disruptiveness(f_disrupt, DISRUPT_UNKNOWN)

            if b_lists or a_lists:
                # Merge root ("") + current cluster Namespace labels monotonically
                effective_ns_labels: dict[str, dict[str, Any]] = {}
                for ns_src in (
                    ns_labels_by_cluster.get("", {}),
                    ns_labels_by_cluster.get(cluster.lower(), {}) if cluster else {},
                ):
                    for ns_name, lbl_map in ns_src.items():
                        effective_ns_labels[ns_name] = _merge_ns_labels_monotonic(
                            effective_ns_labels.get(ns_name, {}), lbl_map
                        )

                if fc.change_type == CHANGE_RENAMED:
                    any_effective = True
                    ren_risk = RISK_MEDIUM
                    ren_blast = BLAST_LOW
                    ren_revert = REVERT_MODERATE
                    reasons.append(
                        Reason(
                            code="MANIFEST_FILE_RENAMED",
                            dimension=DIM_REVERTABILITY,
                            severity=RISK_MEDIUM,
                            message=f"renames manifest {norm_old!r} -> {norm_path!r}",
                            file=norm_path,
                        )
                    )
                    # Propagate namespace tracking, system-ns, self-mod, and prod escalation
                    # across ALL resources in a renamed manifest (even when YAML bytes match).
                    ren_prod = _is_production_path(norm_path) or (bool(norm_old) and _is_production_path(norm_old))
                    ren_high_impact = False
                    for r_item in (*a_out.resources, *b_out.resources):
                        if not is_cluster_scoped_kind(r_item.ref.group, r_item.ref.kind) and r_item.ref.namespace:
                            namespaces_touched.add(r_item.ref.namespace)
                        if _is_production_labels(_resource_meta_labels(r_item)) or _is_production_labels(
                            effective_ns_labels.get(r_item.ref.namespace, {})
                        ):
                            ren_prod = True
                    for r_item in a_out.resources:
                        if _is_kube_agents_group(r_item.ref.group):
                            ren_risk = combine_risk(ren_risk, RISK_CRITICAL)
                            ren_blast = combine_blast_radius(ren_blast, BLAST_CRITICAL)
                            ren_revert = combine_revertability(ren_revert, REVERT_DIFFICULT)
                            reasons.append(
                                Reason(
                                    code="KUBE_AGENTS_SELF_MODIFICATION",
                                    dimension=DIM_SECURITY,
                                    severity=RISK_CRITICAL,
                                    message=f"renames kube-agents control-plane CR {r_item.ref.gvk_label()} {r_item.ref.name!r}",
                                    file=norm_path,
                                    resource=r_item.ref,
                                )
                            )
                        elif r_item.ref.namespace in SYSTEM_NAMESPACES:
                            ren_high_impact = True
                            ren_risk = combine_risk(ren_risk, RISK_HIGH)
                            ren_blast = combine_blast_radius(ren_blast, BLAST_HIGH)
                            reasons.append(
                                Reason(
                                    code="SYSTEM_NAMESPACE_TARGET",
                                    dimension=DIM_SECURITY,
                                    severity=RISK_HIGH,
                                    message=f"renames manifest for {r_item.ref.gvk_label()} {r_item.ref.name!r} in system namespace {r_item.ref.namespace!r}",
                                    file=norm_path,
                                    resource=r_item.ref,
                                )
                            )
                    if ren_prod and ren_risk in (RISK_LOW, RISK_MEDIUM, RISK_HIGH):
                        esc = escalate_risk(ren_risk, high_impact=ren_high_impact)
                        if esc != ren_risk:
                            reasons.append(
                                Reason(
                                    code="PRODUCTION_ENVIRONMENT_ESCALATION",
                                    dimension=DIM_BLAST_RADIUS,
                                    severity=esc,
                                    message=f"escalated {ren_risk} -> {esc} because renamed manifest targets production",
                                    file=norm_path,
                                )
                            )
                            ren_risk = esc
                            ren_blast = combine_blast_radius(ren_blast, BLAST_HIGH)
                    f_risk = combine_risk(f_risk, ren_risk)
                    f_blast = combine_blast_radius(f_blast, ren_blast)
                    f_revert = combine_revertability(f_revert, ren_revert)

                # Build linear-bounded evaluation pairs (even when duplicates exist)
                eval_pairs: list[tuple[ParsedResource | None, ParsedResource | None]] = []
                for key in sorted(set(b_lists.keys()) | set(a_lists.keys())):
                    bl = b_lists.get(key, [])
                    al = a_lists.get(key, [])
                    if len(bl) <= 1 and len(al) <= 1:
                        b_one = bl[0] if bl else None
                        a_one = al[0] if al else None
                        if (
                            b_one is not None
                            and a_one is not None
                            and _strict_equal(b_one.obj, a_one.obj)
                            and _has_explicit_metadata_name(a_one)
                        ):
                            continue  # Unchanged resource in multi-doc manifest
                        eval_pairs.append((b_one, a_one))
                    elif not al:
                        for b_item in bl:
                            eval_pairs.append((b_item, None))
                    elif not bl:
                        for a_item in al:
                            eval_pairs.append((None, a_item))
                    else:
                        for a_item in al:
                            eval_pairs.append((bl[0], a_item))
                        for b_item in bl[1:]:
                            eval_pairs.append((b_item, al[-1]))

                for b_res, a_res in eval_pairs:
                    f_effective_resources += 1
                    any_effective = True
                    if b_res is None:
                        action = ACTION_ADD
                        res_added += 1
                    elif a_res is None:
                        action = ACTION_DELETE
                        res_deleted += 1
                    else:
                        action = ACTION_MODIFY
                        res_modified += 1

                    ev = evaluate_resource(norm_path, action, b_res, a_res, ns_labels=effective_ns_labels)

                    # Guard against metadata.generateName without metadata.name in GitOps manifests
                    primary_res = a_res if a_res is not None else b_res
                    assert primary_res is not None
                    if a_res is not None and not _has_explicit_metadata_name(a_res):
                        ev.add(
                            "GENERATE_NAME_WITHOUT_NAME",
                            DIM_INPUT_INTEGRITY,
                            RISK_UNKNOWN,
                            f"{a_res.ref.gvk_label()} uses metadata.generateName={a_res.ref.name!r} without deterministic metadata.name",
                            blast=BLAST_UNKNOWN,
                            revert=REVERT_UNKNOWN,
                            disrupt=DISRUPT_UNKNOWN,
                            field_path="metadata.generateName",
                            supported=False,
                        )

                    # Guard against API version changes on paired resources (e.g. v1beta1 -> v1 or bogus v999)
                    if b_res is not None and a_res is not None and b_res.ref.version != a_res.ref.version:
                        core_invalid = a_res.ref.group == "" and a_res.ref.version != "v1"
                        ev.add(
                            "RESOURCE_API_VERSION_CHANGED",
                            DIM_DISRUPTIVENESS if core_invalid else DIM_REVERTABILITY,
                            RISK_HIGH if core_invalid else RISK_MEDIUM,
                            f"changes {a_res.ref.gvk_label()} {a_res.ref.name!r} API version from {b_res.ref.version!r} to {a_res.ref.version!r}",
                            revert=REVERT_MODERATE,
                            disrupt=DISRUPT_DISRUPTIVE if core_invalid else None,
                            field_path="apiVersion",
                        )

                    if not is_cluster_scoped_kind(ev.ref.group, ev.ref.kind) and ev.ref.namespace:
                        namespaces_touched.add(ev.ref.namespace)

                    # Compute Kubernetes-accurate pod scheduling delta across ADD/MODIFY/DELETE
                    if (primary_res.ref.group, primary_res.ref.kind) in WORKLOAD_KINDS:
                        b_cpu, b_mem = _workload_scheduling_totals(b_res)
                        a_cpu, a_mem = _workload_scheduling_totals(a_res)
                        total_cpu_delta_m, ov_c = _safe_add_delta(total_cpu_delta_m, a_cpu - b_cpu)
                        total_mem_delta_b, ov_m = _safe_add_delta(total_mem_delta_b, a_mem - b_mem)
                        cpu_delta_overflow = cpu_delta_overflow or ov_c
                        mem_delta_overflow = mem_delta_overflow or ov_m
                    else:
                        total_cpu_delta_m, ov_c = _safe_add_delta(total_cpu_delta_m, ev.cpu_delta_m)
                        total_mem_delta_b, ov_m = _safe_add_delta(total_mem_delta_b, ev.mem_delta_bytes)
                        cpu_delta_overflow = cpu_delta_overflow or ov_c
                        mem_delta_overflow = mem_delta_overflow or ov_m

                    if not ev.supported:
                        f_supported = False
                        all_supported = False
                        unsupported_resources += 1
                    f_risk = combine_risk(f_risk, ev.risk_level)
                    f_blast = combine_blast_radius(f_blast, ev.blast_radius)
                    f_revert = combine_revertability(f_revert, ev.revertability)
                    f_disrupt = combine_disruptiveness(f_disrupt, ev.disruptiveness)
                    reasons.extend(ev.reasons)
                    resource_verdicts.append(ev.to_verdict())

            if (
                f_category == "manifest"
                and f_effective_resources == 0
                and fc.change_type != CHANGE_RENAMED
                and fc.after_mode != _MODE_EXEC
            ):
                if fc.before_mode and fc.after_mode and fc.before_mode != fc.after_mode:
                    reasons.append(
                        Reason(
                            code="FILE_MODE_CHANGED",
                            dimension=DIM_INPUT_INTEGRITY,
                            severity=RISK_LOW,
                            message=f"manifest {norm_path!r} changes file mode {fc.before_mode} -> {fc.after_mode} without resource changes",
                            file=norm_path,
                        )
                    )
                else:
                    reasons.append(
                        Reason(
                            code="COMMENT_OR_WHITESPACE_ONLY",
                            dimension=DIM_BLAST_RADIUS,
                            severity=RISK_LOW,
                            message=f"manifest {norm_path!r} has only comments, whitespace, or key-order changes",
                            file=norm_path,
                        )
                    )

            file_verdicts.append(
                FileVerdict(
                    path=norm_path,
                    old_path=norm_old,
                    change_type=fc.change_type,
                    category=f_category,
                    risk_level=f_risk,
                    blast_radius=f_blast,
                    revertability=f_revert,
                    disruptiveness=f_disrupt,
                    supported=f_supported,
                )
            )
            overall_risk = combine_risk(overall_risk, f_risk)
            overall_blast = combine_blast_radius(overall_blast, f_blast)
            overall_revert = combine_revertability(overall_revert, f_revert)
            overall_disrupt = combine_disruptiveness(overall_disrupt, f_disrupt)
            continue

        # 2i. Everything else (code, scripts, unknown files, executable docs, docs inside sync trees)
        any_effective = True
        all_supported = False
        unsupported_files += 1
        sev = RISK_HIGH if fc.after_mode == _MODE_EXEC else RISK_UNKNOWN
        blast = BLAST_MEDIUM if fc.after_mode == _MODE_EXEC else BLAST_UNKNOWN
        rev = REVERT_UNKNOWN
        sec_reasons = _scan_raw_blob_secrets(fc.after, norm_path, is_binary=fc.is_binary)
        if sec_reasons:
            reasons.extend(sec_reasons)
            sev = combine_risk(sev, RISK_CRITICAL)
            blast = combine_blast_radius(blast, BLAST_HIGH)
            rev = combine_revertability(rev, REVERT_IRREVERSIBLE)
        if fc.after_mode == _MODE_EXEC:
            reasons.append(
                Reason(
                    code="EXECUTABLE_FILE",
                    dimension=DIM_SECURITY,
                    severity=RISK_HIGH,
                    message=f"file {norm_path!r} has executable mode 100755",
                    file=norm_path,
                )
            )
        reasons.append(
            Reason(
                code="UNSUPPORTED_FILE_TYPE",
                dimension=DIM_INPUT_INTEGRITY,
                severity=RISK_UNKNOWN,
                message=f"file {norm_path!r} is outside supported Kubernetes manifest types; defaulting to unknown",
                file=norm_path,
            )
        )
        file_verdicts.append(
            FileVerdict(
                path=norm_path,
                old_path=norm_old,
                change_type=fc.change_type,
                category="unsupported",
                risk_level=sev,
                blast_radius=blast,
                revertability=rev,
                disruptiveness=DISRUPT_UNKNOWN,
                supported=False,
            )
        )
        overall_risk = combine_risk(overall_risk, sev)
        overall_blast = combine_blast_radius(overall_blast, blast)
        overall_revert = combine_revertability(overall_revert, rev)
        overall_disrupt = combine_disruptiveness(overall_disrupt, DISRUPT_UNKNOWN)

    # Pass 3: PR-wide payload, blast-radius, and compound capacity caps
    if pr_payload_exceeded:
        all_supported = False
        overall_risk = combine_risk(overall_risk, RISK_UNKNOWN)
        overall_blast = combine_blast_radius(overall_blast, BLAST_UNKNOWN)
        overall_revert = combine_revertability(overall_revert, REVERT_UNKNOWN)
        overall_disrupt = combine_disruptiveness(overall_disrupt, DISRUPT_UNKNOWN)
        if not budget_exceeded_indices:
            reasons.append(
                Reason(
                    code="PR_PAYLOAD_TOO_LARGE",
                    dimension=DIM_INPUT_INTEGRITY,
                    severity=RISK_UNKNOWN,
                    message=f"total PR content ({total_raw_bytes} bytes) exceeds maximum limit ({MAX_TOTAL_BYTES} bytes)",
                )
            )

    total_objects = res_added + res_modified + res_deleted
    if total_objects > CRITICAL_OBJECT_THRESHOLD:
        overall_risk = combine_risk(overall_risk, RISK_CRITICAL)
        overall_blast = combine_blast_radius(overall_blast, BLAST_CRITICAL)
        reasons.append(
            Reason(
                code="BLAST_RADIUS_CRITICAL_CAP",
                dimension=DIM_BLAST_RADIUS,
                severity=RISK_CRITICAL,
                message=f"changes {total_objects} Kubernetes objects (exceeds critical cap of {CRITICAL_OBJECT_THRESHOLD})",
            )
        )
    elif total_objects > HIGH_OBJECT_THRESHOLD:
        overall_risk = combine_risk(overall_risk, RISK_HIGH)
        overall_blast = combine_blast_radius(overall_blast, BLAST_HIGH)
        reasons.append(
            Reason(
                code="BLAST_RADIUS_HIGH_CAP",
                dimension=DIM_BLAST_RADIUS,
                severity=RISK_HIGH,
                message=f"changes {total_objects} Kubernetes objects (exceeds high cap of {HIGH_OBJECT_THRESHOLD})",
            )
        )
    elif total_objects > MAX_LOW_OBJECTS:
        overall_risk = combine_risk(overall_risk, RISK_MEDIUM)
        overall_blast = combine_blast_radius(overall_blast, BLAST_MEDIUM)
        reasons.append(
            Reason(
                code="BLAST_RADIUS_MULTI_OBJECT",
                dimension=DIM_BLAST_RADIUS,
                severity=RISK_MEDIUM,
                message=f"changes {total_objects} Kubernetes objects (exceeds low-risk limit of {MAX_LOW_OBJECTS})",
            )
        )

    if raw_file_count > MAX_LOW_FILES and any_effective:
        overall_risk = combine_risk(overall_risk, RISK_MEDIUM)
        overall_blast = combine_blast_radius(overall_blast, BLAST_MEDIUM)
        reasons.append(
            Reason(
                code="BLAST_RADIUS_MULTI_FILE",
                dimension=DIM_BLAST_RADIUS,
                severity=RISK_MEDIUM,
                message=f"changes {raw_file_count} files (exceeds low-risk limit of {MAX_LOW_FILES})",
            )
        )

    if len(namespaces_touched) > HIGH_NAMESPACE_THRESHOLD:
        overall_risk = combine_risk(overall_risk, RISK_HIGH)
        overall_blast = combine_blast_radius(overall_blast, BLAST_HIGH)
        reasons.append(
            Reason(
                code="BLAST_RADIUS_MANY_NAMESPACES",
                dimension=DIM_BLAST_RADIUS,
                severity=RISK_HIGH,
                message=f"spans {len(namespaces_touched)} namespaces (exceeds high-risk cap of {HIGH_NAMESPACE_THRESHOLD})",
            )
        )
    elif len(namespaces_touched) > MAX_LOW_NAMESPACES:
        overall_risk = combine_risk(overall_risk, RISK_MEDIUM)
        overall_blast = combine_blast_radius(overall_blast, BLAST_MEDIUM)
        reasons.append(
            Reason(
                code="BLAST_RADIUS_MULTI_NAMESPACE",
                dimension=DIM_BLAST_RADIUS,
                severity=RISK_MEDIUM,
                message=f"spans {len(namespaces_touched)} namespaces (exceeds single-namespace low-risk bound)",
            )
        )

    if len(clusters_touched) > 3:
        overall_risk = combine_risk(overall_risk, RISK_CRITICAL)
        overall_blast = combine_blast_radius(overall_blast, BLAST_CRITICAL)
        reasons.append(
            Reason(
                code="MULTI_CLUSTER_FLEET_CHANGE",
                dimension=DIM_BLAST_RADIUS,
                severity=RISK_CRITICAL,
                message=f"spans {len(clusters_touched)} clusters ({', '.join(sorted(clusters_touched))})",
            )
        )
    elif len(clusters_touched) > 1:
        overall_risk = combine_risk(overall_risk, RISK_HIGH)
        overall_blast = combine_blast_radius(overall_blast, BLAST_HIGH)
        reasons.append(
            Reason(
                code="MULTI_CLUSTER_CHANGE",
                dimension=DIM_BLAST_RADIUS,
                severity=RISK_HIGH,
                message=f"spans multiple clusters ({', '.join(sorted(clusters_touched))})",
            )
        )

    cpu_fmt = "overflow" if cpu_delta_overflow else f"+{total_cpu_delta_m:.0f}m"
    mem_fmt = "overflow" if mem_delta_overflow else f"+{total_mem_delta_b / (1024**2):.0f}Mi"
    if (
        cpu_delta_overflow
        or mem_delta_overflow
        or total_cpu_delta_m > CRITICAL_COMPOUND_CPU_M
        or total_mem_delta_b > CRITICAL_COMPOUND_MEM_B
    ):
        overall_risk = combine_risk(overall_risk, RISK_CRITICAL)
        overall_blast = combine_blast_radius(overall_blast, BLAST_CRITICAL)
        reasons.append(
            Reason(
                code="COMPOUND_CAPACITY_CRITICAL_CAP",
                dimension=DIM_BLAST_RADIUS,
                severity=RISK_CRITICAL,
                message=f"aggregate PR capacity increase ({cpu_fmt} CPU, {mem_fmt} memory) exceeds critical cap",
            )
        )
    elif total_cpu_delta_m > HIGH_COMPOUND_CPU_M or total_mem_delta_b > HIGH_COMPOUND_MEM_B:
        overall_risk = combine_risk(overall_risk, RISK_HIGH)
        overall_blast = combine_blast_radius(overall_blast, BLAST_HIGH)
        reasons.append(
            Reason(
                code="COMPOUND_CAPACITY_HIGH_CAP",
                dimension=DIM_BLAST_RADIUS,
                severity=RISK_HIGH,
                message=f"aggregate PR capacity increase ({cpu_fmt} CPU, {mem_fmt} memory) exceeds high-risk cap",
            )
        )
    elif total_cpu_delta_m > MAX_LOW_COMPOUND_CPU_M or total_mem_delta_b > MAX_LOW_COMPOUND_MEM_B:
        overall_risk = combine_risk(overall_risk, RISK_MEDIUM)
        overall_blast = combine_blast_radius(overall_blast, BLAST_MEDIUM)
        reasons.append(
            Reason(
                code="COMPOUND_CAPACITY_INCREASE",
                dimension=DIM_BLAST_RADIUS,
                severity=RISK_MEDIUM,
                message=f"aggregate PR capacity increase ({cpu_fmt} CPU, {mem_fmt} memory) exceeds low-risk cap",
            )
        )

    # Deduplicate and deterministically sort reasons
    seen_reasons: set[tuple[str, str, str, str, tuple[str, str, str, str, str] | None]] = set()
    unique_reasons: list[Reason] = []
    for r in sorted(reasons, key=lambda item: item.sort_key()):
        res_sig = (
            (r.resource.group, r.resource.version, r.resource.kind, r.resource.namespace, r.resource.name)
            if r.resource is not None
            else None
        )
        sig = (r.code, r.file, r.field_path, r.message, res_sig)
        if sig not in seen_reasons:
            seen_reasons.add(sig)
            unique_reasons.append(r)

    return ClassificationResult(
        risk_level=overall_risk,
        blast_radius=overall_blast,
        revertability=overall_revert,
        disruptiveness=overall_disrupt,
        supported=all_supported,
        effective_changes=any_effective,
        reasons=tuple(unique_reasons),
        files=tuple(file_verdicts),
        resources=tuple(resource_verdicts),
        stats=PRStats(
            files_changed=raw_file_count,
            resources_added=res_added,
            resources_modified=res_modified,
            resources_deleted=res_deleted,
            namespaces_touched=len(namespaces_touched),
            clusters_touched=len(clusters_touched),
            unsupported_files=unsupported_files,
            unsupported_resources=unsupported_resources,
            parse_errors=parse_errors,
        ),
    )
