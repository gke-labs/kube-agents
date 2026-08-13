#!/usr/bin/env python3
# cluster_agent_profile.py - Manage per-cluster Cluster Agent Hermes profiles.
#
# The Platform Agent runs this from its terminal to dynamically create, delete,
# list, and resolve the name of the Cluster Agent profile for a specific GKE
# cluster, inside its own pod. One profile per managed cluster; it persists until
# the cluster is deleted.
#
# Mechanism (verified against the shipped Hermes CLI):
#   - `hermes profile create <name>` registers an isolated profile and stores its
#     home at $HERMES_HOME/profiles/<name>. Because HERMES_HOME is the data PVC
#     (/opt/data) in the pod, profiles persist across restarts automatically.
#   - We then overlay the baked Cluster Agent template (/opt/cluster-template/:
#     SOUL.md, AGENTS.md, config.yaml, skills/) onto that home, pin a kubeconfig
#     scoped to the target cluster, and write the cluster identity into USER.md.
#   - Delegation runs on the shared kanban board (assignee = the profile name);
#     this script only manages profile lifecycle + name resolution, not dispatch.

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from profile_scaffold import ensure_profile, overlay_template

TEMPLATE_DIR = Path(os.environ.get("CLUSTER_TEMPLATE_DIR", "/opt/cluster-template"))
SHARED_PLUGINS_DIR = Path(os.environ.get("SHARED_PLUGINS_DIR", "/opt/defaults/plugins"))
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
# Operator-rendered config overlays and profile-targeted plugin image volumes. The
# entrypoint applies both at pod startup; a profile scaffolded here appears later, so it
# has to pick them up itself (see create_profile steps 2c/2d).
OVERLAY_DIR = Path(os.environ.get("PROFILE_OVERLAY_DIR", "/opt/agent-config"))
PLUGIN_MOUNT_ROOT = Path(os.environ.get("PLUGIN_MOUNT_ROOT", "/opt/agent-plugins"))
# Hermes stores each profile at $HERMES_HOME/profiles/<name> (persists on the data PVC).
PROFILES_BASE = HERMES_HOME / "profiles"

# Files/dirs from the template to overlay onto the created profile home.
OVERLAY_ITEMS = ("SOUL.md", "AGENTS.md", "CAPABILITIES.md", "config.yaml", "skills")
MAX_NAME_LEN = 63

# Non-cluster profiles that live under $HERMES_HOME/profiles but are never
# managed as Cluster Agents: the front-door router (`default`) and the Platform
# Agent itself (`platform`). Reconciliation must never touch these.
RESERVED_PROFILES = frozenset({"default", "platform"})


def log(msg: str) -> None:
    print(f"[CLUSTER-PROFILE] {msg}", file=sys.stderr)


def _run_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Env for subprocesses: HOME -> /tmp (writable creds) and HERMES_HOME pinned."""
    return {**os.environ, "HOME": "/tmp", "HERMES_HOME": str(HERMES_HOME), **(extra or {})}


def _validate(value: str, field: str) -> None:
    if not value or not re.match(r"^[a-zA-Z0-9._-]+$", value):
        raise SystemExit(f"ERROR: invalid {field}: {value!r}")


def profile_name(project: str, cluster: str, location: str) -> str:
    """Derive a stable, sanitized profile name for a target cluster.

    Mirrors the kubeconfig naming convention in platform_mcp_server.switch_kube_context.
    """
    raw = f"cluster-{project}-{cluster}-{location}".lower()
    name = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]+", "-", raw)).strip("-")
    if len(name) > MAX_NAME_LEN:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        name = f"{name[: MAX_NAME_LEN - 9]}-{digest}"
    return name


def profile_home(name: str) -> Path:
    return PROFILES_BASE / name


def _inject_cluster_identity(home: Path, project: str, cluster: str, location: str) -> None:
    """Write a machine-readable cluster_identity block into the profile's config.yaml.

    Records the target project/cluster/location as structured identity metadata — robust
    vs the sanitized/hashed profile name. (Re-dumping drops the template's comments in this
    per-profile copy, which is fine.) Kept intentionally after the fleet-handover retirement:
    it is cheap identity metadata and is what a restored `write_handover` producer would read
    (see docs/designs/fleet-handover-retirement.md).
    """
    import yaml  # lazy: only needed on the scaffold path, keeps the module importable without pyyaml

    config_path = home / "config.yaml"
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        data = {}
    data["cluster_identity"] = {"project": project, "cluster": cluster, "location": location}
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def read_cluster_identity(home: Path) -> dict[str, str] | None:
    """Read the ``cluster_identity`` block written into a profile's ``config.yaml``.

    Returns the ``{project, cluster, location}`` dict, or ``None`` if the config is
    missing/unparseable or the block is absent/incomplete. This is the robust,
    machine-readable inverse of :func:`_inject_cluster_identity` — reconciliation
    reads it rather than trying to reverse the sanitized/hashed profile name.
    """
    import yaml  # lazy: keeps the module importable without pyyaml on pure-lookup paths

    config_path = home / "config.yaml"
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return None
    identity = data.get("cluster_identity")
    if not isinstance(identity, dict):
        return None
    project, cluster, location = identity.get("project"), identity.get("cluster"), identity.get("location")
    if not (project and cluster and location):
        return None
    return {"project": str(project), "cluster": str(cluster), "location": str(location)}


def _pin_kubeconfig_env(home: Path, kubeconfig: Path) -> None:
    """Pin KUBECONFIG for the dispatcher-spawned worker via the profile's ``.env``.

    A worker launched as ``hermes -p <name>`` rewrites HERMES_HOME to this profile
    home and loads ``<home>/.env`` at startup (Hermes ``get_env_path()`` ==
    ``get_hermes_home()/".env"``), so this is what actually exports KUBECONFIG on
    the dispatch path — the gateway's spawn env never sets it. Without it a
    dispatched Cluster Agent runs kubectl against the wrong/absent cluster.

    Idempotent: rewrites the ``KUBECONFIG`` line in place and preserves any other
    lines already present in ``.env``.
    """
    env_path = home / ".env"
    existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    kept = [ln for ln in existing.splitlines(keepends=True) if not ln.startswith("KUBECONFIG=")]
    env_path.write_text("".join(kept) + f"KUBECONFIG={kubeconfig}\n", encoding="utf-8")


def _pin_otel_endpoint(home: Path, name: str) -> None:
    """Point this profile's hermes_otel copy at the collector the operator resolved.

    The profile just copytree'd /opt/defaults/plugins, so it carries the endpoint baked
    into the image — and hermes_otel does not read OTEL_EXPORTER_OTLP_ENDPOINT. The
    entrypoint sweeps every profile that exists at startup, but a cluster profile is
    created at onboarding time, long after that, so it has to do this for itself.

    Side benefit: this is the first time a cluster profile gets a service.name at all.

    Never fatal — a deployment without the plugin, or with unwritable telemetry config, is
    still a working Cluster Agent.
    """
    try:
        from otel_config import apply  # lazy, as with yaml above

        config = home / "plugins" / "hermes_otel" / "config.yaml"
        if not config.exists():
            return
        source = SHARED_PLUGINS_DIR / "hermes_otel" / "config.yaml"
        apply(
            config,
            service_name=os.environ.get("OTEL_SERVICE_NAME") or None,
            endpoint=os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            source_path=source if source.exists() else None,
        )
    except Exception as e:  # noqa: BLE001 - telemetry must not fail the scaffold
        log(f"{name}: pinning the OpenTelemetry endpoint failed ({e}); traces go to the image default")


def create_profile(project: str, cluster: str, location: str) -> str:
    """Scaffold (idempotently) a Cluster Agent profile for a GKE cluster; return its name.

    Shared by the ``create`` CLI subcommand and the reconcile engine. Raises SystemExit on a
    hard failure (missing template, credential fetch failure).
    """
    for field, value in (("project", project), ("cluster", cluster), ("location", location)):
        _validate(value, field)
    name = profile_name(project, cluster, location)

    if not TEMPLATE_DIR.is_dir():
        raise SystemExit(f"ERROR: cluster template dir not found: {TEMPLATE_DIR}")

    # 1. Register the profile with Hermes (idempotent) via the shared scaffold helper.
    description = f"Read-only Cluster Agent for GKE cluster {cluster} ({project}/{location})."
    home = ensure_profile(name, description, HERMES_HOME)

    # 2. Overlay the Cluster Agent persona, scoped config, and skills (+ shared plugins).
    overlay_template(home, TEMPLATE_DIR, SHARED_PLUGINS_DIR, items=OVERLAY_ITEMS)

    # 2a. Repoint the plugin copy the overlay just made at the resolved collector.
    _pin_otel_endpoint(home, name)

    # 2b. Stamp this cluster's identity into the profile config as structured identity
    #     metadata — never derived from the sanitized profile name.
    _inject_cluster_identity(home, project, cluster, location)

    # 2c. Link any plugin image volumes the operator mounted for this profile.
    # 2d. Apply the operator's config overlays: the cluster class overlay carrying
    #     spec.harness.tuning.cluster, plus this profile's own if a plugin targets it.
    #
    # Both are startup steps in docker-entrypoint.sh, and startup is not enough. Nothing
    # rolls the pod when a cluster is onboarded — the ConfigMap has not changed — so a
    # profile scaffolded here would otherwise run on Hermes' defaults (3 retries, 90
    # turns) however the CR is tuned, until an unrelated restart. That failure looks like
    # a run that stops mid-task, which is exactly what raising the limits prevents.
    #
    # Applied after the identity stamp: _inject_cluster_identity rewrites the whole
    # config, and the overlay's last-applied record has to describe the file as it
    # finally stands. Neither step is fatal — a deployment without the operator has no
    # /opt/agent-config at all, and a profile on image defaults still works.
    try:
        from profile_plugins import link_plugins  # lazy, as with yaml above

        linked = link_plugins(home, PLUGIN_MOUNT_ROOT, name)
        if linked:
            log(f"{name}: linked plugin volume(s): {', '.join(linked)}")
    except Exception as e:  # noqa: BLE001 - a missing mount must not fail the scaffold
        log(f"{name}: linking targeted plugin volumes failed ({e}); they will not load")

    try:
        from profile_overlay import sync_profile  # lazy, as with yaml above

        log(f"{name}: {sync_profile(home, OVERLAY_DIR)}")
    except Exception as e:  # noqa: BLE001 - a missing overlay dir must not fail the scaffold
        log(f"{name}: overlay sync failed ({e}); running on image defaults")

    # 3. Pin a kubeconfig scoped to the target cluster.
    kubeconfig = home / "kubeconfig.yaml"
    env = _run_env({"KUBECONFIG": str(kubeconfig)})
    try:
        subprocess.run(
            [
                "gcloud", "container", "clusters", "get-credentials", cluster,
                f"--location={location}", f"--project={project}",
            ],
            check=True, capture_output=True, text=True, timeout=60, env=env,
        )
    except subprocess.CalledProcessError as e:
        raise SystemExit(f"ERROR: failed to fetch credentials for '{cluster}': {e.stderr.strip()}")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"ERROR: timed out fetching credentials for '{cluster}'.")
    except OSError as e:
        # `gcloud` not on PATH or not executable — raised before the process exists,
        # so neither handler above sees it. Same failure mode the `hermes` invocation
        # in profile_scaffold.ensure_profile guards against; keep it to one
        # actionable line instead of a traceback in the container log.
        raise SystemExit(f"ERROR: could not execute 'gcloud' to fetch credentials for '{cluster}': {e}")

    # 3b. Pin KUBECONFIG for the dispatcher-spawned worker via the profile's .env.
    _pin_kubeconfig_env(home, kubeconfig)

    # 4. Write the fixed cluster identity into USER.md.
    #
    # Every field is a `- <key>: <value>` bullet because that is the only shape
    # cluster_preflight.sh's user_md_field() can read (it matches
    # `^[[:space:]]*-[[:space:]]*<key>:`). The kubeconfig line used to sit
    # outside the list with no leading `- `, which made it unparseable — and
    # since it is also the line a human reaches for when repairing a bad pin,
    # editing it achieved nothing at all.
    #
    # It stays informational even so: the pin the runtime honours is KUBECONFIG
    # in the profile's .env (step 3b), not this line. Repointing an agent means
    # re-running this scaffold, not editing USER.md.
    (home / "USER.md").write_text(
        "# Cluster Agent Context\n\n"
        "This Cluster Agent is permanently scoped to the following GKE cluster:\n\n"
        f"- project: {project}\n"
        f"- cluster: {cluster}\n"
        f"- location: {location}\n"
        f"- kubeconfig: {kubeconfig}\n\n"
        "The authoritative KUBECONFIG pin lives in this profile's `.env`; the\n"
        "line above records it for reference. To repoint this agent, re-run\n"
        "`cluster_agent_profile.py create` — do not hand-edit this file.\n",
        encoding="utf-8",
    )
    return name


def cmd_create(args: argparse.Namespace) -> None:
    print(create_profile(args.project, args.cluster, args.location))


def delete_profile(name: str) -> None:
    """Deregister a Hermes profile and remove its home directory.

    Tolerant of an already-absent profile (the ``hermes profile delete`` failure is
    logged, then the home is cleaned up regardless). Shared by the ``delete`` CLI
    subcommand and the reconcile engine.
    """
    home = profile_home(name)
    try:
        subprocess.run(
            ["hermes", "profile", "delete", name, "-y"],
            check=True, capture_output=True, text=True, timeout=30, env=_run_env(),
        )
    except Exception as e:  # noqa: BLE001 - tolerate an already-absent profile
        log(f"'hermes profile delete {name}' failed (continuing to clean up home): {e}")
    if home.exists():
        shutil.rmtree(home, ignore_errors=True)


def list_profiles() -> list[str]:
    """Return sorted names of managed Cluster Agent profiles (excludes reserved profiles)."""
    if not PROFILES_BASE.is_dir():
        return []
    return sorted(
        p.name for p in PROFILES_BASE.iterdir() if p.is_dir() and p.name not in RESERVED_PROFILES
    )


def cmd_delete(args: argparse.Namespace) -> None:
    name = profile_name(args.project, args.cluster, args.location)
    delete_profile(name)
    print(name)


def cmd_list(_args: argparse.Namespace) -> None:
    for name in list_profiles():
        print(name)


def cmd_name(args: argparse.Namespace) -> None:
    """Print the canonical profile name for a cluster — used as the kanban `assignee`.

    Delegation runs on the kanban board: the Platform Agent creates a card with
    `assignee=<this name>`, and the gateway's kanban dispatcher spawns the profile
    as a worker (`hermes -p <name> chat -q "work kanban task <id>"`). This is a pure,
    deterministic lookup (no side effects), so the assignee can be resolved anytime.
    """
    print(profile_name(args.project, args.cluster, args.location))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage per-cluster Cluster Agent Hermes profiles.")
    sub = parser.add_subparsers(dest="command", required=True)

    help_text = {
        "create": "Create (scaffold) a cluster profile",
        "delete": "Delete a cluster profile",
        "name": "Print the canonical profile name (kanban assignee) for a cluster",
    }
    for cmdname in ("create", "delete", "name"):
        sp = sub.add_parser(cmdname, help=help_text[cmdname])
        sp.add_argument("--project", required=True)
        sp.add_argument("--cluster", required=True)
        sp.add_argument("--location", required=True)

    sub.add_parser("list", help="List existing cluster profiles")

    args = parser.parse_args()
    handlers = {"create": cmd_create, "delete": cmd_delete, "list": cmd_list, "name": cmd_name}
    handlers[args.command](args)


if __name__ == "__main__":
    main()
