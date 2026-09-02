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


_REFERENCE = re.compile(
    r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))"
)


def _expand(value: str, scope: dict[str, str]) -> str:
    """Substitute `$VAR` and `${VAR}` from keys the file has already set.

    The installers load these files with `set -a; . install.env; set +a`, and
    install.env.example advertises shell syntax -- so `CLUSTER_NAME=${PROJECT_ID}-host`
    is legal and the installers resolve it. Reading it literally instead fails
    silently: the literal is rejected by CLUSTER_NAME_PATTERN below and the
    portal shows no cluster scope, which is indistinguishable from an install
    that never set one.

    `scope` is the allowlisted keys resolved so far, in file order, so only
    those can be referenced. That is narrower than the shell, which would also
    expand from its own environment and from any other assignment in the file;
    both are deliberate. Reading the environment would make the answer depend on
    who ran the portal, and keeping non-allowlisted values out of `scope` keeps
    the API keys and tokens these files also hold out of this function entirely.

    A reference `scope` cannot resolve is left as written rather than dropped.
    The literal then fails validation exactly as it did before this expansion
    existed, so an unresolvable value degrades to the old behaviour instead of
    turning a discovered install into an undiscovered one.

    `scripts/live_test_lease.py` carries the same expansion for the same files;
    change both together.
    """

    def substitute(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        return scope.get(name, match.group(0))

    return _REFERENCE.sub(substitute, value)


def _parse_assignment_value(raw_value: str, scope: dict[str, str]) -> str:
    """Parse shell quoting and `$VAR` references without sourcing code."""
    try:
        words = shlex.split(raw_value, comments=False, posix=True)
    except ValueError:
        return ""
    if len(words) != 1:
        return ""
    # Single quotes suppress expansion in the shell, so they suppress it here.
    # Testing the raw value rather than the parsed one is what keeps that true:
    # shlex has already removed the quotes by the time `words` exists.
    if "'" in raw_value:
        return words[0]
    return _expand(words[0], scope)


# `export` is optional because the two files this reads differ: install.env is
# a hand-authored dotenv (`K=V`) and the vars.sh it replaced was generated with
# `printf %q` (`export K=V`). A pattern requiring `export` matches nothing in
# install.env and returns None, which the portal reads as "no provisioned
# target" and silently falls back to the query parameter and the persisted
# connection rather than failing -- so getting this wrong fails quietly.
_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(PROJECT_ID|CLUSTER_NAME|REGION|NAMESPACE)=(.*)$"
)


def _read_assignments(path: Path, scope: dict[str, str] | None = None) -> dict[str, str]:
    """The allowlisted assignments in one file, or {} if it cannot be read.

    `scope` accumulates across the call so a later assignment can reference an
    earlier one, which is the order the shell resolves them in. It is both read
    and written; pass the same dict for vars.sh and install.env to let the
    second file reference the first, as sourcing them in that order would.
    """
    values: dict[str, str] = {}
    if scope is None:
        scope = {}
    if not path.is_file():
        return values
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        match = _ASSIGNMENT.fullmatch(line)
        if not match or match.group(1) not in STATE_KEYS:
            continue
        value = _parse_assignment_value(match.group(2).strip(), scope)
        values[match.group(1)] = value
        scope[match.group(1)] = value
    return values


def load_provisioned_target(
    vars_path: Path, install_env_path: Path | None = None
) -> DeploymentTarget | None:
    """Read the non-secret deployment coordinates allowlist from the install.

    Both files may contain secrets and both are shell-ish. Neither is ever
    sourced by the portal. Only fixed assignment names and validated values are
    accepted here.

    `install_env_path` is the hand-authored input and wins on every key it
    carries; `vars_path` is the generated state it replaced, still read so a
    deployment from before the change keeps working.
    """
    scope: dict[str, str] = {}
    values = _read_assignments(vars_path, scope)
    if install_env_path is not None:
        values.update(_read_assignments(install_env_path, scope))
    if not values:
        return None

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
