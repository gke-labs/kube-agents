#!/usr/bin/env python3
"""
networking_audit.py — GCP VPC Networking Fabric & Routing Audit Helper.
Sweeps fleet projects for two checks of governance/gcp_networking_fabric_sop.md:
Private Service Connect forwarding rules (psc-routing-deadlock) and subnet IP
capacity (subnet-ip-exhaustion). `--check` picks one; the default runs both.
The other checks in that SOP are evaluated via SOP commands.
"""

import argparse
import ipaddress
import json
import math
import os
import pathlib
import re
import shlex
import subprocess
import sys

MONITORED_PROJECTS_ENV = "MONITORED_PROJECT_IDS"
PROJECT_ENV_VARS = ("GCP_PROJECT_ID", "GKE_PROJECT_ID", "PROJECT_ID")
GCLOUD = "gcloud"
PROJECT_ID_FORMAT = "--format=value(projectId)"
PROJECTS_LIST_CMD = (GCLOUD, "projects", "list", PROJECT_ID_FORMAT)
CONFIG_PROJECT_CMD = (GCLOUD, "config", "get-value", "project")
PSC_REJECTED_STATUSES = ("REJECTED", "CLOSED")
SERVICE_ATTACHMENT_SUBSTR = "serviceAttachments"
AUDIT_SLUG = "gcp-networking-fabric-audit"
PSC_CHECK_SLUG = "psc-routing-deadlock"
PROJECT_TARGET_PREFIX = "project/"
GLOBAL_LOCATION = "global"
UNKNOWN_PROJECT = "unknown"
# The skipped-target name for "every project `gcloud projects list` would have
# named": the listing failed, so how many other projects the fleet holds is
# unknown and the run must read as partial rather than as a full sweep. Same
# name fleet_drift.py uses, so every stream reports it alike.
UNENUMERATED_PROJECTS = "UNENUMERATED_PROJECTS"
# A run narrowed on purpose -- `--project-id` or `MONITORED_PROJECT_IDS` --
# skips discovery, so it reads the named projects and no other. Without a row
# saying so the document reads as the whole fleet, and `finish` resolves every
# ledger finding on a project the run never looked at. fleet_drift.py records
# the same `project/UNENUMERATED_PROJECTS` row for its `--project` runs.
SCOPED_RUN_NOTE = (
    "scope narrowed to {projects} by {source}: discovery was skipped, so no other project "
    "in this fleet was named or read"
)
ERROR_EXCERPT_CHARS = 300
API_DISABLED = "API_DISABLED"
API_DISABLED_MARKERS = (
    "SERVICE_DISABLED",
    "accessNotConfigured",
    "has not been used in project",
)
# A disabled-API refusal names the consumer project -- by number in gcloud's
# usual phrasing, by id in some. With a quota project set, that consumer is the
# quota project rather than `--project`, so the marker alone cannot say whose
# API is off. fleet_waste.refusal_owner draws the same line.
REFUSED_PROJECT_NUMBER_RE = re.compile(r"\bprojects?[ /](\d+)\b")
PROJECT_DESCRIBE_CMD = (GCLOUD, "projects", "describe")
# A project's number cannot change within a run, and every refused read asks
# for it: a project with the Compute Engine API off refuses five reads here.
# fleet_waste._describing_once answers the repeats from the first result too.
PROJECT_NUMBERS: dict[str, tuple[int, str, str]] = {}
PROJECT_NUMBER_FORMAT = "--format=value(projectNumber)"
PROJECT_FLAG = "--project"
JSON_INDENT = 2
# Long enough for an aggregated `instances list` over every zone of a large
# project, which pages 500 VMs at a time; a read past it is a failed read and
# is named in `limitations`.
GCLOUD_TIMEOUT_SECONDS = 300
CHECK_ALL = "all"
SUBNET_CHECK_SLUG = "subnet-ip-exhaustion"
SUBNET_SEVERITY = "critical"
CHECK_CHOICES = (CHECK_ALL, PSC_CHECK_SLUG, SUBNET_CHECK_SLUG)
# SOP 2.1: a range is flagged when less than this share of it is still free.
AVAILABLE_FRACTION_FLOOR = 0.15
# GCP keeps the network, gateway, second-to-last and broadcast addresses of
# every subnet primary range, so they are used before anything is deployed.
GCP_RESERVED_PRIMARY_ADDRESSES = 4
IPV4_BITS = 32
# Subnets whose addresses VMs, internal addresses and forwarding rules draw
# from. Proxy-only, PSC and private NAT subnets are allocated by Google out of
# band, so no read here can count what they hold.
COUNTABLE_SUBNET_PURPOSES = ("", "PRIVATE", "PRIVATE_RFC_1918")
# The skipped-target leaf for a project whose subnets could not be listed.
UNENUMERATED_SUBNETS = "UNENUMERATED_SUBNETS"
# The skipped-target leaf for a project whose subnet-usage reads failed and
# which owns no subnet entry its failure could be named on.
UNREAD_SUBNET_USAGE = "UNREAD_SUBNET_USAGE"
COMMAND_JOINER = " && "
LIMITATION_JOINER = "; "
SUBNETS_FORMAT = "--format=json(name,region,ipCidrRange,secondaryIpRanges,purpose,selfLink)"
CLUSTERS_FORMAT = "--format=json(name,location,subnetwork,networkConfig,ipAllocationPolicy,nodePools)"
INSTANCES_FORMAT = "--format=json(networkInterfaces[].networkIP,networkInterfaces[].subnetwork)"
ADDRESSES_FILTER = "--filter=addressType=INTERNAL"
ADDRESSES_FORMAT = "--format=json(address,subnetwork)"
FORWARDING_RULES_FORMAT = "--format=json(IPAddress,subnetwork)"
# `gcloud container clusters list` exits 0 when some zones time out and names
# them on stderr, so the listing is partial. Same markers as stall_watch.py's
# INCOMPLETE_LISTING_MARKERS.
INCOMPLETE_LISTING_MARKERS = ("did not respond", "may be incomplete")
# Decimals a free share is rounded to before it is floored to a percentage, so
# float noise (1 - 0.9 = 0.0999...) does not read as one percent less.
PERCENT_ROUNDING_DIGITS = 6
# `[projects/<p>/]regions/<r>/subnetworks/<s>` at the end of a subnet URL or
# partial path; the project is absent from some partial paths.
SUBNET_LINK_RE = re.compile(r"(?:projects/([^/]+)/)?regions/([^/]+)/subnetworks/([^/]+)$")
ZONE_RE = re.compile(r"([a-z]+-[a-z]+\d+)-[a-z]")
# The four other SOP checks, with the reasons the SOP's per-subnet example
# gives for why none of them applies to a subnet entry.
SUBNET_CHECKS_NOT_APPLICABLE = (
    {
        "check": "cloud-nat-exhaustion",
        "reason": "NAT gateways are configured at the Cloud Router level, not per subnet.",
    },
    {
        "check": PSC_CHECK_SLUG,
        "reason": "Private Service Connect endpoints are project-level resources, not subnet resources.",
    },
    {
        "check": "mtu-packet-fragmentation",
        "reason": "VPC network MTU is defined at the VPC level, not per subnet.",
    },
    {
        "check": "cloud-armor-false-positive",
        "reason": "Cloud Armor security policies are backend service resources, not subnet resources.",
    },
)


def run_cmd(cmd: list[str], timeout: int = GCLOUD_TIMEOUT_SECONDS) -> tuple[int, str, str]:
    """Runs a shell command with a timeout and returns (rc, stdout, stderr)."""
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout} seconds"
    except Exception as e:
        return -1, "", str(e)


def project_flag_value(cmd: list[str]) -> str | None:
    """The project a gcloud argv names with `--project <id>` or `--project=<id>`."""
    for i, arg in enumerate(cmd):
        if arg == PROJECT_FLAG and i + 1 < len(cmd):
            return cmd[i + 1]
        if arg.startswith(f"{PROJECT_FLAG}="):
            return arg.split("=", 1)[1]
    return None


def refusal_names_project(project: str, stderr: str) -> tuple[bool, str]:
    """Whether an API-disabled refusal is `project`'s own, with why when it is not.

    Only the project's own refusal means it holds nothing to audit. One naming
    another project -- the credential's quota project -- says nothing about this
    one, and read as empty it would drop the project from both `scope.clusters`
    and `scope.skipped`. A refusal naming no project, or one whose number cannot
    be compared with this project's, is a failed read.
    """
    numbers = set(REFUSED_PROJECT_NUMBER_RE.findall(stderr))
    if not numbers:
        if re.search(rf"\b(?i:projects?)[ /]['\"\[]?{re.escape(project)}(?![\w-])", stderr):
            return True, ""
        return False, f"the refusal does not name {project!r}"
    if project not in PROJECT_NUMBERS:
        PROJECT_NUMBERS[project] = run_cmd([*PROJECT_DESCRIBE_CMD, project, PROJECT_NUMBER_FORMAT])
    rc, stdout, err = PROJECT_NUMBERS[project]
    if rc != 0:
        return False, (
            f"`gcloud projects describe {project}` failed (rc={rc}), so the refusal's project number "
            f"could not be compared: {(err or '').strip()[:ERROR_EXCERPT_CHARS] or 'no stderr'}"
        )
    if numbers == {stdout.strip()}:
        return True, ""
    return False, f"the API is off in a project other than {project!r}, such as a quota project"


def run_gcloud_json(
    cmd: list[str], warnings: list[str] | None = None
) -> tuple[list[dict] | dict | str | None, str | None]:
    """Runs a gcloud command and parses JSON output safely.

    Returns (API_DISABLED, None) only for a refusal naming the `--project` the
    command reads; any other failure returns (None, error_message). A caller
    passing `warnings` gets a successful read's stderr appended to it, which is
    where gcloud says a listing is incomplete.
    """
    rc, stdout, stderr = run_cmd(cmd)
    if rc != 0:
        project = project_flag_value(cmd)
        if project and any(marker in (stderr or "") for marker in API_DISABLED_MARKERS):
            ours, why_not = refusal_names_project(project, stderr)
            if ours:
                return API_DISABLED, None
            stderr = f"{stderr.strip()} ({why_not})"
        sys.stderr.write(f"gcloud command failed ({rc}): {' '.join(cmd)}\n{stderr}\n")
        return None, f"{' '.join(cmd)} failed ({rc}): {stderr.strip()}"
    if warnings is not None and (stderr or "").strip():
        warnings.append(stderr.strip())
    if not stdout.strip():
        return [], None
    try:
        return json.loads(stdout), None
    except Exception as e:
        sys.stderr.write(f"Error parsing gcloud output from {' '.join(cmd)}: {e}\n")
        return None, f"{' '.join(cmd)} returned unparsable JSON: {e}"


def _normalise_project_id(project: str, listing_errors: list[str] | None = None) -> str | None:
    """Resolves a numeric project number (e.g. from spec.harness.projectId) to its projectId."""
    if not project.isdigit():
        return project
    rc, stdout, stderr = run_cmd([*PROJECT_DESCRIBE_CMD, project, PROJECT_ID_FORMAT])
    if rc == 0 and stdout.strip():
        return stdout.strip()
    if listing_errors is not None:
        listing_errors.append(
            f"`gcloud projects describe {project}` rc={rc}: "
            f"{(stderr or '').strip()[:ERROR_EXCERPT_CHARS] or 'no stderr'}; "
            "numeric project could not be resolved to a projectId"
        )
    return None


def get_target_projects(cli_project: str | None = None, listing_errors: list[str] | None = None) -> list[str]:
    """Resolves all target GCP projects to audit.

    Everything that narrows or loses part of the fleet is appended to
    `listing_errors` when the caller passes one, and `main` turns each entry
    into a `project/UNENUMERATED_PROJECTS` skipped target so the run reads as
    partial rather than as the whole fleet:

    - `--project-id` or a non-empty `MONITORED_PROJECT_IDS` narrows the scope
      on purpose and skips discovery;
    - a failed `gcloud projects list` leaves only the env/config project;
    - a listing that succeeds without naming the configured project is
      filtered rather than complete, as fleet_drift.py treats it.

    The host project from `gcloud config get-value project` is always part of
    the discovered scope, alongside any `GCP_PROJECT_ID`-style variable.
    """
    if cli_project and cli_project.strip():
        raw = cli_project.strip()
        project = _normalise_project_id(raw) or raw
        if listing_errors is not None:
            listing_errors.append(SCOPED_RUN_NOTE.format(projects=project, source="`--project-id`"))
        return [project]

    env_projects = {os.environ.get(var, "").strip() for var in PROJECT_ENV_VARS} - {""}
    # Parsed before it is tested, so a blank or separator-only value reads as
    # unset rather than as an override that names nothing and skips discovery.
    monitored = set(os.environ.get(MONITORED_PROJECTS_ENV, "").replace(",", " ").split())
    if monitored:
        projects = {_normalise_project_id(p) or p for p in monitored | env_projects}
        if listing_errors is not None:
            listing_errors.append(
                SCOPED_RUN_NOTE.format(projects=", ".join(sorted(projects)), source=f"`{MONITORED_PROJECTS_ENV}`")
            )
        return sorted(projects)

    raw_projects = set(env_projects)
    rc, stdout, _ = run_cmd(list(CONFIG_PROJECT_CMD))
    if rc == 0 and stdout.strip():
        raw_projects.add(stdout.strip())
    projects = {
        resolved
        for p in sorted(raw_projects)
        if (resolved := _normalise_project_id(p, listing_errors)) is not None
    }

    rc, stdout, stderr = run_cmd(list(PROJECTS_LIST_CMD))
    if rc != 0 and listing_errors is not None:
        listing_errors.append(
            f"`gcloud projects list` rc={rc}: {(stderr or '').strip()[:ERROR_EXCERPT_CHARS] or 'no stderr'}; "
            "the scope fell back to the configured project"
        )
    if rc == 0:
        listed = {line.strip() for line in stdout.splitlines() if line.strip()}
        omitted = sorted(projects - listed)
        if omitted and listing_errors is not None:
            listing_errors.append(
                f"`gcloud projects list` rc=0 did not name {', '.join(omitted)}, so the listing is filtered"
            )
        projects |= listed

    return sorted(projects or raw_projects)


def audit_project_networking(project_id: str, skipped_targets: list, active_targets: list) -> list[dict]:
    """Audits PSC forwarding rules in a project (psc-routing-deadlock).

    A project that cannot be read is recorded in skipped_targets and yields no
    findings; the caller keeps going, because one project the agent lacks
    permission on must not decide the outcome for the rest of the fleet.
    """
    findings = []
    target_name = f"{PROJECT_TARGET_PREFIX}{project_id}"

    # 1. Inspect PSC forwarding rules for disconnected / rejected attachments
    list_cmd = f"gcloud compute forwarding-rules list --project={project_id} --format=json"
    fwd_rules, error = run_gcloud_json(["gcloud", "compute", "forwarding-rules", "list", "--project", project_id, "--format=json"])
    if fwd_rules == API_DISABLED:
        return findings
    if error is not None or not isinstance(fwd_rules, list):
        skipped_targets.append({
            "cluster": target_name,
            "name": target_name,
            "location": GLOBAL_LOCATION,
            "project": project_id,
            "reason": error or f"Failed to list forwarding rules in project {project_id}"
        })
        return findings

    active_targets.append({
        "name": target_name,
        "location": GLOBAL_LOCATION,
        "project": project_id,
        "checks_run": [{"check": PSC_CHECK_SLUG, "command": list_cmd}]
    })

    for fr in fwd_rules:
        name = fr.get("name", "")
        region = fr.get("region", "").split("/")[-1]
        target = fr.get("target", "")
        psc_status = fr.get("pscConnectionStatus", "")
        
        # Only flag when target is a service attachment AND the status is rejected or closed
        if target and SERVICE_ATTACHMENT_SUBSTR in target and psc_status in PSC_REJECTED_STATUSES:
            findings.append({
                "check": PSC_CHECK_SLUG,
                "severity": "major",
                "title": f"Private Service Connect forwarding rule {name} in {region} is in state {psc_status}",
                "cluster": target_name,
                "namespace": "",
                "object": f"ForwardingRule/{name}",
                "impact": f"PSC endpoint {name} cannot route traffic to target service attachment.",
                "evidence": {
                    "command": f"gcloud compute forwarding-rules describe {name} --region={region} --project={project_id} --format=json",
                    "excerpt": f"pscConnectionStatus: {psc_status}"
                },
                "recommendation": {
                    "action": f"Re-establish or re-authorize PSC service attachment connection for {name}.",
                    "rationale": "Service attachment rejected or closed the connection request.",
                    "risk": "Requires verifying target service consumer acceptance list."
                },
                "remediation": {
                    "kind": "gcloud",
                    "path": ""
                }
            })

    return findings

def subnet_commands(project_id: str) -> dict[str, list[str]]:
    """The reads the subnet-capacity sweep issues against one project, by role."""
    project = f"{PROJECT_FLAG}={project_id}"
    return {
        "subnets": [GCLOUD, "compute", "networks", "subnets", "list", project, SUBNETS_FORMAT],
        "clusters": [GCLOUD, "container", "clusters", "list", project, CLUSTERS_FORMAT],
        "instances": [GCLOUD, "compute", "instances", "list", project, INSTANCES_FORMAT],
        "addresses": [GCLOUD, "compute", "addresses", "list", project, ADDRESSES_FILTER, ADDRESSES_FORMAT],
        "forwarding_rules": [GCLOUD, "compute", "forwarding-rules", "list", project, FORWARDING_RULES_FORMAT],
    }


def command_text(*cmds: list[str]) -> str:
    """The literal shell form of one or more argvs, joined the way a shell chains them."""
    return COMMAND_JOINER.join(shlex.join(cmd) for cmd in cmds)


def subnet_key(link: str, default_project: str) -> tuple[str, str, str] | None:
    """(project, region, subnet) from a subnet URL or partial path, or None when it names none.

    Keyed with the project so a VM in a Shared VPC service project, whose NIC
    names the host project's subnet, is never counted against a same-named
    subnet of its own project.
    """
    match = SUBNET_LINK_RE.search(link or "")
    if not match:
        return None
    project, region, subnet = match.groups()
    return project or default_project, region, subnet


def region_of_location(location: str) -> str:
    """The region a cluster `location` lies in: a zone loses its suffix, a region is itself."""
    match = ZONE_RE.fullmatch(location or "")
    return match.group(1) if match else location or ""


def _fraction(value: object) -> float | None:
    """A GKE utilization value as a float, or None when it is absent, unreadable or not in 0-1.

    `json.loads` accepts NaN and Infinity, and a range is flagged on
    `1 - utilization`, so anything but a finite fraction would read as clean.
    """
    try:
        fraction = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return fraction if fraction is not None and math.isfinite(fraction) and 0 <= fraction <= 1 else None


def _prefix(value: object) -> int | None:
    """A per-node Pod block prefix length, or None when it is absent, unreadable or not 0-32."""
    try:
        prefix = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return prefix if prefix is not None and 0 <= prefix <= IPV4_BITS else None


def pod_range_utilization(
    clusters: list, project_id: str, ranges: dict | None = None
) -> dict[tuple[str, str, str, str], dict]:
    """GKE's own Pod-range utilization, keyed by (project, region, subnet, range name).

    The key's project is the subnet's owner, which for a Shared VPC cluster is
    the host project rather than `project_id`, the project whose clusters list
    reported it. Pass `ranges` to merge into the reports of other projects.
    Every report naming a range counts -- the cluster default range, each node
    pool's range and each additional Pod range -- and the highest utilization
    wins. The per-node block prefix kept is the largest block (smallest prefix)
    any pool on the range takes, so "how many more nodes fit" errs low. A
    report whose utilization is not a finite fraction in 0-1 is kept in the
    range's `unreadable` list, and `utilization` stays None until a readable
    report arrives.
    """
    ranges = {} if ranges is None else ranges

    def record(key, raw, prefix, cluster, pool):
        if raw is None or key is None:
            return
        utilization = _fraction(raw)
        entry = ranges.get(key)
        if entry is None:
            entry = ranges[key] = {"utilization": None, "prefix": prefix, "cluster": cluster, "pool": pool,
                                   "project": project_id, "unreadable": []}
        if prefix is not None and (entry["prefix"] is None or prefix < entry["prefix"]):
            entry["prefix"] = prefix
        if utilization is None:
            # The utilization is the measurement: an unreadable one is named on
            # the subnet's entry rather than dropped.
            where = f"cluster {cluster}" + (f", node pool {pool}" if pool else "")
            entry["unreadable"].append(f"{raw!r} ({where})")
            return
        # A pool's report on a tie names more than the cluster default's does.
        if entry["utilization"] is None or (
            (utilization, pool is not None) > (entry["utilization"], entry["pool"] is not None)
        ):
            entry.update(utilization=utilization, cluster=cluster, pool=pool, project=project_id)

    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        name = cluster.get("name", "")
        policy = cluster.get("ipAllocationPolicy") or {}
        pools = [p for p in cluster.get("nodePools") or [] if isinstance(p, dict)]
        bare_subnet = cluster.get("subnetwork", "")
        default_subnet = subnet_key((cluster.get("networkConfig") or {}).get("subnetwork", ""), project_id)
        if default_subnet is None:
            for pool in pools:
                pool_subnet = subnet_key((pool.get("networkConfig") or {}).get("subnetwork", ""), project_id)
                if pool_subnet and pool_subnet[2] == bare_subnet:
                    default_subnet = pool_subnet
                    break
        if default_subnet is None and bare_subnet:
            default_subnet = (project_id, region_of_location(cluster.get("location", "")), bare_subnet)

        def keyed(subnet, range_name):
            return (*subnet, range_name) if subnet and range_name else None

        default_range = policy.get("clusterSecondaryRangeName", "")
        default_prefixes = [
            prefix for pool in pools
            if (pool.get("networkConfig") or {}).get("podRange", default_range) == default_range
            and (prefix := _prefix(pool.get("podIpv4CidrSize"))) is not None
        ]
        record(
            keyed(default_subnet, default_range),
            policy.get("defaultPodIpv4RangeUtilization"),
            min(default_prefixes, default=None),
            name,
            None,
        )
        for info in (policy.get("additionalPodRangesConfig") or {}).get("podRangeInfo") or []:
            if isinstance(info, dict):
                record(keyed(default_subnet, info.get("rangeName", "")), info.get("utilization"),
                       None, name, None)
        for pool in pools:
            net = pool.get("networkConfig") or {}
            pool_subnet = subnet_key(net.get("subnetwork", ""), project_id) or default_subnet
            record(keyed(pool_subnet, net.get("podRange", "")), net.get("podIpv4RangeUtilization"),
                   _prefix(pool.get("podIpv4CidrSize")), name, pool.get("name", ""))
    return ranges


def primary_ips_by_subnet(
    project_id: str, instances: list, addresses: list, forwarding_rules: list
) -> dict[tuple[str, str, str], set[str]]:
    """Unique internal IPv4 addresses per (project, region, subnet) from the three primary reads.

    A reserved address also bound to a VM or a forwarding rule is one address,
    so the sets dedupe by value.
    """
    used: dict[tuple[str, str, str], set[str]] = {}

    def add(link, ip):
        key = subnet_key(link, project_id)
        if key and ip:
            used.setdefault(key, set()).add(ip)

    for instance in instances:
        for nic in (instance.get("networkInterfaces") or []) if isinstance(instance, dict) else []:
            if isinstance(nic, dict):
                add(nic.get("subnetwork", ""), nic.get("networkIP", ""))
    for address in addresses:
        if isinstance(address, dict):
            add(address.get("subnetwork", ""), address.get("address", ""))
    for rule in forwarding_rules:
        if isinstance(rule, dict):
            add(rule.get("subnetwork", ""), rule.get("IPAddress", ""))
    return used


def addresses_in_range(ips: set[str], network: ipaddress.IPv4Network) -> int:
    """How many of `ips` are IPv4 addresses inside `network`."""
    count = 0
    for ip in ips:
        try:
            if ipaddress.ip_address(ip) in network:
                count += 1
        except ValueError:
            continue
    return count


def percent_available(fraction: float) -> int:
    """A free share as a whole percentage, rounded down so a flagged range never reads as 15%.

    Rounded to PERCENT_ROUNDING_DIGITS first, so 1 - 0.9 reads as 10%, not 9%.
    """
    return max(math.floor(round(fraction * 100, PERCENT_ROUNDING_DIGITS)), 0)


def _limitation(read: str, cmd: list[str], error: str | None) -> str:
    excerpt = (error or "no output").strip()[:ERROR_EXCERPT_CHARS]
    return f"{read} not read: `{shlex.join(cmd)}` failed: {excerpt}"


def read_subnet_usage(projects: list[str]) -> dict:
    """First subnet-ip-exhaustion pass: what every project in scope draws from any subnet.

    Clusters, VMs, internal addresses and forwarding rules are read in every
    project and keyed by the subnet's owning project, so a Shared VPC host
    subnet counts the nodes, Pods and load balancers of its service projects.
    Returns the merged Pod-range reports, the unique primary-range addresses
    per subnet, the projects whose reads named an address in each subnet, and
    the limitations of each project's failed or partial reads, keyed by project.
    """
    pod_ranges: dict[tuple[str, str, str, str], dict] = {}
    used_ips: dict[tuple[str, str, str], set[str]] = {}
    readers: dict[tuple[str, str, str], set[str]] = {}
    limitations: dict[str, list[str]] = {}
    for project_id in projects:
        cmds = subnet_commands(project_id)
        own = limitations.setdefault(project_id, [])
        warnings: list[str] = []
        clusters, error = run_gcloud_json(cmds["clusters"], warnings=warnings)
        if clusters == API_DISABLED:
            clusters = []
        elif error is not None or not isinstance(clusters, list):
            own.append(
                _limitation("Pod ranges", cmds["clusters"], error) + "; Pod ranges of its clusters were not measured"
            )
            clusters = []
        partial = next((w for w in warnings if any(m in w for m in INCOMPLETE_LISTING_MARKERS)), None)
        if partial:
            own.append(
                f"Pod ranges partially read: `{shlex.join(cmds['clusters'])}` warned: "
                f"{partial[:ERROR_EXCERPT_CHARS]}"
            )
        pod_range_utilization(clusters, project_id, pod_ranges)

        primary_reads = {}
        for role, read in (("instances", "VM NICs"), ("addresses", "internal addresses"),
                           ("forwarding_rules", "forwarding rules")):
            items, error = run_gcloud_json(cmds[role])
            if items == API_DISABLED:
                items = []
            elif error is not None or not isinstance(items, list):
                own.append(
                    _limitation(read, cmds[role], error) + "; primary-range use is counted without them"
                )
                items = []
            primary_reads[role] = items
        for key, ips in primary_ips_by_subnet(project_id, primary_reads["instances"], primary_reads["addresses"],
                                              primary_reads["forwarding_rules"]).items():
            used_ips.setdefault(key, set()).update(ips)
            readers.setdefault(key, set()).add(project_id)
    return {"pod_ranges": pod_ranges, "used_ips": used_ips, "readers": readers, "limitations": limitations}


def subnet_entry(target: str, region: str, project_id: str, command: str, limitations: list[str]) -> dict:
    """A `<project>/<region>/<subnet>` scope entry, in the SOP example's per-subnet shape."""
    entry = {
        "name": target,
        "location": region,
        "project": project_id,
        "checks_run": [{"check": SUBNET_CHECK_SLUG, "command": command}],
        "checks_not_applicable": [dict(na) for na in SUBNET_CHECKS_NOT_APPLICABLE],
    }
    if limitations:
        entry["limitations"] = LIMITATION_JOINER.join(limitations)
    return entry


def audit_subnet_capacity(projects: list[str], usage: dict, skipped_targets: list,
                          active_targets: list) -> list[dict]:
    """Second subnet-ip-exhaustion pass: each listed subnet against `read_subnet_usage`'s maps.

    One `<project>/<region>/<subnet>` scope entry per countable subnet. Pod
    ranges are measured from GKE's own utilization fields; the primary range
    as a lower bound from VM NICs, internal addresses and forwarding rules,
    plus the four addresses GCP reserves. A failed subnet listing skips that
    project. A Pod range on a subnet no listing named -- a Shared VPC host
    project outside the scope, or one whose listing failed -- still gets an
    entry under its owner's name, measured on the Pod range alone.

    A failed or partial read of the first pass is named in the `limitations`
    of the reading project's own subnet entries. A project whose reads failed
    but which owns no evaluated subnet -- a Shared VPC service project -- gets
    a `<project>/UNREAD_SUBNET_USAGE` skipped row instead, so the run still
    reads as partial: what it draws from a host subnet cannot be known
    without the read, but one unreadable project does not mark every subnet
    in the fleet.
    """
    limitations = usage["limitations"]
    pods_by_subnet: dict[tuple[str, str, str], dict[str, dict]] = {}
    for (owner, region, subnet_name, range_name), pod in usage["pod_ranges"].items():
        pods_by_subnet.setdefault((owner, region, subnet_name), {})[range_name] = pod

    def clusters_command(pod):
        return command_text(subnet_commands(pod["project"])["clusters"])

    def pod_findings(target, subnet_name, region, key, cidrs):
        return [
            pod_range_finding(target, subnet_name, region, range_name, cidrs.get(range_name, ""), pod,
                              clusters_command(pod))
            for range_name, pod in sorted(pods_by_subnet.get(key, {}).items())
            if pod["utilization"] is not None and 1 - pod["utilization"] < AVAILABLE_FRACTION_FLOOR
        ]

    def unreadable_notes(key):
        return [
            f"Pod range {range_name}: GKE reported utilization {', '.join(pod['unreadable'])}, not a "
            "fraction in 0-1, so that report was not measured"
            for range_name, pod in sorted(pods_by_subnet.get(key, {}).items()) if pod["unreadable"]
        ]

    findings = []
    # Under `--check all` the PSC sweep has already added a `project/<id>`
    # entry for most projects; only this sweep's own entries say whether a
    # project's failed reads have a subnet entry to be named on.
    first_entry = len(active_targets)
    evaluated: set[tuple[str, str, str]] = set()
    # Why a project in scope named none of its subnets, for the entries of Pod
    # ranges on them.
    unlisted: dict[str, str] = {}
    for project_id in projects:
        cmds = subnet_commands(project_id)
        subnets, error = run_gcloud_json(cmds["subnets"])
        if subnets == API_DISABLED:
            unlisted[project_id] = f"the Compute Engine API is disabled in {project_id}"
            continue
        if error is not None or not isinstance(subnets, list):
            target = f"{project_id}/{UNENUMERATED_SUBNETS}"
            skipped_targets.append({
                "cluster": target,
                "name": target,
                "location": GLOBAL_LOCATION,
                "project": project_id,
                "reason": error or f"`{shlex.join(cmds['subnets'])}` did not return a list",
            })
            unlisted[project_id] = f"the subnet listing of {project_id} failed (see {target})"
            continue

        # The same reads run in every project in scope; the entry names its
        # own project's, which keeps it under audit_report's MAX_COMMAND_CHARS
        # however many service projects draw on the subnet.
        scope_command = command_text(*cmds.values())
        uncounted = []
        for subnet in subnets:
            if not isinstance(subnet, dict):
                continue
            name = subnet.get("name", "")
            purpose = subnet.get("purpose") or ""
            key = subnet_key(subnet.get("selfLink", ""), project_id) or (
                project_id, str(subnet.get("region", "")).rsplit("/", 1)[-1], name)
            cidr = subnet.get("ipCidrRange", "")
            if purpose not in COUNTABLE_SUBNET_PURPOSES or not cidr:
                uncounted.append(f"{name} ({purpose or 'no IPv4 range'})")
                continue
            try:
                network = ipaddress.ip_network(cidr, strict=False)
            except ValueError:
                uncounted.append(f"{name} (unparsable range {cidr!r})")
                continue
            _, region, name = key
            target = f"{project_id}/{region}/{name}"
            evaluated.add(key)
            active_targets.append(subnet_entry(target, region, project_id, scope_command,
                                               [*limitations.get(project_id, []), *unreadable_notes(key)]))

            capacity = network.num_addresses
            used = (addresses_in_range(usage["used_ips"].get(key, set()), network)
                    + GCP_RESERVED_PRIMARY_ADDRESSES)
            available = max(capacity - used, 0) / capacity
            if available < AVAILABLE_FRACTION_FLOOR:
                # The subnet's own listing, then the primary reads of
                # every project that named an address in it.
                sources = [project_id, *sorted(usage["readers"].get(key, set()) - {project_id})]
                primary_command = command_text(cmds["subnets"], *(
                    subnet_commands(p)[role] for p in sources
                    for role in ("instances", "addresses", "forwarding_rules")))
                findings.append(primary_finding(target, name, region, cidr, used, capacity, available,
                                                primary_command))

            cidrs = {
                secondary.get("rangeName", ""): secondary.get("ipCidrRange", "")
                for secondary in subnet.get("secondaryIpRanges") or [] if isinstance(secondary, dict)
            }
            findings.extend(pod_findings(target, name, region, key, cidrs))
        if uncounted:
            sys.stderr.write(
                f"{project_id}: subnet-ip-exhaustion skipped subnets no read can count: {', '.join(uncounted)}\n"
            )

    for key in sorted(set(pods_by_subnet) - evaluated):
        owner, region, name = key
        why = unlisted.get(owner) or (
            f"{owner} is outside this run's scope" if owner not in projects
            else f"the subnet listing of {owner} did not name it among the subnets this sweep counts"
        )
        target = f"{owner}/{region}/{name}"
        # One command keeps the entry under MAX_COMMAND_CHARS: the read behind
        # its fullest range. Each finding's evidence names its own read.
        fullest = max(pods_by_subnet[key].values(),
                      key=lambda pod: -1 if pod["utilization"] is None else pod["utilization"])
        note = (
            f"subnet {name} was not listed because {why}, so only the Pod ranges GKE reports on it were "
            "measured, not its primary range"
        )
        active_targets.append(subnet_entry(target, region, owner, clusters_command(fullest),
                                           [note, *limitations.get(owner, []), *unreadable_notes(key)]))
        findings.extend(pod_findings(target, name, region, key, {}))

    entry_projects = {entry["project"] for entry in active_targets[first_entry:]}
    unlisted_rows = {row["project"]: row for row in skipped_targets
                     if row["cluster"].endswith(f"/{UNENUMERATED_SUBNETS}")}
    for project_id in projects:
        if not limitations.get(project_id) or project_id in entry_projects:
            continue
        if project_id in unlisted_rows:
            # Already skipped for its subnet listing; its other failed reads join that row.
            row = unlisted_rows[project_id]
            row["reason"] = LIMITATION_JOINER.join([row["reason"], *limitations[project_id]])
        else:
            target = f"{project_id}/{UNREAD_SUBNET_USAGE}"
            skipped_targets.append({
                "cluster": target,
                "name": target,
                "location": GLOBAL_LOCATION,
                "project": project_id,
                "reason": LIMITATION_JOINER.join(limitations[project_id]),
            })
    return findings


def primary_finding(target: str, subnet: str, region: str, cidr: str, used: int, capacity: int,
                    available: float, command: str) -> dict:
    """A subnet-ip-exhaustion finding on a subnet's primary range."""
    pct = percent_available(available)
    return {
        "check": SUBNET_CHECK_SLUG,
        "severity": SUBNET_SEVERITY,
        "title": f"Subnet {subnet} in {region} has {pct}% of its primary range available",
        "cluster": target,
        "namespace": "",
        "object": f"Subnet/{subnet}",
        "impact": (
            f"New VMs, GKE nodes and internal load balancers in subnet {subnet} cannot get an "
            "address once its primary range is full."
        ),
        "evidence": {
            "command": command,
            "excerpt": (
                f"primary range {cidr}: at least {used} of {capacity} addresses in use ({pct}% available); "
                "counted from VM NICs, internal addresses and forwarding rules, so serverless connectors "
                "and Google-managed endpoints are not included"
            ),
        },
        "recommendation": {
            "action": f"Expand the primary CIDR of subnet {subnet} in its Terraform VPC definition.",
            "rationale": (
                f"Less than {percent_available(AVAILABLE_FRACTION_FLOOR)}% of the primary range is free, "
                "and the count is a lower bound."
            ),
            "risk": (
                "A primary range can only grow, never shrink, and the larger range must not overlap "
                "another subnet or a peered network."
            ),
        },
        "remediation": {"kind": "manual"},
    }


def pod_range_finding(target: str, subnet: str, region: str, range_name: str, cidr: str,
                      pod: dict, command: str) -> dict:
    """A subnet-ip-exhaustion finding on a GKE Pod secondary range."""
    utilization = pod["utilization"]
    pct = percent_available(1 - utilization)
    owner = f"cluster {pod['cluster']}" + (f", node pool {pod['pool']}" if pod["pool"] else "")
    # No CIDR when the subnet listing that carries it was not read.
    excerpt = (f"Pod range {range_name}" + (f" ({cidr})" if cidr else "")
               + f": GKE reports {utilization * 100:.1f}% allocated ({owner})")
    blocks = node_blocks_that_fit(cidr, utilization, pod["prefix"])
    if blocks is not None:
        excerpt += f"; about {blocks} more /{pod['prefix']} node blocks fit"
    return {
        "check": SUBNET_CHECK_SLUG,
        "severity": SUBNET_SEVERITY,
        "title": f"Pod range {range_name} of subnet {subnet} in {region} has {pct}% available",
        "cluster": target,
        "namespace": "",
        "object": f"SecondaryRange/{range_name}",
        "impact": (
            f"GKE cannot add nodes that take their Pod block from {range_name} once it is fully allocated, "
            "so autoscaling and surge upgrades on those node pools fail."
        ),
        "evidence": {"command": command, "excerpt": excerpt},
        "recommendation": {
            "action": (
                f"Add an additional Pod range to cluster {pod['cluster']} (additionalPodRangesConfig), "
                "or lower maxPodsPerNode on new node pools so each node takes a smaller block."
            ),
            "rationale": "GKE allocates one fixed block of the Pod range per node, whatever the node runs.",
            "risk": (
                "An additional range needs unused VPC space that overlaps no other range; maxPodsPerNode "
                "applies only to new node pools, so existing pools must be recreated to benefit."
            ),
        },
        "remediation": {"kind": "manual"},
    }


def node_blocks_that_fit(cidr: str, utilization: float, prefix: int | None) -> int | None:
    """How many more per-node /prefix blocks the unallocated part of a Pod range holds, or None."""
    if prefix is None:
        return None
    try:
        capacity = ipaddress.ip_network(cidr, strict=False).num_addresses
    except ValueError:
        return None
    available = round(capacity * (1 - utilization))
    return max(available // (1 << (IPV4_BITS - prefix)), 0)


def main():
    parser = argparse.ArgumentParser(description="Audit GCP VPC Networking Fabric")
    parser.add_argument("--project-id", help="Optional GCP Project ID")
    parser.add_argument("--output", help="Optional path to write findings JSON")
    parser.add_argument("--check", choices=CHECK_CHOICES, default=CHECK_ALL,
                        help="Run one sweep, or both (default)")
    args = parser.parse_args()
    # A run killed before it writes must not leave the last run's document to
    # be merged as this one's.
    if args.output:
        pathlib.Path(args.output).unlink(missing_ok=True)

    listing_errors: list[str] = []
    target_projects = get_target_projects(args.project_id, listing_errors)
    all_findings = []
    skipped_targets = []
    active_targets = []

    for error in listing_errors:
        sys.stderr.write(f"{error}; auditing {target_projects or 'no project'}\n")
        skipped_targets.append({
            "cluster": f"{PROJECT_TARGET_PREFIX}{UNENUMERATED_PROJECTS}",
            "name": f"{PROJECT_TARGET_PREFIX}{UNENUMERATED_PROJECTS}",
            "location": GLOBAL_LOCATION,
            "project": UNENUMERATED_PROJECTS,
            "reason": f"{error}. How many other projects the fleet holds is unknown.",
        })

    if not target_projects:
        sys.stderr.write("No target projects resolved from CLI, environment, or gcloud.\n")
        skipped_targets.append({
            "cluster": f"{PROJECT_TARGET_PREFIX}{UNKNOWN_PROJECT}",
            "name": f"{PROJECT_TARGET_PREFIX}{UNKNOWN_PROJECT}",
            "location": GLOBAL_LOCATION,
            "project": UNKNOWN_PROJECT,
            "reason": "No GCP project ID configured or resolved"
        })

    if args.check in (CHECK_ALL, PSC_CHECK_SLUG):
        for proj in target_projects:
            all_findings.extend(audit_project_networking(proj, skipped_targets, active_targets))
    if args.check in (CHECK_ALL, SUBNET_CHECK_SLUG):
        # Two passes, because a Shared VPC subnet's users live in other projects.
        usage = read_subnet_usage(target_projects)
        all_findings.extend(audit_subnet_capacity(target_projects, usage, skipped_targets, active_targets))

    findings_document = {
        "audit": AUDIT_SLUG,
        "scope": {
            "clusters": active_targets,
            "skipped": skipped_targets
        },
        "findings": all_findings
    }

    if args.output:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(findings_document, f, indent=JSON_INDENT)
        except Exception as e:
            sys.stderr.write(f"Failed to write output to {args.output}: {e}\n")
            sys.exit(1)

    written = f"; wrote {args.output}" if args.output else "; no --output, nothing written"
    print(f"Found {len(all_findings)} networking findings across {len(active_targets)} audited targets. "
          f"{len(skipped_targets)} targets skipped{written}.")

if __name__ == "__main__":
    main()
