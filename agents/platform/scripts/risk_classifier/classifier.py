"""PR-level file triage, single-pass manifest classification, and blast-radius aggregation."""

from __future__ import annotations

import math
import posixpath
from typing import Any

from .k8s_rules import _is_production_labels, _is_production_path, evaluate_resource
from .parser import MAX_TOTAL_BYTES, ParseOutcome, ParsedResource, _strict_equal, parse_manifest_bytes
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
    DESTRUCTIVE_NO,
    DESTRUCTIVE_UNKNOWN,
    DESTRUCTIVE_YES,
    DIM_BLAST_RADIUS,
    DIM_DESTRUCTIVENESS,
    DIM_INPUT_INTEGRITY,
    DIM_REVERTABILITY,
    DIM_SECURITY,
    REVERT_DIFFICULT,
    REVERT_EASY,
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
    combine_destructiveness,
    combine_revertability,
    combine_risk,
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

# Critical governance / CI / agent control-plane paths (all checked case-insensitively)
_CRITICAL_DIR_PREFIXES = (
    ".kube-agents/",
    ".github/workflows/",
    ".github/actions/",
    ".tekton/",
    ".circleci/",
    ".buildkite/",
    ".claude/",
)

_CRITICAL_EXACT_PATHS = frozenset(
    {
        ".github/dependabot.yml",
        ".github/dependabot.yaml",
        ".github/copilot-instructions.md",
        ".gitlab-ci.yml",
        ".gitlab-ci.yaml",
    }
)

_CRITICAL_BASENAMES = frozenset(
    {
        "codeowners",
        "owners",
        "owners_aliases",
        ".sops.yaml",
        ".sops.yml",
        ".gitmodules",
        ".gitattributes",
        "renovate.json",
        "renovate.json5",
        ".renovaterc",
        ".renovaterc.json",
        "agents.md",
        "claude.md",
        "gemini.md",
        "skill.md",
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
        "taskfile.yml",
        "taskfile.yaml",
        "go.mod",
        "go.sum",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "requirements.txt",
        "pyproject.toml",
        "poetry.lock",
        "pipfile",
        "pipfile.lock",
        "cargo.toml",
        "cargo.lock",
    }
)

# Unrendered Helm / Kustomize packaging files -> `unknown`
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
        "kptfile",
        "values.yaml",
        "values.yml",
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
)

_DOC_EXTENSIONS = frozenset({".md", ".rst", ".adoc"})
_MANIFEST_EXTENSIONS = frozenset({".yaml", ".yml", ".json"})
_OVERLAY_SEGMENTS = frozenset({"patches", "overlays", "templates"})
_TERRAFORM_EXTENSIONS = frozenset({".tf", ".tfvars", ".hcl"})

_MODE_SYMLINK = "120000"
_MODE_GITLINK = "160000"
_MODE_EXEC = "100755"


def _normalize_path(raw: str) -> tuple[str, bool]:
    """Normalize a repo-relative POSIX path; returns `(normalized, ok)`."""
    if not raw or "\x00" in raw or "\\" in raw:
        return raw, False
    if raw.startswith("/"):
        return raw, False
    norm = posixpath.normpath(raw)
    if norm in (".", "..") or norm.startswith("../"):
        return norm, False
    return norm, True


def _cluster_from_path(norm_path: str) -> str:
    parts = norm_path.split("/")
    if len(parts) >= 3 and parts[0] == "clusters" and parts[1]:
        return parts[1]
    return ""


def _is_critical_governance_path(norm_path: str) -> bool:
    low = norm_path.lower()
    base = posixpath.basename(low)
    if low in _CRITICAL_EXACT_PATHS:
        return True
    if any(low.startswith(pfx) or f"/{pfx}" in low for pfx in _CRITICAL_DIR_PREFIXES):
        return True
    if base in _CRITICAL_BASENAMES:
        return True
    if base.startswith("jenkinsfile") or base.startswith("cloudbuild."):
        return True
    return False


def _is_supply_chain_path(norm_path: str) -> bool:
    base = posixpath.basename(norm_path.lower())
    if base in _SUPPLY_CHAIN_BASENAMES:
        return True
    if base.startswith(("dockerfile.", "containerfile.", "requirements-")) or base.endswith(".dockerfile"):
        return True
    return False


def _is_unrendered_package_path(norm_path: str) -> bool:
    low = norm_path.lower()
    base = posixpath.basename(low)
    if base in _UNRENDERED_PACKAGE_BASENAMES:
        return True
    if base.startswith(("values.", "values-")) and base.endswith((".yaml", ".yml")):
        return True
    parts = low.split("/")[:-1]
    if any(seg in _OVERLAY_SEGMENTS for seg in parts):
        return True
    return False


def _is_pure_doc_path(norm_path: str) -> bool:
    low = norm_path.lower()
    if any(low.startswith(pfx) for pfx in _SYNC_ROOT_PREFIXES):
        return False
    if low.startswith(".") or "/." in low:
        return False
    base = posixpath.basename(low)
    if base.startswith("license"):
        return True
    _, ext = posixpath.splitext(base)
    return ext in _DOC_EXTENSIONS


def _path_bucket(norm_path: str) -> str:
    if _is_critical_governance_path(norm_path):
        return "governance"
    if _is_supply_chain_path(norm_path):
        return "supply_chain"
    if _is_unrendered_package_path(norm_path):
        return "unrendered_package"
    if _is_pure_doc_path(norm_path):
        return "documentation"
    low = norm_path.lower()
    _, ext = posixpath.splitext(low)
    if ext in _TERRAFORM_EXTENSIONS or low.endswith(".tf.json"):
        return "terraform"
    if ext in _MANIFEST_EXTENSIONS:
        return "manifest"
    return "other"


def _sync_root(norm_path: str) -> str:
    low = norm_path.lower()
    for pfx in _SYNC_ROOT_PREFIXES:
        if low.startswith(pfx):
            parts = low.split("/")
            return "/".join(parts[:2]) if len(parts) >= 2 else pfx.rstrip("/")
    return ""


def _expand_cross_boundary_renames(changes: list[FileChange]) -> list[FileChange]:
    """Split renames crossing category, cluster, sync-tree, or prod/non-prod boundaries into delete + add."""
    expanded: list[FileChange] = []
    for fc in changes:
        if not fc.old_path or fc.old_path == fc.path:
            expanded.append(fc)
            continue
        new_norm, new_ok = _normalize_path(fc.path)
        old_norm, old_ok = _normalize_path(fc.old_path)
        if not new_ok or not old_ok:
            expanded.append(fc)
            continue
        old_sig = (
            _path_bucket(old_norm),
            _cluster_from_path(old_norm),
            _sync_root(old_norm),
            _is_production_path(old_norm),
        )
        new_sig = (
            _path_bucket(new_norm),
            _cluster_from_path(new_norm),
            _sync_root(new_norm),
            _is_production_path(new_norm),
        )
        if old_sig != new_sig:
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


def _merge_ns_labels_monotonic(existing: dict[str, Any], incoming: dict[str, Any], *, from_before: bool) -> dict[str, Any]:
    """Merge Namespace labels so an `after` edit can add prod protection but never strip it."""
    merged = dict(existing)
    for k, v in incoming.items():
        if from_before or k not in merged:
            merged[k] = v
        elif _is_production_labels({k: v}) and not _is_production_labels({k: merged[k]}):
            merged[k] = v
    return merged


def _group_resources(resources: tuple[ParsedResource, ...]) -> tuple[dict[tuple[str, str, str, str], ParsedResource], bool]:
    """Map resources by identity key; returns `(mapping, had_duplicate)`."""
    out: dict[tuple[str, str, str, str], ParsedResource] = {}
    for res in resources:
        key = res.identity_key
        if key in out:
            return out, True
        out[key] = res
    return out, False


def classify_changes(changes: list[FileChange]) -> ClassificationResult:
    """Deterministically classify a list of changed PR files across all 4 risk axes."""
    if not changes:
        return unknown_result("EMPTY_PR_DIFF", "pull request contains no changed files")

    raw_file_count = len(changes)
    expanded = _expand_cross_boundary_renames(changes)

    # Sort changes deterministically by path
    ordered = sorted(expanded, key=lambda c: (c.path, c.old_path, c.change_type))

    # Enforce PR-wide total byte bound
    total_bytes = sum(len(c.before or b"") + len(c.after or b"") for c in ordered)
    if total_bytes > MAX_TOTAL_BYTES:
        return unknown_result(
            "PR_PAYLOAD_TOO_LARGE",
            f"total PR content ({total_bytes} bytes) exceeds maximum limit ({MAX_TOTAL_BYTES} bytes)",
            files_changed=raw_file_count,
        )

    # Pass 1: normalize paths and single-pass parse all candidate K8s manifests so
    # Namespace labels in the same PR are available to every resource in Pass 2.
    norm_entries: list[tuple[FileChange, str, str, bool]] = []
    parsed_cache: dict[int, tuple[ParseOutcome, ParseOutcome]] = {}
    pr_ns_labels: dict[str, dict[str, Any]] = {}

    for idx, fc in enumerate(ordered):
        norm_path, path_ok = _normalize_path(fc.path)
        norm_old = ""
        if fc.old_path:
            norm_old, old_ok = _normalize_path(fc.old_path)
            path_ok = path_ok and old_ok
        norm_entries.append((fc, norm_path, norm_old, path_ok))
        if not path_ok:
            continue
        if fc.is_binary or fc.oversized or fc.integrity_error:
            continue
        if _is_critical_governance_path(norm_path) or (norm_old and _is_critical_governance_path(norm_old)):
            continue
        if _is_supply_chain_path(norm_path) or _is_unrendered_package_path(norm_path):
            continue
        _, ext = posixpath.splitext(norm_path.lower())
        if ext in _MANIFEST_EXTENSIONS:
            is_json = ext == ".json"
            b_out = parse_manifest_bytes(fc.before, is_json=is_json)
            a_out = parse_manifest_bytes(fc.after, is_json=is_json)
            parsed_cache[idx] = (b_out, a_out)
            for from_before, out in ((True, b_out), (False, a_out)):
                for res in out.resources:
                    if res.ref.group == "" and res.ref.kind == "Namespace":
                        meta = res.obj.get("metadata")
                        lbls = meta.get("labels") if isinstance(meta, dict) else None
                        if isinstance(lbls, dict):
                            pr_ns_labels[res.ref.name] = _merge_ns_labels_monotonic(
                                pr_ns_labels.get(res.ref.name, {}),
                                lbls,
                                from_before=from_before,
                            )

    # Pass 2: evaluate every file cleanly
    overall_risk = RISK_LOW
    overall_blast = BLAST_LOW
    overall_revert = REVERT_EASY
    overall_destruct = DESTRUCTIVE_NO
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

    for idx, (fc, norm_path, norm_old, path_ok) in enumerate(norm_entries):
        if not path_ok:
            all_supported = False
            unsupported_files += 1
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
                    risk_level=RISK_HIGH,
                    blast_radius=BLAST_UNKNOWN,
                    revertability=REVERT_UNKNOWN,
                    destructiveness=DESTRUCTIVE_UNKNOWN,
                    supported=False,
                )
            )
            overall_risk = combine_risk(overall_risk, RISK_HIGH)
            overall_blast = combine_blast_radius(overall_blast, BLAST_UNKNOWN)
            overall_revert = combine_revertability(overall_revert, REVERT_UNKNOWN)
            overall_destruct = combine_destructiveness(overall_destruct, DESTRUCTIVE_UNKNOWN)
            continue

        cluster = _cluster_from_path(norm_path)
        if cluster:
            clusters_touched.add(cluster)
        if norm_old:
            old_cluster = _cluster_from_path(norm_old)
            if old_cluster:
                clusters_touched.add(old_cluster)

        modes = {fc.before_mode, fc.after_mode}
        in_sync_tree = any(norm_path.lower().startswith(pfx) for pfx in _SYNC_ROOT_PREFIXES)
        is_gov = _is_critical_governance_path(norm_path) or (bool(norm_old) and _is_critical_governance_path(norm_old))

        # 2a. Critical governance / CI / agent control-plane files
        if is_gov:
            any_effective = True
            dest = DESTRUCTIVE_YES if fc.change_type == CHANGE_DELETED else DESTRUCTIVE_NO
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
                    revertability=REVERT_DIFFICULT,
                    destructiveness=dest,
                    supported=True,
                )
            )
            overall_risk = combine_risk(overall_risk, RISK_CRITICAL)
            overall_blast = combine_blast_radius(overall_blast, BLAST_CRITICAL)
            overall_revert = combine_revertability(overall_revert, REVERT_DIFFICULT)
            overall_destruct = combine_destructiveness(overall_destruct, dest)
            continue

        # 2b. Symlinks and git submodules
        if _MODE_SYMLINK in modes or _MODE_GITLINK in modes:
            any_effective = True
            all_supported = False
            unsupported_files += 1
            is_link = _MODE_SYMLINK in modes
            code = "SYMLINK_CHANGE" if is_link else "SUBMODULE_CHANGE"
            sev = RISK_HIGH if in_sync_tree else RISK_UNKNOWN
            reasons.append(
                Reason(
                    code=code,
                    dimension=DIM_SECURITY if in_sync_tree else DIM_INPUT_INTEGRITY,
                    severity=sev,
                    message=f"{'symlink' if is_link else 'git submodule'} change at {norm_path!r} cannot be verified offline",
                    file=norm_path,
                )
            )
            file_verdicts.append(
                FileVerdict(
                    path=norm_path,
                    old_path=norm_old,
                    change_type=fc.change_type,
                    category="symlink" if is_link else "submodule",
                    risk_level=sev,
                    blast_radius=BLAST_HIGH if in_sync_tree else BLAST_UNKNOWN,
                    revertability=REVERT_UNKNOWN,
                    destructiveness=DESTRUCTIVE_UNKNOWN,
                    supported=False,
                )
            )
            overall_risk = combine_risk(overall_risk, sev)
            overall_blast = combine_blast_radius(overall_blast, BLAST_HIGH if in_sync_tree else BLAST_UNKNOWN)
            overall_revert = combine_revertability(overall_revert, REVERT_UNKNOWN)
            overall_destruct = combine_destructiveness(overall_destruct, DESTRUCTIVE_UNKNOWN)
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
                    destructiveness=DESTRUCTIVE_UNKNOWN,
                    supported=False,
                )
            )
            overall_risk = combine_risk(overall_risk, RISK_HIGH)
            overall_blast = combine_blast_radius(overall_blast, BLAST_UNKNOWN)
            overall_revert = combine_revertability(overall_revert, REVERT_UNKNOWN)
            overall_destruct = combine_destructiveness(overall_destruct, DESTRUCTIVE_UNKNOWN)
            continue

        # 2c. Oversized or binary files -> `unknown`
        if fc.oversized or fc.is_binary:
            any_effective = True
            all_supported = False
            unsupported_files += 1
            code = "OVERSIZED_FILE" if fc.oversized else "BINARY_FILE"
            msg = (
                f"file {norm_path!r} exceeds size limit and cannot be inspected"
                if fc.oversized
                else f"binary file {norm_path!r} cannot be inspected deterministically"
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
                    category="binary" if fc.is_binary else "oversized",
                    risk_level=RISK_UNKNOWN,
                    blast_radius=BLAST_UNKNOWN,
                    revertability=REVERT_UNKNOWN,
                    destructiveness=DESTRUCTIVE_UNKNOWN,
                    supported=False,
                )
            )
            overall_risk = combine_risk(overall_risk, RISK_UNKNOWN)
            overall_blast = combine_blast_radius(overall_blast, BLAST_UNKNOWN)
            overall_revert = combine_revertability(overall_revert, REVERT_UNKNOWN)
            overall_destruct = combine_destructiveness(overall_destruct, DESTRUCTIVE_UNKNOWN)
            continue

        # 2d. Supply-chain / container / dependency build files -> `high`
        if _is_supply_chain_path(norm_path):
            any_effective = True
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
                    risk_level=RISK_HIGH,
                    blast_radius=BLAST_HIGH,
                    revertability=REVERT_EASY,
                    destructiveness=DESTRUCTIVE_UNKNOWN,
                    supported=True,
                )
            )
            overall_risk = combine_risk(overall_risk, RISK_HIGH)
            overall_blast = combine_blast_radius(overall_blast, BLAST_HIGH)
            overall_revert = combine_revertability(overall_revert, REVERT_EASY)
            overall_destruct = combine_destructiveness(overall_destruct, DESTRUCTIVE_UNKNOWN)
            continue

        # 2e. Unrendered Helm / Kustomize packaging files -> `unknown`
        if _is_unrendered_package_path(norm_path):
            any_effective = True
            all_supported = False
            unsupported_files += 1
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
                    risk_level=RISK_UNKNOWN,
                    blast_radius=BLAST_UNKNOWN,
                    revertability=REVERT_UNKNOWN,
                    destructiveness=DESTRUCTIVE_UNKNOWN,
                    supported=False,
                )
            )
            overall_risk = combine_risk(overall_risk, RISK_UNKNOWN)
            overall_blast = combine_blast_radius(overall_blast, BLAST_UNKNOWN)
            overall_revert = combine_revertability(overall_revert, REVERT_UNKNOWN)
            overall_destruct = combine_destructiveness(overall_destruct, DESTRUCTIVE_UNKNOWN)
            continue

        # 2f. Pure non-agent documentation outside sync trees -> `low` (not runtime effective)
        if _is_pure_doc_path(norm_path):
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
                    destructiveness=DESTRUCTIVE_NO,
                    supported=True,
                )
            )
            continue

        # 2g. Kubernetes manifests (`.yaml`, `.yml`, `.json`)
        _, ext = posixpath.splitext(norm_path.lower())
        if idx in parsed_cache:
            b_out, a_out = parsed_cache[idx]

            f_risk = RISK_LOW
            f_blast = BLAST_LOW
            f_revert = REVERT_EASY
            f_destruct = DESTRUCTIVE_NO
            f_supported = True
            f_category = "manifest"
            f_effective_resources = 0

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

            # Parse errors -> `unknown` (still evaluate any resources parsed before the error
            # so a benign error in doc 2 cannot mask a critical resource in doc 1)
            err_out = a_out if a_out.error else (b_out if b_out.error else None)
            if err_out is not None:
                any_effective = True
                all_supported = False
                f_supported = False
                unsupported_files += 1
                parse_errors += 1
                f_category = "invalid_manifest"
                reasons.append(
                    Reason(
                        code=err_out.error_code or "MANIFEST_PARSE_ERROR",
                        dimension=DIM_INPUT_INTEGRITY,
                        severity=RISK_UNKNOWN,
                        message=f"could not safely parse {norm_path!r}: {err_out.error}",
                        file=norm_path,
                    )
                )
                f_risk = combine_risk(f_risk, RISK_UNKNOWN)
                f_blast = combine_blast_radius(f_blast, BLAST_UNKNOWN)
                f_revert = combine_revertability(f_revert, REVERT_UNKNOWN)
                f_destruct = combine_destructiveness(f_destruct, DESTRUCTIVE_UNKNOWN)

            # Unrendered templates / Kustomize files discovered by content:
            # always combine with any valid embedded resources so `${HARMLESS}` in an
            # annotation can NEVER downgrade a critical/high K8s change to `unknown`.
            if b_out.is_template or a_out.is_template or b_out.is_kustomize or a_out.is_kustomize:
                any_effective = True
                all_supported = False
                f_supported = False
                if err_out is None:
                    unsupported_files += 1
                    f_category = "unrendered_package"
                is_tmpl = b_out.is_template or a_out.is_template
                code = "UNRENDERED_TEMPLATE" if is_tmpl else "UNRENDERED_KUSTOMIZE"
                reasons.append(
                    Reason(
                        code=code,
                        dimension=DIM_INPUT_INTEGRITY,
                        severity=RISK_UNKNOWN,
                        message=f"file {norm_path!r} contains unrendered {'template syntax' if is_tmpl else 'Kustomize manifest'}",
                        file=norm_path,
                    )
                )
                f_risk = combine_risk(f_risk, RISK_UNKNOWN)
                f_blast = combine_blast_radius(f_blast, BLAST_UNKNOWN)
                f_revert = combine_revertability(f_revert, REVERT_UNKNOWN)
                f_destruct = combine_destructiveness(f_destruct, DESTRUCTIVE_UNKNOWN)

            # Valid non-Kubernetes YAML/JSON (e.g. `.golangci.yml`, `tsconfig.json`) -> `unknown`
            is_non_k8s = (
                err_out is None
                and not (b_out.is_template or a_out.is_template or b_out.is_kustomize or a_out.is_kustomize)
                and (not b_out.is_k8s_manifest or not a_out.is_k8s_manifest)
            )
            if is_non_k8s:
                any_effective = True
                all_supported = False
                f_supported = False
                unsupported_files += 1
                f_category = "non_k8s_config"
                reasons.append(
                    Reason(
                        code="NON_K8S_STRUCTURED_FILE",
                        dimension=DIM_INPUT_INTEGRITY,
                        severity=RISK_UNKNOWN,
                        message=f"structured file {norm_path!r} is not a Kubernetes manifest",
                        file=norm_path,
                    )
                )
                f_risk = combine_risk(f_risk, RISK_UNKNOWN)
                f_blast = combine_blast_radius(f_blast, BLAST_UNKNOWN)
                f_revert = combine_revertability(f_revert, REVERT_UNKNOWN)
                f_destruct = combine_destructiveness(f_destruct, DESTRUCTIVE_UNKNOWN)

            # Match and evaluate any parsed Kubernetes resources (even alongside template/error flags)
            b_map, b_dup = _group_resources(b_out.resources)
            a_map, a_dup = _group_resources(a_out.resources)
            if b_dup or a_dup:
                any_effective = True
                all_supported = False
                f_supported = False
                if f_category == "manifest":
                    unsupported_files += 1
                    f_category = "invalid_manifest"
                parse_errors += 1
                reasons.append(
                    Reason(
                        code="DUPLICATE_RESOURCE_IDENTITY",
                        dimension=DIM_INPUT_INTEGRITY,
                        severity=RISK_UNKNOWN,
                        message=f"manifest {norm_path!r} defines duplicate Kubernetes resource identities",
                        file=norm_path,
                    )
                )
                f_risk = combine_risk(f_risk, RISK_UNKNOWN)
                f_blast = combine_blast_radius(f_blast, BLAST_UNKNOWN)
                f_revert = combine_revertability(f_revert, REVERT_UNKNOWN)
                f_destruct = combine_destructiveness(f_destruct, DESTRUCTIVE_UNKNOWN)
            elif b_map or a_map:
                if fc.change_type == CHANGE_RENAMED:
                    any_effective = True
                    f_risk = combine_risk(f_risk, RISK_MEDIUM)
                    f_revert = combine_revertability(f_revert, REVERT_MODERATE)
                    reasons.append(
                        Reason(
                            code="MANIFEST_FILE_RENAMED",
                            dimension=DIM_REVERTABILITY,
                            severity=RISK_MEDIUM,
                            message=f"renames manifest {norm_old!r} -> {norm_path!r}",
                            file=norm_path,
                        )
                    )

                for key in sorted(set(b_map.keys()) | set(a_map.keys())):
                    b_res = b_map.get(key)
                    a_res = a_map.get(key)
                    if b_res is not None and a_res is not None and _strict_equal(b_res.obj, a_res.obj):
                        continue  # Unchanged resource in multi-doc manifest
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

                    ev = evaluate_resource(norm_path, action, b_res, a_res, ns_labels=pr_ns_labels)
                    if ev.ref.namespace:
                        namespaces_touched.add(ev.ref.namespace)
                    if math.isfinite(ev.cpu_delta_m):
                        total_cpu_delta_m += ev.cpu_delta_m
                    if math.isfinite(ev.mem_delta_bytes):
                        total_mem_delta_b += ev.mem_delta_bytes
                    if not ev.supported:
                        f_supported = False
                        all_supported = False
                        unsupported_resources += 1
                    f_risk = combine_risk(f_risk, ev.risk_level)
                    f_blast = combine_blast_radius(f_blast, ev.blast_radius)
                    f_revert = combine_revertability(f_revert, ev.revertability)
                    f_destruct = combine_destructiveness(f_destruct, ev.destructiveness)
                    reasons.extend(ev.reasons)
                    resource_verdicts.append(ev.to_verdict())

            if (
                f_category == "manifest"
                and f_effective_resources == 0
                and fc.change_type != CHANGE_RENAMED
                and fc.after_mode != _MODE_EXEC
            ):
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
                    destructiveness=f_destruct,
                    supported=f_supported,
                )
            )
            overall_risk = combine_risk(overall_risk, f_risk)
            overall_blast = combine_blast_radius(overall_blast, f_blast)
            overall_revert = combine_revertability(overall_revert, f_revert)
            overall_destruct = combine_destructiveness(overall_destruct, f_destruct)
            continue

        # 2h. Terraform / OpenTofu files
        if ext in _TERRAFORM_EXTENSIONS or norm_path.lower().endswith(".tf.json"):
            any_effective = True
            all_supported = False
            unsupported_files += 1
            if fc.change_type == CHANGE_DELETED:
                sev, dest = RISK_HIGH, DESTRUCTIVE_YES
                msg = f"deletes Terraform/HCL infrastructure file {norm_path!r} (can destroy cloud resources on apply)"
            else:
                sev, dest = RISK_UNKNOWN, DESTRUCTIVE_UNKNOWN
                msg = f"Terraform/HCL file {norm_path!r} requires a verified plan to determine blast radius"
            reasons.append(
                Reason(
                    code="UNSUPPORTED_TERRAFORM_FILE",
                    dimension=DIM_INPUT_INTEGRITY,
                    severity=sev,
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
                    blast_radius=BLAST_UNKNOWN,
                    revertability=REVERT_UNKNOWN,
                    destructiveness=dest,
                    supported=False,
                )
            )
            overall_risk = combine_risk(overall_risk, sev)
            overall_blast = combine_blast_radius(overall_blast, BLAST_UNKNOWN)
            overall_revert = combine_revertability(overall_revert, REVERT_UNKNOWN)
            overall_destruct = combine_destructiveness(overall_destruct, dest)
            continue

        # 2i. Everything else (code, scripts, unknown files, docs inside sync trees) -> `unknown`
        any_effective = True
        all_supported = False
        unsupported_files += 1
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
                risk_level=RISK_UNKNOWN,
                blast_radius=BLAST_UNKNOWN,
                revertability=REVERT_UNKNOWN,
                destructiveness=DESTRUCTIVE_UNKNOWN,
                supported=False,
            )
        )
        overall_risk = combine_risk(overall_risk, RISK_UNKNOWN)
        overall_blast = combine_blast_radius(overall_blast, BLAST_UNKNOWN)
        overall_revert = combine_revertability(overall_revert, REVERT_UNKNOWN)
        overall_destruct = combine_destructiveness(overall_destruct, DESTRUCTIVE_UNKNOWN)

    # Pass 3: PR-wide blast-radius and compound capacity caps
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

    if total_cpu_delta_m > MAX_LOW_COMPOUND_CPU_M or total_mem_delta_b > MAX_LOW_COMPOUND_MEM_B:
        overall_risk = combine_risk(overall_risk, RISK_MEDIUM)
        overall_blast = combine_blast_radius(overall_blast, BLAST_MEDIUM)
        reasons.append(
            Reason(
                code="COMPOUND_CAPACITY_INCREASE",
                dimension=DIM_BLAST_RADIUS,
                severity=RISK_MEDIUM,
                message=f"aggregate PR capacity increase (+{total_cpu_delta_m:.0f}m CPU, +{total_mem_delta_b / (1024**2):.0f}Mi memory) exceeds low-risk cap",
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
        destructiveness=overall_destruct,
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
