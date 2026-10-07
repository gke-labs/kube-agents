#!/usr/bin/env python3
"""Sweep leftover Compute networks, subnets and addresses planted by killed bench runs.

A bench stack that plants project-level Compute resources (such as
prebuilt subnet exhaustion stacks) creates a VPC network, subnet,
and internal addresses. A run killed hard between `tofu apply` and
`tofu destroy` (e.g. deadline, node loss, SIGKILL) leaves those project-level
resources behind.

Why nothing in-job reaches them:
- `hack/ci-teardown.sh` is Kubernetes-only (Helm uninstall, CRD deletes, and
  label-sweeping in-cluster resources).
- `reap_orphans` in `bench/tf/modules/cluster/gke/main.tf` only lists and
  reaps GKE clusters with `resourceLabels.managed-by=kube-agents-bench`; it
  does not run for stacks that create no cluster.
- The Boskos janitor is disabled for the pool (`docs/ci-pool-projects.md`).

Leftover VPCs and subnets count against project quotas and cause subsequent
networking audit evaluations (SOP 2.1) to file false positive critical
`subnet-ip-exhaustion` findings.

This script sweeps Compute addresses, subnets, and networks whose
`description` starts with `kube-agents-bench plant`. In `--project` mode, it
acquires the named project from Boskos out of `free` into `cleaning` and refuses
if it is leased or busy, ensuring active evaluation runs are not swept. Gating is
rooted on the VPC network: plant networks older than `max_age_hours` (default: 4 hours,
with unparseable or missing timestamps treated as old) are selected along with their
child plant subnets and internal addresses, while plant subnets and addresses attached
to non-plant networks (or orphaned) are gated on their own creation timestamp.

Deletion order is strictly dependency-ordered:
1. Addresses go first (releasing in-use IP reservations on the subnet).
2. Subnets go second (removing the subnet from the VPC).
3. Networks go third (deleting the empty VPC).
"""

import argparse
from datetime import datetime, timezone
import json
import math
import os
import pathlib
import re
import signal
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import boskos_pool  # noqa: E402

PLANT_DESCRIPTION_PREFIX = "kube-agents-bench plant"
DEFAULT_MAX_AGE_HOURS = 4.0
MIN_MAX_AGE_HOURS = 2.0
SECONDS_PER_HOUR = 3600.0

REPORT_FILE = "compute-sweep.json"
REPORT_SCHEMA_VERSION = 1
MODE_PROJECT = "project"
MODE_POOL = "pool"
EXIT_NAME_OK = "ok"
EXIT_NAME_FAILED = "failed"
EXIT_NAME_TERMINATED = "terminated"
EXIT_NAME_ERROR = "error"
ISO_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
ARTIFACTS_ENV = "ARTIFACTS"

BOSKOS_DEFAULT_SERVER = boskos_pool.DEFAULT_SERVER
BOSKOS_SWEEP_STATE = "cleaning"
DEFAULT_BOSKOS_OWNER = "ci-kube-agents-compute-sweep"
BOSKOS_STRANDED_AFTER = "5m"
TERMINATED_EXIT_CODE = boskos_pool.TERMINATED_EXIT_CODE
Terminated = boskos_pool.Terminated

CI_DEPLOY_SCRIPT = pathlib.Path(__file__).resolve().parent / "ci-deploy.sh"
MAPPING_FUNCTION = "gitops_repo_for_project"
MAPPING_LINE_RE = re.compile(r'^\s+([A-Za-z0-9-]+)\)\s+echo "([^"/]+/[^"]+)"\s+;;\s*$')


class SweepError(Exception):
    """A sweep operation failed for a project."""

    def __init__(self, message: str, deleted: dict | None = None):
        super().__init__(message)
        self.deleted = deleted or {"addresses": [], "subnets": [], "networks": []}


def parse_timestamp(ts_str: str | None) -> datetime | None:
    """Parse an RFC 3339 / ISO 8601 timestamp string from GCP."""
    if not ts_str or not isinstance(ts_str, str):
        return None
    try:
        # Handles Z and explicit timezone offsets (+00:00, -07:00)
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def is_older_than(ts_str: str | None, max_age_hours: float, now: datetime | None = None) -> bool:
    """Check if the given GCP creationTimestamp is older than max_age_hours.

    An unreadable or missing creationTimestamp on a plant resource is treated
    as old so an unparseable timestamp does not leave leftover plant leaks
    unselected forever.
    """
    dt = parse_timestamp(ts_str)
    if dt is None:
        return True
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - dt).total_seconds() >= max_age_hours * SECONDS_PER_HOUR


def matches_plant_description(description: str | None) -> bool:
    """True if description starts with the fixed bench plant prefix."""
    if not description or not isinstance(description, str):
        return False
    return description.strip().startswith(PLANT_DESCRIPTION_PREFIX)


def resource_name(url_or_name: str | None) -> str:
    """Extract trailing resource name from a full GCP URL, URI, or name."""
    if not url_or_name or not isinstance(url_or_name, str):
        return ""
    return url_or_name.rstrip("/").split("/")[-1]


def pool_projects(ci_deploy_script=CI_DEPLOY_SCRIPT) -> set[str]:
    """Set of project IDs mapped in gitops_repo_for_project() in hack/ci-deploy.sh."""
    projects = set()
    try:
        text = pathlib.Path(ci_deploy_script).read_text(encoding="utf-8")
    except OSError as exc:
        raise SweepError("could not read %s: %s" % (ci_deploy_script, exc))
    in_func = False
    for line in text.splitlines():
        if not in_func:
            if MAPPING_FUNCTION in line and "()" in line:
                in_func = True
            continue
        if line.strip() == "}":
            break
        m = MAPPING_LINE_RE.match(line)
        if m:
            projects.add(m.group(1))
    if not projects:
        raise SweepError("no project mappings found in %s:%s()" % (ci_deploy_script, MAPPING_FUNCTION))
    return projects


def list_compute_resources(project: str, resource_type: str, runner=subprocess.run) -> list[dict]:
    """List Compute resources of a given type in a project as JSON dicts."""
    cmd = ["gcloud", "compute", *resource_type.split(), "list", f"--project={project}", "--format=json"]
    proc = runner(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        raise SweepError(f"could not list {resource_type} in {project}: {err}")
    try:
        items = json.loads(proc.stdout or "[]")
    except ValueError as exc:
        raise SweepError(f"gcloud output for {resource_type} in {project} is not JSON: {exc}")
    if not isinstance(items, list):
        raise SweepError(f"gcloud output for {resource_type} in {project} is not a list")
    return items


def sweep_project(
    project: str,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    dry_run: bool = False,
    runner=subprocess.run,
    now: datetime | None = None,
) -> dict:
    """Sweep planted Compute resources in one project older than max_age_hours.

    Gating is rooted on the VPC network: plant networks older than max_age_hours
    are selected, and all plant subnets and addresses belonging to those networks
    are selected regardless of their individual creation timestamp (avoiding
    red runs when tofu apply creates the dependency chain oldest-first across
    the age threshold). Standalone/orphaned subnets or addresses are gated by
    their own creationTimestamp.

    Deletes addresses first, then subnets, then networks.
    Returns a dict with lists of deleted resources:
      {"addresses": [...], "subnets": [...], "networks": [...]}
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # 1. Inspect networks first (root of dependency chain)
    raw_networks = list_compute_resources(project, "networks", runner=runner)
    plant_network_names = {
        net.get("name") for net in raw_networks
        if net.get("name") and matches_plant_description(net.get("description"))
    }
    networks_to_delete = [
        net for net in raw_networks
        if matches_plant_description(net.get("description"))
        and is_older_than(net.get("creationTimestamp"), max_age_hours, now=now)
    ]
    selected_network_names = {
        net.get("name") for net in networks_to_delete if net.get("name")
    }

    # 2. Inspect subnets
    raw_subnets = list_compute_resources(project, "networks subnets", runner=runner)
    subnets_to_delete = []
    for sub in raw_subnets:
        if not matches_plant_description(sub.get("description")):
            continue
        sub_name = sub.get("name", "")
        net_name = resource_name(sub.get("network"))
        if net_name and net_name in selected_network_names:
            subnets_to_delete.append(sub)
        elif net_name and net_name in plant_network_names and net_name not in selected_network_names:
            # Parent is a plant network younger than max_age_hours: protect it
            print(f"  skipping subnet {sub_name}: parent plant network {net_name} is younger than {max_age_hours}h")
        elif is_older_than(sub.get("creationTimestamp"), max_age_hours, now=now):
            # Parent is not a plant network (e.g. standalone/mock without network,
            # parent already deleted, or parent is a standing non-plant network like default):
            # gate on the subnet's own age
            subnets_to_delete.append(sub)

    selected_subnet_keys = {
        (resource_name(sub.get("region")), sub.get("name"))
        for sub in subnets_to_delete
        if sub.get("name")
    }
    selected_subnet_self_links = {
        sub.get("selfLink") for sub in subnets_to_delete if sub.get("selfLink")
    }
    plant_subnet_keys = {
        (resource_name(sub.get("region")), sub.get("name"))
        for sub in raw_subnets
        if sub.get("name") and matches_plant_description(sub.get("description"))
    }
    plant_subnet_self_links = {
        sub.get("selfLink")
        for sub in raw_subnets
        if sub.get("selfLink") and matches_plant_description(sub.get("description"))
    }

    # 3. Inspect addresses
    raw_addresses = list_compute_resources(project, "addresses", runner=runner)
    addresses_to_delete = []
    for addr in raw_addresses:
        if not matches_plant_description(addr.get("description")):
            continue
        addr_name = addr.get("name", "")
        sub_ref = addr.get("subnetwork")
        sub_name = resource_name(sub_ref)
        addr_region = resource_name(addr.get("region"))
        net_name = resource_name(addr.get("network"))

        if net_name and net_name in selected_network_names:
            addresses_to_delete.append(addr)
            continue

        subnet_matches = False
        if sub_ref and sub_ref in selected_subnet_self_links:
            subnet_matches = True
        elif sub_name and (addr_region, sub_name) in selected_subnet_keys:
            subnet_matches = True
        elif sub_name and not addr_region and any(k[1] == sub_name for k in selected_subnet_keys):
            # Standalone or mock fixtures where neither specifies region
            subnet_matches = True

        if subnet_matches:
            addresses_to_delete.append(addr)
            continue

        # If the address explicitly references an active (unselected) plant network, protect it
        if net_name and net_name in plant_network_names and net_name not in selected_network_names:
            print(f"  skipping address {addr_name}: parent plant network {net_name} is younger than {max_age_hours}h")
            continue

        # If the address explicitly references an active (unselected) plant subnet, protect it
        is_young_plant_subnet = False
        if sub_ref and sub_ref in plant_subnet_self_links:
            is_young_plant_subnet = True
        elif sub_name and (addr_region, sub_name) in plant_subnet_keys:
            is_young_plant_subnet = True
        elif sub_name and not addr_region and any(k[1] == sub_name for k in plant_subnet_keys):
            is_young_plant_subnet = True

        if is_young_plant_subnet:
            print(f"  skipping address {addr_name}: parent plant subnet {sub_name} is younger than {max_age_hours}h")
            continue

        # Otherwise (parent is a non-plant network/subnet, or parent deleted, or standalone):
        # gate on the address's own age
        if is_older_than(addr.get("creationTimestamp"), max_age_hours, now=now):
            addresses_to_delete.append(addr)

    deleted = {"addresses": [], "subnets": [], "networks": []}
    failed = {"addresses": [], "subnets": [], "networks": []}

    try:
        # Step 1: Delete addresses
        for addr in addresses_to_delete:
            name = addr.get("name", "")
            region = addr.get("region")
            if dry_run:
                print(f"  would delete address {name} in {project}")
                deleted["addresses"].append(name)
                continue

            cmd = ["gcloud", "compute", "addresses", "delete", name, f"--project={project}"]
            if region:
                region_name = region.split("/")[-1]
                cmd.append(f"--region={region_name}")
            else:
                cmd.append("--global")
            cmd.append("--quiet")

            del_proc = runner(cmd, capture_output=True, text=True, check=False)
            if del_proc.returncode == 0:
                print(f"  deleted address {name} in {project}")
                deleted["addresses"].append(name)
            else:
                err = (del_proc.stderr or "").strip()
                print(f"  could not delete address {name} in {project}: {err}", file=sys.stderr)
                failed["addresses"].append((name, err))

        # Step 2: Delete subnets
        for sub in subnets_to_delete:
            name = sub.get("name", "")
            region = sub.get("region")
            if dry_run:
                print(f"  would delete subnet {name} in {project}")
                deleted["subnets"].append(name)
                continue

            cmd = ["gcloud", "compute", "networks", "subnets", "delete", name, f"--project={project}"]
            if region:
                region_name = region.split("/")[-1]
                cmd.append(f"--region={region_name}")
            cmd.append("--quiet")

            del_proc = runner(cmd, capture_output=True, text=True, check=False)
            if del_proc.returncode == 0:
                print(f"  deleted subnet {name} in {project}")
                deleted["subnets"].append(name)
            else:
                err = (del_proc.stderr or "").strip()
                print(f"  could not delete subnet {name} in {project}: {err}", file=sys.stderr)
                failed["subnets"].append((name, err))

        # Step 3: Delete networks
        for net in networks_to_delete:
            name = net.get("name", "")
            if dry_run:
                print(f"  would delete network {name} in {project}")
                deleted["networks"].append(name)
                continue

            cmd = ["gcloud", "compute", "networks", "delete", name, f"--project={project}", "--quiet"]
            del_proc = runner(cmd, capture_output=True, text=True, check=False)
            if del_proc.returncode == 0:
                print(f"  deleted network {name} in {project}")
                deleted["networks"].append(name)
            else:
                err = (del_proc.stderr or "").strip()
                print(f"  could not delete network {name} in {project}: {err}", file=sys.stderr)
                failed["networks"].append((name, err))
    except Terminated as exc:
        setattr(exc, "deleted", deleted)
        raise

    total_failed = len(failed["addresses"]) + len(failed["subnets"]) + len(failed["networks"])
    if total_failed > 0:
        fault_msgs = []
        for kind, err_list in failed.items():
            if err_list:
                fault_msgs.append(f"{kind}: {', '.join(f'{n} ({e})' for n, e in err_list)}")
        raise SweepError(f"{project}: {'; '.join(fault_msgs)}", deleted=deleted)

    verb = "would delete" if dry_run else "deleted"
    print(
        f"{project}: {verb} {len(deleted['addresses'])} address(es), "
        f"{len(deleted['subnets'])} subnet(s), {len(deleted['networks'])} network(s)"
    )
    return deleted


def boskos_reset_stranded(server: str):
    """Return projects left in BOSKOS_SWEEP_STATE by an earlier dead sweep run to free."""
    return boskos_pool.reset_stranded(server, BOSKOS_SWEEP_STATE, BOSKOS_STRANDED_AFTER, "compute-sweep")


def sweep_pool(
    server: str,
    owner: str,
    max_age_hours: float,
    projects: set[str],
    dry_run: bool = False,
    runner=subprocess.run,
    report: dict | None = None,
    now: datetime | None = None,
) -> tuple[dict, dict, list]:
    """Sweep every free project Boskos hands out.

    Returns (deleted_by_project, failures_by_project, unmapped).
    """
    boskos_reset_stranded(server)
    report = report if report is not None else {}
    deleted = report.setdefault("deleted", {})
    failures = report.setdefault("failures", {})
    unmapped = report.setdefault("unmapped", [])
    skipped = report.setdefault("skipped", [])
    report.setdefault("ended_early", None)

    def visit(name: str):
        if name not in projects:
            print(f"skipping {name}: not in mapped pool projects")
            unmapped.append(name)
            return
        print(f"sweeping {name}")
        try:
            res = sweep_project(name, max_age_hours=max_age_hours, dry_run=dry_run, runner=runner, now=now)
            deleted[name] = {
                "addresses": len(res.get("addresses", [])),
                "subnets": len(res.get("subnets", [])),
                "networks": len(res.get("networks", [])),
            }
        except Terminated as exc:
            report["ended_early"] = str(exc)
            del_counts = getattr(exc, "deleted", None)
            if del_counts and (del_counts.get("addresses") or del_counts.get("subnets") or del_counts.get("networks")):
                deleted[name] = {
                    "addresses": len(del_counts.get("addresses", [])),
                    "subnets": len(del_counts.get("subnets", [])),
                    "networks": len(del_counts.get("networks", [])),
                }
            failures[name] = str(exc)
            raise
        except Exception as exc:
            print(f"  {name}: {boskos_pool.describe(exc)}", file=sys.stderr)
            del_counts = getattr(exc, "deleted", None)
            if del_counts and (del_counts.get("addresses") or del_counts.get("subnets") or del_counts.get("networks")):
                deleted[name] = {
                    "addresses": len(del_counts.get("addresses", [])),
                    "subnets": len(del_counts.get("subnets", [])),
                    "networks": len(del_counts.get("networks", [])),
                }
            failures[name] = boskos_pool.describe(exc)

    boskos_pool.walk(
        server,
        owner,
        BOSKOS_SWEEP_STATE,
        len(projects),
        visit,
        heartbeat=True,
        release_failures=failures,
    )

    total_addr = sum(d.get("addresses", 0) for d in deleted.values())
    total_sub = sum(d.get("subnets", 0) for d in deleted.values())
    total_net = sum(d.get("networks", 0) for d in deleted.values())
    print(
        f"swept {len(set(deleted) | set(failures))} project(s): "
        f"deleted {total_addr} address(es), {total_sub} subnet(s), {total_net} network(s); "
        f"{len(failures)} failed, {len(unmapped)} unmapped, {len(skipped)} skipped"
    )
    return deleted, failures, unmapped


def default_report_path() -> str | None:
    artifacts = os.environ.get(ARTIFACTS_ENV)
    return str(pathlib.Path(artifacts) / REPORT_FILE) if artifacts else None


def write_report(path: str | None, args: argparse.Namespace, run: dict, code: int, error: str | None, started: float):
    """Write run summary as a JSON document."""
    if not path:
        return
    names = {0: EXIT_NAME_OK, 1: EXIT_NAME_FAILED, TERMINATED_EXIT_CODE: EXIT_NAME_TERMINATED}
    document = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "mode": MODE_PROJECT if args.project else MODE_POOL,
        "dry_run": bool(args.dry_run),
        "max_age_hours": args.max_age_hours,
        "started_at": time.strftime(ISO_UTC_FORMAT, time.gmtime(started)),
        "finished_at": time.strftime(ISO_UTC_FORMAT, time.gmtime(time.time())),
        "exit": names.get(code, EXIT_NAME_ERROR),
        "exit_code": code,
        "error": error,
        "ended_early": run.get("ended_early"),
        "deleted": run.get("deleted", {}),
        "failures": run.get("failures", {}),
        "unmapped": run.get("unmapped", []),
        "skipped": run.get("skipped", []),
    }
    try:
        target = pathlib.Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"could not write report to {path}: {exc}", file=sys.stderr)


def parse_max_age_hours(val: str) -> float:
    """Parse and validate --max-age-hours: positive, finite, at or above MIN_MAX_AGE_HOURS."""
    try:
        v = float(val)
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(f"invalid float value: {val!r}")
    if math.isnan(v) or math.isinf(v) or v < MIN_MAX_AGE_HOURS:
        raise argparse.ArgumentTypeError(
            f"--max-age-hours must be a finite number >= {MIN_MAX_AGE_HOURS} (got {val!r}): "
            "a lower threshold risks sweeping active runs"
        )
    return v


def main(argv=None) -> int:
    description = (__doc__ or "").splitlines()[0]
    parser = argparse.ArgumentParser(description=description)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pool", action="store_true", help="sweep every project Boskos reports free")
    mode.add_argument("--project", help="sweep one project after acquiring it from Boskos")
    parser.add_argument(
        "--max-age-hours",
        type=parse_max_age_hours,
        default=DEFAULT_MAX_AGE_HOURS,
        help=f"minimum age in hours of plant networks to sweep (default: {DEFAULT_MAX_AGE_HOURS}, min: {MIN_MAX_AGE_HOURS})",
    )
    parser.add_argument("--dry-run", action="store_true", help="report what would be deleted, delete nothing")
    parser.add_argument(
        "--ci-deploy-script",
        default=str(CI_DEPLOY_SCRIPT),
        help="where gitops_repo_for_project() lives (default: beside this script)",
    )
    parser.add_argument(
        "--boskos-server",
        default=os.environ.get("BOSKOS_SERVER", BOSKOS_DEFAULT_SERVER),
        help=f"Boskos API endpoint (default: $BOSKOS_SERVER, else {BOSKOS_DEFAULT_SERVER})",
    )
    parser.add_argument(
        "--boskos-owner",
        default=os.environ.get("BOSKOS_OWNER") or DEFAULT_BOSKOS_OWNER,
        help=f"owner name the acquisitions are recorded under (default: $BOSKOS_OWNER, else {DEFAULT_BOSKOS_OWNER})",
    )
    parser.add_argument(
        "--report",
        default=default_report_path(),
        help="where to write the JSON report (default: $ARTIFACTS/compute-sweep.json when set)",
    )

    args = parser.parse_args(argv)
    for sig in boskos_pool.TERMINATION_SIGNALS:
        signal.signal(sig, boskos_pool.terminate)
    started = time.time()
    run = {"deleted": {}, "failures": {}, "unmapped": [], "skipped": []}
    code = 0
    error = None

    try:
        if args.project:
            boskos_reset_stranded(args.boskos_server)
            release_failures = {}

            def visit(p):
                res = sweep_project(p, max_age_hours=args.max_age_hours, dry_run=args.dry_run)
                run["deleted"][p] = {
                    "addresses": len(res.get("addresses", [])),
                    "subnets": len(res.get("subnets", [])),
                    "networks": len(res.get("networks", [])),
                }

            outcome = None
            try:
                outcome = boskos_pool.acquire_and_hold(
                    args.boskos_server,
                    args.boskos_owner,
                    BOSKOS_SWEEP_STATE,
                    lambda: boskos_pool.acquire(
                        args.boskos_server, args.boskos_owner, BOSKOS_SWEEP_STATE, name=args.project
                    ),
                    visit,
                    release_failures,
                    heartbeat=True,
                )
            except SweepError as exc:
                code = 1
                error = str(exc)
                del_counts = getattr(exc, "deleted", None)
                if del_counts and (del_counts.get("addresses") or del_counts.get("subnets") or del_counts.get("networks")):
                    run["deleted"][args.project] = {
                        "addresses": len(del_counts.get("addresses", [])),
                        "subnets": len(del_counts.get("subnets", [])),
                        "networks": len(del_counts.get("networks", [])),
                    }
                run["failures"][args.project] = str(exc)
                print(f"ERROR: {exc}", file=sys.stderr)
            except Terminated as exc:
                del_counts = getattr(exc, "deleted", None)
                if del_counts and (del_counts.get("addresses") or del_counts.get("subnets") or del_counts.get("networks")):
                    run["deleted"][args.project] = {
                        "addresses": len(del_counts.get("addresses", [])),
                        "subnets": len(del_counts.get("subnets", [])),
                        "networks": len(del_counts.get("networks", [])),
                    }
                run["failures"][args.project] = str(exc)
                raise
            finally:
                if args.project in release_failures:
                    code = 1
                    before = run["failures"].get(args.project)
                    run["failures"][args.project] = ("%s; " % before if before else "") + release_failures[args.project]
                    if not error:
                        error = release_failures[args.project]

            if outcome is boskos_pool.NOT_ACQUIRED:
                code = 1
                error = f"project {args.project} is not free in Boskos (leased or busy)"
                run["failures"][args.project] = error
                print(f"ERROR: {error}", file=sys.stderr)
        else:
            projects = pool_projects(args.ci_deploy_script)
            deleted, failures, unmapped = sweep_pool(
                args.boskos_server,
                args.boskos_owner,
                args.max_age_hours,
                projects,
                dry_run=args.dry_run,
                report=run,
            )
            if failures:
                code = 1
                error = f"{len(failures)} project(s) not fully swept: {', '.join(sorted(failures))}"
                print(f"ERROR: {error}", file=sys.stderr)
    except Terminated as exc:
        code = TERMINATED_EXIT_CODE
        error = f"terminated ({exc})"
        print(f"ERROR: {error}", file=sys.stderr)
    except Exception as exc:
        code = 1
        error = f"{type(exc).__name__}: {exc}"
        print(f"ERROR: {error}", file=sys.stderr)
    except BaseException as exc:
        code = 1
        error = f"{type(exc).__name__}: {exc}"
        print(f"ERROR: {error}", file=sys.stderr)
        raise
    finally:
        write_report(args.report, args, run, code, error, started)

    return code


if __name__ == "__main__":
    sys.exit(main())
