#!/usr/bin/env python3
"""
Invalidate local stdio MCP server schemas in on-disk mcp_schema_cache.json files.

When container images upgrade, new tools in local scripts (e.g. platform_mcp_server.py,
router_server.py) must be discovered on first use instead of being shadowed by stale
schema caches on persistent storage (Issue #854).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Known local stdio MCP servers in kube-agents whose tool definitions
# are backed by image-provided local scripts.
KNOWN_LOCAL_MCP_SERVERS = frozenset({"platform_control", "router"})


class CacheInvalidationError(RuntimeError):
    """Raised when one or more MCP schema caches could not be invalidated cleanly."""

    def __init__(self, message: str, results: dict[str, list[str]] | None = None):
        super().__init__(message)
        self.results = results or {}


def is_local_mcp_server(server_name: str, config: dict | None = None) -> bool:
    """Return True if the MCP server is locally hosted rather than a remote proxy."""
    if server_name in KNOWN_LOCAL_MCP_SERVERS:
        return True
    if not config or not isinstance(config, dict):
        return False
    # Remote servers have a direct URL or use mcp-remote proxy to an https:// endpoint
    if config.get("url"):
        return False
    args = config.get("args") or []
    for arg in args:
        if isinstance(arg, str):
            if "/opt/mcp-remote" in arg or arg.startswith("https://") or arg.startswith("http://"):
                return False
    # If it's a command like python / script inside scripts/ or local file, it's local
    command = str(config.get("command") or "")
    if "python" in command or any(str(a).endswith(".py") or "scripts/" in str(a) for a in args):
        return True
    return False


def get_profile_mcp_configs(profile_dir: Path) -> dict[str, dict]:
    """Try to read mcp_servers from a profile's config.yaml or template."""
    config_file = profile_dir / "config.yaml"
    if not config_file.is_file():
        return {}
    try:
        import yaml  # type: ignore
        with open(config_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if isinstance(data, dict):
                servers = data.get("mcp_servers")
                if isinstance(servers, dict):
                    return servers
    except Exception:
        pass
    return {}


def invalidate_cache_file(cache_file: Path, mcp_configs: dict[str, dict] | None = None) -> list[str]:
    """Remove local MCP server entries from a single mcp_schema_cache.json file."""
    if not cache_file.is_file():
        return []
    with open(cache_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"schema cache {cache_file} root is not a JSON object")

    removed: list[str] = []
    for server_name in list(data.keys()):
        cfg = (mcp_configs or {}).get(server_name)
        if is_local_mcp_server(server_name, cfg):
            del data[server_name]
            removed.append(server_name)

    if removed:
        tmp_file = cache_file.with_suffix(".tmp")
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_file, cache_file)
        finally:
            if tmp_file.exists():
                tmp_file.unlink(missing_ok=True)
    return removed


def invalidate_all_mcp_caches(target_dir: Path) -> dict[str, list[str]]:
    """Scan root and all profile directories under target_dir and invalidate local MCP caches."""
    results: dict[str, list[str]] = {}
    failed = False

    targets: list[tuple[str, Path, dict[str, dict]]] = []
    root_cache = target_dir / "cache" / "mcp_schema_cache.json"
    if root_cache.is_file():
        targets.append(("default", root_cache, get_profile_mcp_configs(target_dir)))

    profiles_dir = target_dir / "profiles"
    if profiles_dir.is_dir():
        for p in sorted(profiles_dir.iterdir()):
            if p.is_dir():
                p_cache = p / "cache" / "mcp_schema_cache.json"
                if p_cache.is_file():
                    targets.append((p.name, p_cache, get_profile_mcp_configs(p)))

    for profile_name, cache_file, configs in targets:
        try:
            removed = invalidate_cache_file(cache_file, configs)
            if removed:
                results[profile_name] = removed
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"unreadable cache {cache_file}: {e}; deleting", file=sys.stderr)
            try:
                cache_file.unlink(missing_ok=True)
            except OSError as unlink_err:
                print(f"could not delete corrupt cache {cache_file}: {unlink_err}", file=sys.stderr)
            failed = True

    if failed:
        raise CacheInvalidationError("failed to invalidate one or more MCP schema caches", results=results)

    return results


def main() -> None:
    target_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(os.environ.get("PLATFORM_AGENT_HOME", "/opt/data"))
    results: dict[str, list[str]] = {}
    failed = False
    try:
        results = invalidate_all_mcp_caches(target_path)
    except CacheInvalidationError as e:
        results = e.results
        failed = True

    for profile, servers in results.items():
        print(f"[ENTRYPOINT] Invalidated local MCP schema cache for profile '{profile}': {', '.join(servers)}")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
