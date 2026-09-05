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
# resilient (always exit 0 on the cron path) and posts a summary to every configured chat
# platform only when it created or pruned. `--require-create-pass` opts out of that for a caller
# that has to know whether the roster is actually reconciled; the bootstrap scan gate is the only
# one.

import argparse
import fcntl
import json
import os
import subprocess
import sys
import urllib.request
from contextlib import contextmanager
from pathlib import Path

import sandbox_exec
from chat_platforms import enabled_chat_platforms
from cluster_agent_profile import (
    HERMES_BIN,
    RESERVED_PROFILES,  # noqa: F401 - re-exported for callers/tests; used indirectly via list_profiles
    create_profile,
    delete_profile,
    kubeconfig_landed,
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
    """HOME -> /tmp so a subprocess can write on the writable scratch disk.

    For `hermes` only. Every gcloud call in this file goes through
    `sandbox_exec.run`, which runs it in the shell sandbox and builds its own
    environment there — this one carries the agent pod's, including
    `API_SERVER_KEY`, and must not travel over the connection.
    """
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
        r = sandbox_exec.run(["gcloud", "config", "get-value", "project"], timeout=30)
        return r.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _all_clusters(project: str) -> list | None:
    """Every cluster in the project as (project, name, location) tuples.

    `check=True` matters: without it a failed `gcloud` (expired auth, no network,
    revoked permission) returns a non-zero exit with empty stdout, which parses to
    an empty list and is indistinguishable from "this project has no clusters".

    None means the list could not be read; `[]` means the project genuinely has no
    clusters. The caller degrades identically either way — PRUNE runs off
    `_cluster_exists`, not this list, so a bad list can never delete anything — but
    only the caller can tell the bootstrap gate whether the roster it is about to
    read was actually reconciled.
    """
    try:
        r = sandbox_exec.run(
            ["gcloud", "container", "clusters", "list", "--project", project,
             "--format=value(name,location)"],
            check=True, timeout=120,
        )
    except subprocess.CalledProcessError as e:
        # CalledProcessError stringifies to just the exit status; gcloud puts the
        # actual reason on stderr, which is the only part worth reading.
        log(f"listing clusters in {project} failed (skipping create this run): "
            f"{(e.stderr or '').strip() or e}")
        return None
    except Exception as e:  # noqa: BLE001 - timeout, gcloud missing, OSError
        log(f"listing clusters in {project} failed (skipping create this run): {e}")
        return None
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
        sandbox_exec.run(cmd, check=True, timeout=DESCRIBE_TIMEOUT_SECONDS)
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


# Distinct from 1 so a caller can tell "the roster is not reconciled" from a crash.
EXIT_CREATE_PASS_SKIPPED = 3
# Another reconcile holds the lock. Also distinct from 1: the caller has learned
# nothing about the roster and should retry rather than count this as a failure.
EXIT_ALREADY_RUNNING = 4

RECONCILE_LOCK = ".cluster_agent_reconcile.lock"


@contextmanager
def _exclusive_run():
    """Hold the reconcile lock, or yield False if another run already has it.

    Two schedules drive this script — the hourly `cluster-agent-reconcile` job and
    the bootstrap scan gate, which runs it every minute until the roster is usable —
    and the gateway's cron lock is per job id, so nothing upstream keeps the two
    apart. Overlapping runs would call `create_profile` and `delete_profile` against
    the same profile home: interleaved read-modify-writes of `config.yaml` and
    `.env`, or an rmtree under a scaffold in progress. The lock lives here rather
    than in either caller because it has to cover both.
    """
    path = Path(os.environ.get("HERMES_HOME", "/opt/data")) / RECONCILE_LOCK
    try:
        handle = open(path, "w")  # noqa: SIM115 - closed by this contextmanager
    except Exception as e:  # noqa: BLE001 - an unlockable path must not block the roster
        log(f"could not open {path} ({e}); running without the lock.")
        yield True
        return
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True

# Written by create_profile after the identity stamp, so their absence means the
# scaffold was interrupted between the two. The kubeconfig is checked separately:
# it is not on this pod's filesystem.
SCAFFOLD_ARTIFACTS = ("USER.md",)

# What create_profile fetches in step 3, relative to the profile home. Named here
# because this pod cannot stat it -- the path is resolved on whichever side
# kubectl runs, which kubeconfig_landed decides.
KUBECONFIG_ARTIFACT = "kubeconfig.yaml"


def _scaffold_gaps(home: Path) -> list[str]:
    """Artifacts create_profile writes after the identity stamp that this home lacks.

    ``create_profile`` stamps ``cluster_identity`` into ``config.yaml`` (step 2b)
    before it fetches the kubeconfig (step 3) and writes ``USER.md`` (step 4). A
    process killed in that window -- the bootstrap gate runs this script under a
    240s timeout, and Python SIGKILLs on expiry -- leaves a home that reads as fully
    managed: CREATE finds its identity tuple and skips the cluster, PRUNE keeps it
    because the cluster still exists, and the half-scaffolded profile survives with
    no credentials for the life of the volume. Treating it as absent re-runs the
    scaffold, which is idempotent.

    The kubeconfig is asked for over the sandbox rather than stat'ed here. With a
    sandbox, ``gcloud container clusters get-credentials`` runs in the shell pod
    and writes to the shell pod's volume, so this pod never sees the file:
    stat'ing it locally reports every profile incomplete on every tick, which
    re-scaffolds the whole fleet hourly and re-fetches a credential for each.
    ``kubeconfig_landed`` asks the side that has it -- the same way create_profile
    confirmed the fetch -- and answers "not landed" when the sandbox cannot be
    reached, which is the case a recreated sandbox volume actually needs.
    """
    gaps = [f for f in SCAFFOLD_ARTIFACTS if not (home / f).exists()]
    if not kubeconfig_landed(home / KUBECONFIG_ARTIFACT):
        gaps.insert(0, KUBECONFIG_ARTIFACT)
    return gaps


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
        "incomplete": [],        # identity stamped but the scaffold never finished
        "create_failed": [],     # cluster that should have a profile and could not get one
    }
    # Not a bucket: whether the CREATE direction ran at all this run. Every failure
    # below is caught and logged so a cron producer can always exit 0, which leaves
    # a caller no way to tell "this project has no clusters to add" from "the list
    # call failed". `--require-create-pass` turns this into an exit code for the one
    # caller that needs the difference.
    report["create_pass_ran"] = False

    profiles = list_profiles()
    identities = {name: read_cluster_identity(profile_home(name)) for name in profiles}
    existing_keys = set()
    for name, identity in identities.items():
        if not identity:
            continue
        missing = _scaffold_gaps(profile_home(name))
        if missing:
            log(f"{name}: incomplete scaffold ({', '.join(missing)} missing) — recreating.")
            report["incomplete"].append(name)
            continue
        existing_keys.add((identity["project"], identity["cluster"], identity["location"]))

    # --- CREATE: ensure every project cluster (except RECONCILE_EXCLUDE names) has a
    #     profile. Requires only a resolvable project now that the management cluster is
    #     managed like any other — the metadata-server self-identification this used to
    #     gate on existed solely to recognise the cluster being skipped.
    project = _project()
    listed = _all_clusters(project) if project else None
    if listed is not None:
        report["create_pass_ran"] = True
        for (proj, cluster, location) in sorted(listed):
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
                report["create_failed"].append(f"{cluster}/{location}")
    elif not project:
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
    failed = report.get("create_failed", [])
    if failed:
        lines.append(
            f"  ❌ {len(failed)} cluster(s) could not be given a profile "
            f"(retried next run): {', '.join(f'`{n}`' for n in failed)}."
        )
    if report.get("skipped_error"):
        lines.append(
            f"  ⚠️ {len(report['skipped_error'])} profile(s) could not be verified this run "
            f"(left untouched): {', '.join(f'`{n}`' for n in report['skipped_error'])}."
        )
    return "\n".join(lines)


def _notify(message: str) -> None:
    """Post a summary to each configured chat platform's home channel (best-effort).

    Stays in the agent pod. `hermes` is not cluster tooling: it needs the
    profiles on the data PVC and the gateway on loopback, neither of which the
    sandbox has, and the sandbox image does not carry the binary.

    The target used to be the literal `google_chat`, which meant a Slack-only
    install never heard that a Cluster Agent profile had been created or pruned:
    the send failed on the missing Google Chat home channel and the `except`
    below turned it into one line of stderr on a run that still exits 0. #989.

    Each platform is sent to independently — a Google Chat outage must not cost
    Slack the summary, and the reverse.
    """
    for platform in enabled_chat_platforms():
        try:
            subprocess.run(
                [HERMES_BIN, "send", "--to", platform, message],
                capture_output=True, text=True, check=True, timeout=30, env=_run_env(),
            )
        except Exception as e:  # noqa: BLE001 - notification is best-effort; never fail the run
            log(f"Failed to post reconcile notification to {platform}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile Cluster Agent profiles with the project's GKE clusters "
                    "(create for every cluster except RECONCILE_EXCLUDE names; prune orphans)."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be created/pruned without changing anything or notifying.",
    )
    parser.add_argument(
        "--require-create-pass", action="store_true",
        help="Exit non-zero if the CREATE direction could not run (no project, or the "
             "cluster list failed), or if it ran and every create failed. Off by default: "
             "the cron producer must always exit 0.",
    )
    args = parser.parse_args()

    with _exclusive_run() as acquired:
        if not acquired:
            log("another reconcile is already running; leaving the roster to it.")
            # The cron producer still exits 0 — an overlap is expected, not an error.
            # Only the caller that asked to be told about the roster hears about it,
            # and what it hears is "ask again", not "reconcile failed".
            if args.require_create_pass:
                raise SystemExit(EXIT_ALREADY_RUNNING)
            return

        try:
            report = reconcile(dry_run=args.dry_run)
        except Exception as e:  # noqa: BLE001 - resilient: a cron producer must always exit 0
            log(f"Reconcile aborted unexpectedly: {e}")
            if args.require_create_pass:
                raise SystemExit(EXIT_CREATE_PASS_SKIPPED)
            return

    log(
        "Done: created={} failed={} pruned={} kept={} no_identity={} unknown={}.".format(
            len(report.get("created", [])), len(report.get("create_failed", [])),
            len(report.get("pruned", [])),
            len(report.get("kept", [])), len(report.get("skipped_no_identity", [])),
            len(report.get("skipped_error", [])),
        )
    )

    # PRUNE walks every profile including the half-built ones: their cluster exists, so
    # they land in `kept` alongside the healthy homes. Counting them as "already in
    # place" would let a run whose only create failed report a reconciled roster from
    # the second tick onward, the tick where the failure's own wreckage is on disk.
    incomplete = set(report.get("incomplete", []))
    kept_scaffolded = [n for n in report.get("kept", []) if n not in incomplete]

    unreconciled = None
    if not report.get("create_pass_ran"):
        unreconciled = "CREATE direction did not run"
    elif report.get("create_failed") and not (report.get("created") or kept_scaffolded):
        # Every create failed and nothing was already in place, so the roster is empty
        # apart from the half-built homes those failures left behind — `create_profile`
        # stamps the identity before it fetches credentials. The caller that gates a
        # one-shot fan-out on this exit code must not file against that; a retry either
        # succeeds or repairs the home on the next run.
        #
        # A partial failure is deliberately not reported here. One unscaffoldable cluster
        # among several costs the sweep one `gaps` row — the audit SOP's preflight branch
        # catches the missing kubeconfig — and holding the whole report back for it buys
        # nothing when the cause is permanent (no IAM, a private control plane).
        unreconciled = "every CREATE failed: " + ", ".join(report["create_failed"])

    if args.dry_run:
        print(json.dumps(report, indent=2))
    else:
        # Notify only when there's something actionable to report (avoid idle hourly
        # noise). A failed create rides along on a run that already has something to say
        # rather than triggering its own message: it repeats every run until the cause is
        # fixed, and the gate re-runs this script every minute during onboarding.
        if report.get("created") or report.get("pruned"):
            _notify(_format_notification(report))

    if args.require_create_pass and unreconciled:
        log(f"{unreconciled}; the roster is not reconciled.")
        raise SystemExit(EXIT_CREATE_PASS_SKIPPED)


if __name__ == "__main__":
    main()
