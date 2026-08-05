"""Secure persistence for the local portal's non-secret connection target."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from admin_console.project_config import (
    DeploymentTarget,
    is_valid_cluster_name,
    is_valid_location,
    is_valid_namespace,
    is_valid_project_id,
)

STATE_VERSION = 1
MAX_STATE_BYTES = 4096


@dataclass(frozen=True)
class PersistedConnection:
    target: DeploymentTarget
    account: str
    verified_at: datetime


def connection_state_path() -> Path:
    """Return the per-user state path, with a test/deployment override."""
    override = os.environ.get("KUBE_AGENTS_ADMIN_CONNECTION_STATE", "").strip()
    if override:
        return Path(override).expanduser()
    state_root = os.environ.get("XDG_STATE_HOME", "").strip()
    root = Path(state_root).expanduser() if state_root else Path.home() / ".local/state"
    return root / "kube-agents" / "admin-portal-connection.json"


def load_connection(account: str) -> PersistedConnection | None:
    """Load validated metadata belonging to the launcher-verified account."""
    path = connection_state_path()
    try:
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > MAX_STATE_BYTES
        ):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
        return None
    stored_account = str(payload.get("account", ""))
    if not account or stored_account != account:
        return None

    project_id = str(payload.get("project_id", ""))
    cluster_name = str(payload.get("cluster_name", ""))
    location = str(payload.get("location", ""))
    namespace = str(payload.get("namespace", ""))
    if not (
        is_valid_project_id(project_id)
        and is_valid_cluster_name(cluster_name)
        and is_valid_location(location)
        and is_valid_namespace(namespace)
    ):
        return None
    try:
        verified_at = datetime.fromisoformat(str(payload.get("verified_at", "")))
    except ValueError:
        return None
    if verified_at.tzinfo is None:
        return None
    verified_at = verified_at.astimezone(timezone.utc)

    source = str(payload.get("source", "persisted selection"))
    if source not in {
        "kube-agents-host label",
        "manual selection",
        "provisioned state",
        "persisted selection",
    }:
        source = "persisted selection"
    return PersistedConnection(
        DeploymentTarget(project_id, cluster_name, location, namespace, source),
        stored_account,
        verified_at,
    )


def save_connection(
    account: str,
    target: DeploymentTarget,
    verified_at: datetime,
) -> None:
    """Atomically persist connection metadata with owner-only permissions."""
    if not account:
        raise ValueError("a verified account is required")
    if not (
        is_valid_project_id(target.project_id)
        and is_valid_cluster_name(target.cluster_name)
        and is_valid_location(target.location)
        and is_valid_namespace(target.namespace)
    ):
        raise ValueError("invalid deployment target")
    if verified_at.tzinfo is None:
        raise ValueError("verified_at must be timezone-aware")

    path = connection_state_path()
    parent_existed = path.parent.exists()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not parent_existed:
        os.chmod(path.parent, 0o700)
    payload = {
        "version": STATE_VERSION,
        "account": account,
        "project_id": target.project_id,
        "cluster_name": target.cluster_name,
        "location": target.location,
        "namespace": target.namespace,
        "source": target.source,
        "verified_at": verified_at.astimezone(timezone.utc).isoformat(),
    }
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise


def delete_connection() -> None:
    """Remove the persisted target. Credentials are never stored here."""
    connection_state_path().unlink(missing_ok=True)
