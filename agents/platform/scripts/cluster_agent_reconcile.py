#!/usr/bin/env python3
# cluster_agent_reconcile.py - Reconcile Cluster Agent profiles with the live GKE fleet.
#
# Cluster Agents are Hermes profiles on the data PVC ($HERMES_HOME/profiles/<name>), one per
# managed GKE cluster, each stamped with a `cluster_identity` block in its config.yaml.
#
# Policy: **every cluster in the project gets a Cluster Agent profile**, including the
# management cluster where kube-agents itself runs. Only names listed in RECONCILE_EXCLUDE
# are left unmanaged. Per run this deterministic engine:
#   • CREATE — scaffolds a profile for every project cluster that doesn't have one yet;
#   • PRUNE  — deletes a profile whose cluster is *definitively* gone (a NotFound/404 from
#     `gcloud container clusters describe`), or whose cluster was added to RECONCILE_EXCLUDE
#     after the fact. Any other error path — auth, network, timeout, quota, an unreadable
#     identity — is treated as "unknown" and the profile is left untouched: we never delete
#     on ambiguity.
#
# The management cluster used to be excluded, identified via the GKE metadata server. It is
# not any more, because an event on that cluster now needs an agent scoped to it like every
# other cluster's does: the triage session runs on the Planning Agent, whose one instruction is
# to delegate it to the profile scoped to the cluster that raised the event
# (session_kv_server.trigger_agent_troubleshooter), so a cluster without a profile is a
# cluster whose alerts have nobody to answer them. Two consequences worth knowing:
#   • the event watcher must not then watch that cluster twice, once through --in-cluster and
#     once through the new profile — buildWatchSet in cmd/k8s-event-watcher/main.go drops the
#     duplicate;
#   • the management cluster's Cluster Agent can read the harness's own namespace with the pod's
#     GSA — not the KSA, since create_profile pins a get-credentials kubeconfig — so how far that
#     reaches is the GSA's permission set: no Secrets on the default read-only roles, and Secrets
#     included on any `custom` set that names an admin role. RECONCILE_EXCLUDE is the opt-out,
#     and the security reference is the canonical statement.
#
# It runs as a `no_agent` cron job on the profile the gateway actually ticks (the `default`/chat
# profile — see docs/designs/fleet-handover-retirement.md §4). Scripts and the profiles PVC are
# shared pod-wide, so it operates on every profile regardless of which profile ticks it. It is
# resilient (always exit 0) and posts a Google Chat summary only when it created or pruned.

import argparse
import json
import os
import subprocess
import sys
import urllib.request

from cluster_agent_profile import (
    RESERVED_PROFILES,  # noqa: F401 - re-exported for callers/tests; used indirectly via list_profiles
    create_profile,
    delete_profile,
    list_profiles,
    profile_home,
    read_cluster_identity,
)

DESCRIBE_TIMEOUT_SECONDS = 30
_MD_BASE = "http://metadata.google.internal/computeMetadata/v1/"
EXTRA_EXCLUDE = {c for c in os.environ.get("RECONCILE_EXCLUDE", "").split(",") if c}


def log(msg: str) -> None:
    print(f"[CLUSTER-RECONCILE] {msg}", file=sys.stderr)


def _run_env() -> dict[str, str]:
    """HOME -> /tmp so gcloud can read/write credentials on the writable scratch disk."""
    return {**os.environ, "HOME": "/tmp"}


def _metadata(path: str):
    """Read a GKE/GCE metadata value, or None if unavailable."""
    try:
        req = urllib.request.Request(_MD_BASE + path, headers={"Metadata-Flavor": "Google"})
        # Context-managed: this runs on a cron tick, so a socket left to the garbage
        # collector is a socket leaked once per tick, forever.
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode().strip()
    except Exception:  # noqa: BLE001
        return None


def _project() -> str | None:
    p = os.environ.get("RECONCILE_PROJECT") or _metadata("project/project-id")
    if p:
        return p
    try:
        r = subprocess.run(["gcloud", "config", "get-value", "project"],
                           capture_output=True, text=True, timeout=30, env=_run_env())
        return r.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _all_clusters(project: str) -> list:
    """Every cluster in the project as (project, name, location) tuples.

    `check=True` matters: without it a failed `gcloud` (expired auth, no network,
    revoked permission) returns a non-zero exit with empty stdout, which parses to
    an empty list and is indistinguishable from "this project has no clusters".
    The CREATE pass would then do nothing, silently, on every tick. Raising turns
    that into the log line below. Returning [] is still the right degradation —
    PRUNE runs off `_cluster_exists`, not this list, so a bad list can never
    delete anything.
    """
    try:
        r = subprocess.run(
            ["gcloud", "container", "clusters", "list", "--project", project,
             "--format=value(name,location)"],
            capture_output=True, text=True, check=True, timeout=120, env=_run_env(),
        )
    except subprocess.CalledProcessError as e:
        # CalledProcessError stringifies to just the exit status; gcloud puts the
        # actual reason on stderr, which is the only part worth reading.
        log(f"listing clusters in {project} failed (skipping create this run): "
            f"{(e.stderr or '').strip() or e}")
        return []
    except Exception as e:  # noqa: BLE001 - timeout, gcloud missing, OSError
        log(f"listing clusters in {project} failed (skipping create this run): {e}")
        return []
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            out.append((project, parts[0], parts[1]))
    return out


def _cluster_exists(project: str, cluster: str, location: str) -> bool | None:
    """Return True if the GKE cluster exists, False if it definitively does not, None if unknown.

    Mirrors platform_mcp_server.verify_gke_cluster's classification: a NotFound/404 is the *only*
    signal that authorizes deletion. Any other failure (auth, network, timeout, quota) returns
    None so the caller leaves the profile in place.
    """
    cmd = [
        "gcloud", "container", "clusters", "describe", cluster,
        f"--location={location}", f"--project={project}", "--format=json(status, id)",
    ]
    try:
        subprocess.run(
            cmd, capture_output=True, text=True, check=True,
            timeout=DESCRIBE_TIMEOUT_SECONDS, env=_run_env(),
        )
        return True
    except subprocess.CalledProcessError as e:
        stderr = e.stderr or ""
        if "NotFound" in stderr or "not found" in stderr.lower() or "404" in stderr:
            return False
        log(f"describe {cluster} ({project}/{location}) failed (treating as unknown): {stderr.strip()}")
        return None
    except subprocess.TimeoutExpired:
        log(f"describe {cluster} ({project}/{location}) timed out (treating as unknown).")
        return None
    except Exception as e:  # noqa: BLE001 - any unexpected failure is 'unknown', never 'absent'
        log(f"describe {cluster} ({project}/{location}) errored (treating as unknown): {e}")
        return None


def reconcile(dry_run: bool = False) -> dict:
    """Reconcile Cluster Agent profiles with the project's clusters (create + prune).

    Returns a structured report dict with the profile names/clusters in each outcome bucket.
    Isolated per-item: one bad profile/cluster never aborts the sweep.
    """
    report: dict[str, list] = {
        "created": [],           # profile scaffolded for a cluster that lacked one
        "pruned": [],            # profile removed (cluster gone, or RECONCILE_EXCLUDE'd)
        "kept": [],              # cluster still exists and should be managed
        "skipped_no_identity": [],  # config.yaml lacked a usable cluster_identity
        "skipped_error": [],     # liveness check was inconclusive (auth/network/etc.)
    }

    profiles = list_profiles()
    identities = {name: read_cluster_identity(profile_home(name)) for name in profiles}
    existing_keys = {
        (i["project"], i["cluster"], i["location"]) for i in identities.values() if i
    }

    # --- CREATE: ensure every project cluster (except RECONCILE_EXCLUDE names) has a
    #     profile. Requires only a resolvable project now that the management cluster is
    #     managed like any other — the metadata-server self-identification this used to
    #     gate on existed solely to recognise the cluster being skipped.
    project = _project()
    if project:
        for (proj, cluster, location) in sorted(_all_clusters(project)):
            if cluster in EXTRA_EXCLUDE or (proj, cluster, location) in existing_keys:
                continue
            if dry_run:
                log(f"{cluster} ({proj}/{location}) has no profile — WOULD create (dry-run).")
                report["created"].append(f"{cluster}/{location}")
                continue
            try:
                name = create_profile(proj, cluster, location)
                log(f"created profile {name} for {cluster} ({proj}/{location}).")
                report["created"].append(name)
            except (SystemExit, Exception) as e:  # noqa: BLE001 - one failure never aborts the sweep
                log(f"create for {cluster} ({proj}/{location}) failed (left unmanaged): {e}")
    else:
        log("could not resolve the project — skipping the CREATE direction this run "
            "(prune-only).")

    # --- PRUNE: remove profiles whose cluster is gone, or whose cluster has since been
    #     added to RECONCILE_EXCLUDE (it must not carry a profile).
    log(f"Reconciling {len(profiles)} managed profile(s){' (dry-run)' if dry_run else ''}.")
    for name in profiles:
        identity = identities[name]
        if identity is None:
            log(f"{name}: no readable cluster_identity — skipping (never delete unverifiable profiles).")
            report["skipped_no_identity"].append(name)
            continue

        # Policy prune: an excluded cluster must not carry a profile, so adding a name to
        # RECONCILE_EXCLUDE removes the profile it already has rather than merely stopping
        # a new one being made.
        if identity["cluster"] in EXTRA_EXCLUDE:
            if dry_run:
                log(f"{name}: {identity['cluster']} is in RECONCILE_EXCLUDE — WOULD prune (dry-run).")
            else:
                log(f"{name}: {identity['cluster']} is in RECONCILE_EXCLUDE — pruning.")
                delete_profile(name)
            report["pruned"].append(name)
            continue

        exists = _cluster_exists(**identity)
        if exists is True:
            report["kept"].append(name)
            continue
        if exists is None:
            report["skipped_error"].append(name)
            continue

        # exists is False -> definitive NotFound -> orphan.
        if dry_run:
            log(f"{name}: cluster {identity['cluster']} ({identity['project']}/{identity['location']}) "
                f"is gone — WOULD prune (dry-run).")
        else:
            log(f"{name}: cluster {identity['cluster']} ({identity['project']}/{identity['location']}) "
                f"is gone — pruning.")
            delete_profile(name)
        report["pruned"].append(name)

    return report


def _format_notification(report: dict) -> str:
    created = report.get("created", [])
    pruned = report.get("pruned", [])
    lines = ["🔧 *Cluster Agent reconcile*"]
    if created:
        lines.append(f"  ➕ created {len(created)} profile(s): "
                     + ", ".join(f"`{n}`" for n in created))
    if pruned:
        lines.append(f"  🧹 pruned {len(pruned)} profile(s):")
        for name in pruned:
            lines.append(f"     • `{name}` (cluster gone or unmanaged)")
    if report.get("skipped_error"):
        lines.append(
            f"  ⚠️ {len(report['skipped_error'])} profile(s) could not be verified this run "
            f"(left untouched): {', '.join(f'`{n}`' for n in report['skipped_error'])}."
        )
    return "\n".join(lines)


def _notify(message: str) -> None:
    """Post a summary to the user's Google Chat home channel (best-effort)."""
    try:
        subprocess.run(
            ["hermes", "send", "--to", "google_chat", message],
            capture_output=True, text=True, check=True, timeout=30, env=_run_env(),
        )
    except Exception as e:  # noqa: BLE001 - notification is best-effort; never fail the run
        log(f"Failed to post reconcile notification: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile Cluster Agent profiles with the project's GKE clusters "
                    "(create for every cluster except RECONCILE_EXCLUDE names; prune orphans)."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be created/pruned without changing anything or notifying.",
    )
    args = parser.parse_args()

    try:
        report = reconcile(dry_run=args.dry_run)
    except Exception as e:  # noqa: BLE001 - resilient: a cron producer must always exit 0
        log(f"Reconcile aborted unexpectedly: {e}")
        return

    log(
        "Done: created={} pruned={} kept={} no_identity={} unknown={}.".format(
            len(report.get("created", [])), len(report.get("pruned", [])),
            len(report.get("kept", [])), len(report.get("skipped_no_identity", [])),
            len(report.get("skipped_error", [])),
        )
    )

    if args.dry_run:
        print(json.dumps(report, indent=2))
        return

    # Notify only when there's something actionable to report (avoid idle hourly noise).
    if report.get("created") or report.get("pruned"):
        _notify(_format_notification(report))


if __name__ == "__main__":
    main()
