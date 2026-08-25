"""Safe discovery and validation of admin-console deployment scope."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
CLUSTER_NAME_PATTERN = re.compile(r"^[a-z](?:[a-z0-9-]{0,38}[a-z0-9])?$")
LOCATION_PATTERN = re.compile(r"^[a-z]+-[a-z0-9]+[0-9](?:-[a-z])?$")
REGION_PATTERN = re.compile(r"^[a-z]+-[a-z0-9]+[0-9]$")
NAMESPACE_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$"
)
STATE_KEYS = {"PROJECT_ID", "CLUSTER_NAME", "REGION", "NAMESPACE"}
TARGET_SCOPE_HEADERS = (
    ("x-kube-agents-project", "project_id"),
    ("x-kube-agents-cluster", "cluster_name"),
    ("x-kube-agents-location", "location"),
    ("x-kube-agents-namespace", "namespace"),
)


@dataclass(frozen=True)
class DeploymentTarget:
    project_id: str
    cluster_name: str = ""
    location: str = ""
    namespace: str = "kubeagents-system"
    source: str = "provisioned state"


@dataclass(frozen=True)
class ProjectCandidate:
    project_id: str
    source: str


def deployment_target_headers(target: DeploymentTarget) -> dict[str, str]:
    """Return the request scope used to reject stale browser tabs."""

    return {
        header: str(getattr(target, attribute))
        for header, attribute in TARGET_SCOPE_HEADERS
    }


def is_valid_project_id(value: str) -> bool:
    """Return whether value is a syntactically valid Google Cloud project ID."""
    return bool(PROJECT_ID_PATTERN.fullmatch(value.strip()))


def is_valid_cluster_name(value: str) -> bool:
    return bool(CLUSTER_NAME_PATTERN.fullmatch(value.strip()))


def is_valid_location(value: str) -> bool:
    return bool(LOCATION_PATTERN.fullmatch(value.strip()))


def is_valid_region(value: str) -> bool:
    return bool(REGION_PATTERN.fullmatch(value.strip()))


def is_valid_namespace(value: str) -> bool:
    return bool(NAMESPACE_PATTERN.fullmatch(value.strip()))


def _parse_assignment_value(raw_value: str) -> str:
    """Parse shell quoting without evaluating substitutions or sourcing code."""
    try:
        words = shlex.split(raw_value, comments=False, posix=True)
    except ValueError:
        return ""
    return words[0] if len(words) == 1 else ""


def load_provisioned_target(vars_path: Path) -> DeploymentTarget | None:
    """Read the non-secret deployment coordinates allowlist from vars.sh.

    The provision state is shell code and may contain secrets. It must never be
    sourced by the portal. Only fixed assignment names and validated values are
    accepted here.
    """
    if not vars_path.is_file():
        return None

    values: dict[str, str] = {}
    try:
        lines = vars_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    assignment = re.compile(
        r"^\s*export\s+(PROJECT_ID|CLUSTER_NAME|REGION|NAMESPACE)=(.*)$"
    )
    for line in lines:
        match = assignment.fullmatch(line)
        if not match or match.group(1) not in STATE_KEYS:
            continue
        values[match.group(1)] = _parse_assignment_value(match.group(2).strip())

    project_id = values.get("PROJECT_ID", "")
    cluster_name = values.get("CLUSTER_NAME", "")
    location = values.get("REGION", "")
    namespace = values.get("NAMESPACE", "") or "kubeagents-system"
    if not is_valid_project_id(project_id):
        return None
    if cluster_name and not CLUSTER_NAME_PATTERN.fullmatch(cluster_name):
        cluster_name = ""
    if location and not LOCATION_PATTERN.fullmatch(location):
        location = ""
    if not NAMESPACE_PATTERN.fullmatch(namespace):
        namespace = "kubeagents-system"

    return DeploymentTarget(
        project_id=project_id,
        cluster_name=cluster_name,
        location=location,
        namespace=namespace,
    )


def build_project_candidates(
    provisioned: DeploymentTarget | None,
    configured_project: str,
    requested_project: str = "",
    persisted_project: str = "",
) -> tuple[ProjectCandidate, ...]:
    """Return unique, validated project choices in preferred order."""
    candidates: list[ProjectCandidate] = []

    def add(project_id: str, source: str) -> None:
        project_id = project_id.strip()
        if not is_valid_project_id(project_id):
            return
        if any(item.project_id == project_id for item in candidates):
            return
        candidates.append(ProjectCandidate(project_id, source))

    if provisioned:
        add(provisioned.project_id, provisioned.source)
    add(configured_project, "active gcloud configuration")
    add(persisted_project, "saved connection")
    add(requested_project, "URL selection")
    return tuple(candidates)
