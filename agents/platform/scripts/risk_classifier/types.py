"""Core types, enums, and anti-masking aggregation lattices for PR risk classification."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

SCHEMA_VERSION = 2

# Risk levels
RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"
RISK_CRITICAL = "critical"
RISK_UNKNOWN = "unknown"

RISK_LEVELS = (RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL, RISK_UNKNOWN)

# Policy-eligible ordering for concrete risk levels. `unknown` is intentionally
# absent so policy comparisons can never accidentally treat `unknown` as low/medium.
CONCRETE_RISK_ORDER: Mapping[str, int] = MappingProxyType(
    {
        RISK_LOW: 0,
        RISK_MEDIUM: 1,
        RISK_HIGH: 2,
        RISK_CRITICAL: 3,
    }
)

# Anti-masking lattice for aggregating risk across files/resources:
#   low < medium < unknown < high < critical
# Unknown prevents claiming low/medium when any part of the PR is unsupported,
# while known high/critical can never be downgraded to unknown by adding an
# unsupported file alongside a dangerous change.
_RISK_COMBINE_RANK: Mapping[str, int] = MappingProxyType(
    {
        RISK_LOW: 0,
        RISK_MEDIUM: 1,
        RISK_UNKNOWN: 2,
        RISK_HIGH: 3,
        RISK_CRITICAL: 4,
    }
)

# Blast radius levels (scope of impact)
BLAST_LOW = "low"
BLAST_MEDIUM = "medium"
BLAST_HIGH = "high"
BLAST_CRITICAL = "critical"
BLAST_UNKNOWN = "unknown"

BLAST_LEVELS = (BLAST_LOW, BLAST_MEDIUM, BLAST_HIGH, BLAST_CRITICAL, BLAST_UNKNOWN)

_BLAST_COMBINE_RANK: Mapping[str, int] = MappingProxyType(
    {
        BLAST_LOW: 0,
        BLAST_MEDIUM: 1,
        BLAST_UNKNOWN: 2,
        BLAST_HIGH: 3,
        BLAST_CRITICAL: 4,
    }
)

# Revertability levels
REVERT_EASY = "easy"
REVERT_MODERATE = "moderate"
REVERT_DIFFICULT = "difficult"
REVERT_IRREVERSIBLE = "irreversible"
REVERT_UNKNOWN = "unknown"

REVERTABILITY_LEVELS = (
    REVERT_EASY,
    REVERT_MODERATE,
    REVERT_DIFFICULT,
    REVERT_IRREVERSIBLE,
    REVERT_UNKNOWN,
)

# Anti-masking revertability lattice:
#   easy < moderate < difficult < unknown < irreversible
# Unknown dominates easy/moderate/difficult so an unmodeled change can never be
# masked as moderate by a nearby rename; irreversible dominates everything.
_REVERT_COMBINE_RANK: Mapping[str, int] = MappingProxyType(
    {
        REVERT_EASY: 0,
        REVERT_MODERATE: 1,
        REVERT_DIFFICULT: 2,
        REVERT_UNKNOWN: 3,
        REVERT_IRREVERSIBLE: 4,
    }
)

# Disruptiveness levels (separating live outage/disruption from data/resource destruction)
DISRUPT_NONE = "non_disruptive"
DISRUPT_DISRUPTIVE = "disruptive"
DISRUPT_DESTRUCTIVE = "destructive"
DISRUPT_UNKNOWN = "unknown"

DISRUPTIVENESS_LEVELS = (
    DISRUPT_NONE,
    DISRUPT_DISRUPTIVE,
    DISRUPT_DESTRUCTIVE,
    DISRUPT_UNKNOWN,
)

# Anti-masking disruptiveness lattice:
#   non_disruptive < unknown < disruptive < destructive
# Unknown prevents claiming non_disruptive when any file is unsupported, while
# known disruptive/destructive impact can never be downgraded to unknown.
_DISRUPT_COMBINE_RANK: Mapping[str, int] = MappingProxyType(
    {
        DISRUPT_NONE: 0,
        DISRUPT_UNKNOWN: 1,
        DISRUPT_DISRUPTIVE: 2,
        DISRUPT_DESTRUCTIVE: 3,
    }
)

# Reason dimensions
DIM_BLAST_RADIUS = "blast_radius"
DIM_DISRUPTIVENESS = "disruptiveness"
DIM_REVERTABILITY = "revertability"
DIM_SECURITY = "security"
DIM_INPUT_INTEGRITY = "input_integrity"
DIM_POLICY = "policy"

REASON_DIMENSIONS = (
    DIM_BLAST_RADIUS,
    DIM_DISRUPTIVENESS,
    DIM_REVERTABILITY,
    DIM_SECURITY,
    DIM_INPUT_INTEGRITY,
    DIM_POLICY,
)

# File change types
CHANGE_ADDED = "added"
CHANGE_MODIFIED = "modified"
CHANGE_DELETED = "deleted"
CHANGE_RENAMED = "renamed"

CHANGE_TYPES = (CHANGE_ADDED, CHANGE_MODIFIED, CHANGE_DELETED, CHANGE_RENAMED)

# Resource action types
ACTION_ADD = "add"
ACTION_MODIFY = "modify"
ACTION_DELETE = "delete"

RESOURCE_ACTIONS = (ACTION_ADD, ACTION_MODIFY, ACTION_DELETE)

# Deterministic risk labels (broker-owned, reserved against caller writes)
RISK_LABEL_PREFIX = "risk:"
RISK_LABELS = tuple(f"{RISK_LABEL_PREFIX}{level}" for level in RISK_LEVELS)

RISK_LABEL_COLORS: Mapping[str, str] = MappingProxyType(
    {
        "risk:low": "0e8a16",
        "risk:medium": "fbca04",
        "risk:high": "d93f0b",
        "risk:critical": "b60205",
        "risk:unknown": "cfd3d7",
    }
)

RISK_LABEL_DESCRIPTIONS: Mapping[str, str] = MappingProxyType(
    {
        "risk:low": "kube-agents deterministic PR risk: low blast radius, non-disruptive, easy revert",
        "risk:medium": "kube-agents deterministic PR risk: moderate impact requiring review or dev policy",
        "risk:high": "kube-agents deterministic PR risk: high impact (security, disruption, deletion, or cluster scope)",
        "risk:critical": "kube-agents deterministic PR risk: critical control-plane, RBAC, governance, or data-loss risk",
        "risk:unknown": "kube-agents deterministic PR risk: unsupported or unverifiable PR content (auto-merge blocked)",
    }
)


def _norm_level(value: Any, rank_map: Mapping[str, int], fallback: str) -> str:
    if isinstance(value, str) and value in rank_map:
        return value
    return fallback


def risk_label_for(risk_level: str) -> str:
    """Return the canonical `risk:<level>` label for `risk_level`."""
    level = _norm_level(risk_level, _RISK_COMBINE_RANK, RISK_UNKNOWN)
    return f"{RISK_LABEL_PREFIX}{level}"


def combine_risk(a: str, b: str) -> str:
    """Combine two risk levels using the anti-masking lattice."""
    na = _norm_level(a, _RISK_COMBINE_RANK, RISK_UNKNOWN)
    nb = _norm_level(b, _RISK_COMBINE_RANK, RISK_UNKNOWN)
    return nb if _RISK_COMBINE_RANK[nb] > _RISK_COMBINE_RANK[na] else na


def combine_blast_radius(a: str, b: str) -> str:
    """Combine two blast radius levels using the anti-masking lattice."""
    na = _norm_level(a, _BLAST_COMBINE_RANK, BLAST_UNKNOWN)
    nb = _norm_level(b, _BLAST_COMBINE_RANK, BLAST_UNKNOWN)
    return nb if _BLAST_COMBINE_RANK[nb] > _BLAST_COMBINE_RANK[na] else na


def combine_revertability(a: str, b: str) -> str:
    """Combine two revertability levels using the anti-masking lattice."""
    na = _norm_level(a, _REVERT_COMBINE_RANK, REVERT_UNKNOWN)
    nb = _norm_level(b, _REVERT_COMBINE_RANK, REVERT_UNKNOWN)
    return nb if _REVERT_COMBINE_RANK[nb] > _REVERT_COMBINE_RANK[na] else na


def combine_disruptiveness(a: str, b: str) -> str:
    """Combine two disruptiveness values (`destructive > disruptive > unknown > non_disruptive`)."""
    na = _norm_level(a, _DISRUPT_COMBINE_RANK, DISRUPT_UNKNOWN)
    nb = _norm_level(b, _DISRUPT_COMBINE_RANK, DISRUPT_UNKNOWN)
    return nb if _DISRUPT_COMBINE_RANK[nb] > _DISRUPT_COMBINE_RANK[na] else na


def escalate_risk(level: str, *, high_impact: bool = False) -> str:
    """Escalate a concrete risk level by one step for production environments.

    `low -> medium`, `medium -> high`, and `high -> critical` when disruptive,
    destructive, irreversible, or security-sensitive. `unknown` and `critical` stay as-is;
    unrecognized inputs fail closed to `unknown`.
    """
    norm = _norm_level(level, _RISK_COMBINE_RANK, RISK_UNKNOWN)
    if norm == RISK_LOW:
        return RISK_MEDIUM
    if norm == RISK_MEDIUM:
        return RISK_HIGH
    if norm == RISK_HIGH and high_impact:
        return RISK_CRITICAL
    return norm


def _require_choice(value: Any, allowed: tuple[str, ...], field_name: str) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError(f"invalid {field_name}: {value!r} (expected one of {allowed})")


def _require_bool(value: Any, field_name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a bool, got {type(value).__name__}")


def _require_nonneg_int(value: Any, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a non-negative int, got {value!r}")


@dataclass(frozen=True)
class FileChange:
    """One file changed in a pull request, with its before/after blob bytes."""

    path: str
    change_type: str = CHANGE_MODIFIED
    old_path: str = ""
    before: bytes | None = None
    after: bytes | None = None
    before_mode: str = ""
    after_mode: str = ""
    is_binary: bool = False
    oversized: bool = False
    integrity_error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.path, str):
            raise TypeError(f"FileChange.path must be str, got {type(self.path).__name__}")
        _require_choice(self.change_type, CHANGE_TYPES, "FileChange.change_type")
        if self.before is not None and not isinstance(self.before, bytes):
            raise TypeError(f"FileChange.before must be bytes or None, got {type(self.before).__name__}")
        if self.after is not None and not isinstance(self.after, bytes):
            raise TypeError(f"FileChange.after must be bytes or None, got {type(self.after).__name__}")
        _require_bool(self.is_binary, "FileChange.is_binary")
        _require_bool(self.oversized, "FileChange.oversized")


@dataclass(frozen=True)
class ResourceRef:
    """Canonical Kubernetes resource identity."""

    group: str
    version: str
    kind: str
    namespace: str
    name: str

    def __post_init__(self) -> None:
        for attr in ("group", "version", "kind", "namespace", "name"):
            if not isinstance(getattr(self, attr), str):
                raise TypeError(f"ResourceRef.{attr} must be str")

    def to_dict(self) -> dict[str, str]:
        return {
            "group": self.group,
            "version": self.version,
            "kind": self.kind,
            "namespace": self.namespace,
            "name": self.name,
        }

    def gvk_label(self) -> str:
        return f"{self.group}/{self.kind}" if self.group else self.kind


_NON_EFFECTIVE_REASON_CODES = frozenset(
    {
        "DOCUMENTATION_ONLY",
        "COMMENT_OR_WHITESPACE_ONLY",
        "ALLOWLISTED_METADATA_EDIT",
    }
)


@dataclass(frozen=True)
class Reason:
    """One deterministic, structured explanation contributing to a PR verdict."""

    code: str
    dimension: str
    severity: str
    message: str
    file: str = ""
    field_path: str = ""
    resource: ResourceRef | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.code, str) or not self.code.strip():
            raise ValueError("Reason.code must be a non-empty string")
        _require_choice(self.dimension, REASON_DIMENSIONS, "Reason.dimension")
        _require_choice(self.severity, RISK_LEVELS, "Reason.severity")
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("Reason.message must be a non-empty string")
        if self.resource is not None and not isinstance(self.resource, ResourceRef):
            raise TypeError("Reason.resource must be a ResourceRef or None")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "code": self.code,
            "dimension": self.dimension,
            "severity": self.severity,
            "message": self.message,
        }
        if self.file:
            out["file"] = self.file
        if self.field_path:
            out["field_path"] = self.field_path
        if self.resource is not None:
            out["resource"] = self.resource.to_dict()
        return out

    def sort_key(self) -> tuple[int, int, str, str, tuple[str, str, str, str, str], str, str, str]:
        sev_rank = -_RISK_COMBINE_RANK.get(self.severity, _RISK_COMBINE_RANK[RISK_UNKNOWN])
        non_eff_rank = 1 if self.code in _NON_EFFECTIVE_REASON_CODES else 0
        res_key = (
            (
                self.resource.group,
                self.resource.version,
                self.resource.kind,
                self.resource.namespace,
                self.resource.name,
            )
            if self.resource is not None
            else ("", "", "", "", "")
        )
        return (sev_rank, non_eff_rank, self.code, self.file, res_key, self.field_path, self.message, self.dimension)


@dataclass(frozen=True)
class ResourceVerdict:
    """Risk qualification for a single Kubernetes resource document."""

    file: str
    action: str
    ref: ResourceRef
    risk_level: str
    blast_radius: str
    revertability: str
    disruptiveness: str
    supported: bool = True

    def __post_init__(self) -> None:
        _require_choice(self.action, RESOURCE_ACTIONS, "ResourceVerdict.action")
        if not isinstance(self.ref, ResourceRef):
            raise TypeError("ResourceVerdict.ref must be a ResourceRef")
        _require_choice(self.risk_level, RISK_LEVELS, "ResourceVerdict.risk_level")
        _require_choice(self.blast_radius, BLAST_LEVELS, "ResourceVerdict.blast_radius")
        _require_choice(self.revertability, REVERTABILITY_LEVELS, "ResourceVerdict.revertability")
        _require_choice(self.disruptiveness, DISRUPTIVENESS_LEVELS, "ResourceVerdict.disruptiveness")
        _require_bool(self.supported, "ResourceVerdict.supported")
        if self.risk_level == RISK_UNKNOWN and self.supported:
            raise ValueError("ResourceVerdict.supported cannot be True when risk_level is 'unknown'")

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "action": self.action,
            "group": self.ref.group,
            "version": self.ref.version,
            "kind": self.ref.kind,
            "namespace": self.ref.namespace,
            "name": self.ref.name,
            "risk_level": self.risk_level,
            "blast_radius": self.blast_radius,
            "revertability": self.revertability,
            "disruptiveness": self.disruptiveness,
            "supported": self.supported,
        }


@dataclass(frozen=True)
class FileVerdict:
    """Risk qualification for a single changed file in the PR."""

    path: str
    change_type: str
    category: str
    risk_level: str
    blast_radius: str
    revertability: str
    disruptiveness: str
    supported: bool = True
    old_path: str = ""

    def __post_init__(self) -> None:
        _require_choice(self.change_type, CHANGE_TYPES, "FileVerdict.change_type")
        _require_choice(self.risk_level, RISK_LEVELS, "FileVerdict.risk_level")
        _require_choice(self.blast_radius, BLAST_LEVELS, "FileVerdict.blast_radius")
        _require_choice(self.revertability, REVERTABILITY_LEVELS, "FileVerdict.revertability")
        _require_choice(self.disruptiveness, DISRUPTIVENESS_LEVELS, "FileVerdict.disruptiveness")
        _require_bool(self.supported, "FileVerdict.supported")
        if self.risk_level == RISK_UNKNOWN and self.supported:
            raise ValueError("FileVerdict.supported cannot be True when risk_level is 'unknown'")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "change_type": self.change_type,
            "category": self.category,
            "risk_level": self.risk_level,
            "blast_radius": self.blast_radius,
            "revertability": self.revertability,
            "disruptiveness": self.disruptiveness,
            "supported": self.supported,
        }
        if self.old_path and self.old_path != self.path:
            out["old_path"] = self.old_path
        return out


@dataclass(frozen=True)
class PRStats:
    """Deterministic summary counts for a classified PR."""

    files_changed: int = 0
    resources_added: int = 0
    resources_modified: int = 0
    resources_deleted: int = 0
    namespaces_touched: int = 0
    clusters_touched: int = 0
    unsupported_files: int = 0
    unsupported_resources: int = 0
    parse_errors: int = 0

    def __post_init__(self) -> None:
        for attr in (
            "files_changed",
            "resources_added",
            "resources_modified",
            "resources_deleted",
            "namespaces_touched",
            "clusters_touched",
            "unsupported_files",
            "unsupported_resources",
            "parse_errors",
        ):
            _require_nonneg_int(getattr(self, attr), f"PRStats.{attr}")

    def to_dict(self) -> dict[str, int]:
        return {
            "files_changed": self.files_changed,
            "resources_added": self.resources_added,
            "resources_modified": self.resources_modified,
            "resources_deleted": self.resources_deleted,
            "namespaces_touched": self.namespaces_touched,
            "clusters_touched": self.clusters_touched,
            "unsupported_files": self.unsupported_files,
            "unsupported_resources": self.unsupported_resources,
            "parse_errors": self.parse_errors,
        }


@dataclass(frozen=True)
class ClassificationResult:
    """Complete deterministic risk qualification for a pull request."""

    risk_level: str
    blast_radius: str
    revertability: str
    disruptiveness: str
    supported: bool
    effective_changes: bool
    reasons: tuple[Reason, ...] = ()
    files: tuple[FileVerdict, ...] = ()
    resources: tuple[ResourceVerdict, ...] = ()
    stats: PRStats = field(default_factory=PRStats)

    def __post_init__(self) -> None:
        _require_choice(self.risk_level, RISK_LEVELS, "ClassificationResult.risk_level")
        _require_choice(self.blast_radius, BLAST_LEVELS, "ClassificationResult.blast_radius")
        _require_choice(self.revertability, REVERTABILITY_LEVELS, "ClassificationResult.revertability")
        _require_choice(self.disruptiveness, DISRUPTIVENESS_LEVELS, "ClassificationResult.disruptiveness")
        _require_bool(self.supported, "ClassificationResult.supported")
        _require_bool(self.effective_changes, "ClassificationResult.effective_changes")
        if self.risk_level == RISK_UNKNOWN and self.supported:
            raise ValueError("ClassificationResult.supported cannot be True when risk_level is 'unknown'")
        if not isinstance(self.reasons, tuple):
            object.__setattr__(self, "reasons", tuple(self.reasons))
        if not isinstance(self.files, tuple):
            object.__setattr__(self, "files", tuple(self.files))
        if not isinstance(self.resources, tuple):
            object.__setattr__(self, "resources", tuple(self.resources))
        if not isinstance(self.stats, PRStats):
            raise TypeError("ClassificationResult.stats must be a PRStats instance")

    @property
    def risk_label(self) -> str:
        return risk_label_for(self.risk_level)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "risk_level": self.risk_level,
            "blast_radius": self.blast_radius,
            "revertability": self.revertability,
            "disruptiveness": self.disruptiveness,
            "supported": self.supported,
            "effective_changes": self.effective_changes,
            "risk_label": self.risk_label,
            "reasons": [r.to_dict() for r in self.reasons],
            "files": [f.to_dict() for f in self.files],
            "resources": [r.to_dict() for r in self.resources],
            "stats": self.stats.to_dict(),
        }


def unknown_result(
    code: str,
    message: str,
    *,
    files_changed: int = 0,
    file: str = "",
) -> ClassificationResult:
    """Construct a fail-closed `unknown` verdict for unsupported or unreadable PRs."""
    effective_files = max(files_changed, 1) if file else files_changed
    return ClassificationResult(
        risk_level=RISK_UNKNOWN,
        blast_radius=BLAST_UNKNOWN,
        revertability=REVERT_UNKNOWN,
        disruptiveness=DISRUPT_UNKNOWN,
        supported=False,
        effective_changes=False,
        reasons=(
            Reason(
                code=code,
                dimension=DIM_INPUT_INTEGRITY,
                severity=RISK_UNKNOWN,
                message=message,
                file=file,
            ),
        ),
        stats=PRStats(
            files_changed=effective_files,
            unsupported_files=effective_files,
        ),
    )
