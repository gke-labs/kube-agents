"""Core types, enums, and anti-masking aggregation lattices for PR risk classification."""

from __future__ import annotations

from dataclasses import dataclass, field
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
CONCRETE_RISK_ORDER: dict[str, int] = {
    RISK_LOW: 0,
    RISK_MEDIUM: 1,
    RISK_HIGH: 2,
    RISK_CRITICAL: 3,
}

# Anti-masking lattice for aggregating risk across files/resources:
#   low < medium < unknown < high < critical
# Unknown prevents claiming low/medium when any part of the PR is unsupported,
# while known high/critical can never be downgraded to unknown by adding an
# unsupported file alongside a dangerous change.
_RISK_COMBINE_RANK: dict[str, int] = {
    RISK_LOW: 0,
    RISK_MEDIUM: 1,
    RISK_UNKNOWN: 2,
    RISK_HIGH: 3,
    RISK_CRITICAL: 4,
}

# Blast radius levels (scope of impact)
BLAST_LOW = "low"
BLAST_MEDIUM = "medium"
BLAST_HIGH = "high"
BLAST_CRITICAL = "critical"
BLAST_UNKNOWN = "unknown"

BLAST_LEVELS = (BLAST_LOW, BLAST_MEDIUM, BLAST_HIGH, BLAST_CRITICAL, BLAST_UNKNOWN)

_BLAST_COMBINE_RANK: dict[str, int] = {
    BLAST_LOW: 0,
    BLAST_MEDIUM: 1,
    BLAST_UNKNOWN: 2,
    BLAST_HIGH: 3,
    BLAST_CRITICAL: 4,
}

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
_REVERT_COMBINE_RANK: dict[str, int] = {
    REVERT_EASY: 0,
    REVERT_MODERATE: 1,
    REVERT_DIFFICULT: 2,
    REVERT_UNKNOWN: 3,
    REVERT_IRREVERSIBLE: 4,
}

# Destructiveness levels (explicit 3-state enum, avoiding bool truthiness traps)
DESTRUCTIVE_NO = "non_destructive"
DESTRUCTIVE_YES = "destructive"
DESTRUCTIVE_UNKNOWN = "unknown"

DESTRUCTIVENESS_LEVELS = (DESTRUCTIVE_NO, DESTRUCTIVE_YES, DESTRUCTIVE_UNKNOWN)

_DESTRUCTIVE_COMBINE_RANK: dict[str, int] = {
    DESTRUCTIVE_NO: 0,
    DESTRUCTIVE_UNKNOWN: 1,
    DESTRUCTIVE_YES: 2,
}

# Reason dimensions
DIM_BLAST_RADIUS = "blast_radius"
DIM_DESTRUCTIVENESS = "destructiveness"
DIM_REVERTABILITY = "revertability"
DIM_SECURITY = "security"
DIM_INPUT_INTEGRITY = "input_integrity"
DIM_POLICY = "policy"

# File change types
CHANGE_ADDED = "added"
CHANGE_MODIFIED = "modified"
CHANGE_DELETED = "deleted"
CHANGE_RENAMED = "renamed"

# Resource action types
ACTION_ADD = "add"
ACTION_MODIFY = "modify"
ACTION_DELETE = "delete"

# Deterministic risk labels (broker-owned, reserved against caller writes)
RISK_LABEL_PREFIX = "risk:"
RISK_LABELS = tuple(f"{RISK_LABEL_PREFIX}{level}" for level in RISK_LEVELS)

RISK_LABEL_COLORS: dict[str, str] = {
    "risk:low": "0e8a16",
    "risk:medium": "fbca04",
    "risk:high": "d93f0b",
    "risk:critical": "b60205",
    "risk:unknown": "cfd3d7",
}

RISK_LABEL_DESCRIPTIONS: dict[str, str] = {
    "risk:low": "kube-agents deterministic PR risk: low blast radius, non-destructive, easy revert",
    "risk:medium": "kube-agents deterministic PR risk: moderate impact requiring review or dev policy",
    "risk:high": "kube-agents deterministic PR risk: high impact (security, traffic, deletion, or cluster scope)",
    "risk:critical": "kube-agents deterministic PR risk: critical control-plane, RBAC, governance, or data-loss risk",
    "risk:unknown": "kube-agents deterministic PR risk: unsupported or unverifiable PR content (auto-merge blocked)",
}


def risk_label_for(risk_level: str) -> str:
    """Return the canonical `risk:<level>` label for `risk_level`."""
    level = risk_level if risk_level in _RISK_COMBINE_RANK else RISK_UNKNOWN
    return f"{RISK_LABEL_PREFIX}{level}"


def combine_risk(a: str, b: str) -> str:
    """Combine two risk levels using the anti-masking lattice."""
    ra = _RISK_COMBINE_RANK.get(a, _RISK_COMBINE_RANK[RISK_UNKNOWN])
    rb = _RISK_COMBINE_RANK.get(b, _RISK_COMBINE_RANK[RISK_UNKNOWN])
    return b if rb > ra else a


def combine_blast_radius(a: str, b: str) -> str:
    """Combine two blast radius levels using the anti-masking lattice."""
    ra = _BLAST_COMBINE_RANK.get(a, _BLAST_COMBINE_RANK[BLAST_UNKNOWN])
    rb = _BLAST_COMBINE_RANK.get(b, _BLAST_COMBINE_RANK[BLAST_UNKNOWN])
    return b if rb > ra else a


def combine_revertability(a: str, b: str) -> str:
    """Combine two revertability levels using the anti-masking lattice."""
    ra = _REVERT_COMBINE_RANK.get(a, _REVERT_COMBINE_RANK[REVERT_UNKNOWN])
    rb = _REVERT_COMBINE_RANK.get(b, _REVERT_COMBINE_RANK[REVERT_UNKNOWN])
    return b if rb > ra else a


def combine_destructiveness(a: str, b: str) -> str:
    """Combine two destructiveness values (`destructive > unknown > non_destructive`)."""
    ra = _DESTRUCTIVE_COMBINE_RANK.get(a, _DESTRUCTIVE_COMBINE_RANK[DESTRUCTIVE_UNKNOWN])
    rb = _DESTRUCTIVE_COMBINE_RANK.get(b, _DESTRUCTIVE_COMBINE_RANK[DESTRUCTIVE_UNKNOWN])
    return b if rb > ra else a


def escalate_risk(level: str, *, destructive_or_irreversible: bool = False) -> str:
    """Escalate a concrete risk level by one step for production environments.

    `low -> medium`, `medium -> high`, and `high -> critical` when destructive,
    irreversible, or security-sensitive. `unknown` and `critical` stay as-is.
    """
    if level == RISK_LOW:
        return RISK_MEDIUM
    if level == RISK_MEDIUM:
        return RISK_HIGH
    if level == RISK_HIGH and destructive_or_irreversible:
        return RISK_CRITICAL
    return level


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


@dataclass(frozen=True)
class ResourceRef:
    """Canonical Kubernetes resource identity."""

    group: str
    version: str
    kind: str
    namespace: str
    name: str

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

    def sort_key(self) -> tuple[int, str, str, str, str, str]:
        sev_rank = -_RISK_COMBINE_RANK.get(self.severity, 2)
        res_key = (
            f"{self.resource.group}/{self.resource.version}/{self.resource.kind}/"
            f"{self.resource.namespace}/{self.resource.name}"
            if self.resource
            else ""
        )
        return (sev_rank, self.code, self.file, res_key, self.field_path, self.message)


@dataclass(frozen=True)
class ResourceVerdict:
    """Risk qualification for a single Kubernetes resource document."""

    file: str
    action: str
    ref: ResourceRef
    risk_level: str
    blast_radius: str
    revertability: str
    destructiveness: str
    supported: bool = True

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
            "destructiveness": self.destructiveness,
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
    destructiveness: str
    supported: bool = True
    old_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "change_type": self.change_type,
            "category": self.category,
            "risk_level": self.risk_level,
            "blast_radius": self.blast_radius,
            "revertability": self.revertability,
            "destructiveness": self.destructiveness,
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
    destructiveness: str
    supported: bool
    effective_changes: bool
    reasons: tuple[Reason, ...] = ()
    files: tuple[FileVerdict, ...] = ()
    resources: tuple[ResourceVerdict, ...] = ()
    stats: PRStats = field(default_factory=PRStats)

    @property
    def risk_label(self) -> str:
        return risk_label_for(self.risk_level)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "risk_level": self.risk_level,
            "blast_radius": self.blast_radius,
            "revertability": self.revertability,
            "destructiveness": self.destructiveness,
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
    return ClassificationResult(
        risk_level=RISK_UNKNOWN,
        blast_radius=BLAST_UNKNOWN,
        revertability=REVERT_UNKNOWN,
        destructiveness=DESTRUCTIVE_UNKNOWN,
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
            files_changed=files_changed,
            unsupported_files=max(files_changed, 1) if file else files_changed,
        ),
    )
