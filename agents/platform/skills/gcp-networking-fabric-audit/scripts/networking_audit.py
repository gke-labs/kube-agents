#!/usr/bin/env python3
"""
networking_audit.py — collector for the GCP networking fabric and VPC IPAM audit.

Sweeps every project in scope for all six checks of
governance/gcp_networking_fabric_sop.md and writes the run manifest
docs/designs/fleet-audit-collector-manifest.md §2 specifies, so
`audit_report.py finish --manifest-file` cross-checks the worker's
`checks_run` against what ran and adopts the evidence computed here.

Two target shapes, matching `AuditSpec.scopes` for this stream:

- `<project>/<region>/<subnet>` owes `subnet-ip-exhaustion` alone. The sweep
  behind it reads GKE's own Pod-range utilization from `container clusters
  list` and counts each primary range as a lower bound from VM NICs, internal
  addresses and forwarding rules, across every project in scope so a Shared VPC
  host subnet counts its service projects' use (`read_subnet_usage`,
  `audit_subnet_capacity`). No Network Analyzer insight and no
  `subnets list-usable`: both need grants the agent does not hold.
- `project/<project>` owes the other five: `cloud-nat-exhaustion`
  (`routers list`, then `get-status` and a per-gateway `get-nat-mapping-info`
  for each dynamic-allocation gateway), `psc-routing-deadlock`
  (`forwarding-rules list`, unfiltered), `mtu-packet-fragmentation`
  (`networks list`, comparing ACTIVE peerings), `cloud-armor-false-positive`
  (`security-policies list` against `backend-services list`) and
  `firewall-world-open-ingress` (`firewall-rules list` against `instances
  list`). A failed read costs only the checks that use it. Each such check
  goes in the target's `checks_unevaluated`, and the other checks keep their
  verdicts. The target is `gate-failed` only when no read passes.

`--check` narrows a run to the subnet sweep or to the project-level checks;
the default runs both. With `--output` the manifest is written there and a
one-line summary printed; without it the manifest goes to stdout.
"""

import argparse
import hashlib
import ipaddress
import json
import math
import os
import pathlib
import re
import shlex
import subprocess
import sys
import time

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
# Each project target's firewall and instance reads, kept for the run so
# `collect_fleet` can measure a Shared VPC host's rules against its service
# projects' instances: `{project: (firewalls, instances, command)}`.
FIREWALL_READS: dict[str, dict] = {}
PROJECT_NUMBER_FORMAT = "--format=value(projectNumber)"
PROJECT_FLAG = "--project"
JSON_INDENT = 2
# The project-level reads take the whole object: each check reads several fields.
JSON_FORMAT = "--format=json"
# Long enough for an aggregated `instances list` over every zone of a large
# project, which pages 500 VMs at a time; a read past it is a failed read and
# is named in `limitations`.
GCLOUD_TIMEOUT_SECONDS = 300
CHECK_ALL = "all"
SUBNET_CHECK_SLUG = "subnet-ip-exhaustion"
SUBNET_SEVERITY = "critical"
# `psc-routing-deadlock` runs every project-level check (`PROJECT_CHECKS_CHOICE`).
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
CLUSTERS_FORMAT = "--format=json(name,location,subnetwork,networkConfig,ipAllocationPolicy,nodePools,autopilot)"
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
MANIFEST_VERSION = 1
# A digest of this file, published as `checks_revision`. The manifest contract
# carries it unread today, reserved for the run-over-run comparison that tells
# a finding that stopped reproducing from a check that stopped looking.
REVISION_DIGEST_CHARS = 12
CHECKS_REVISION = hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest()[:REVISION_DIGEST_CHARS]
OUTCOME_COLLECTED = "collected"
OUTCOME_GATE_FAILED = "gate-failed"
TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
# `audit_report.validate_check_command`'s ceiling, restated because this script
# ships standalone. An over-length `command` is refused, not clipped, so a
# project with enough Cloud Routers to overflow the joined NAT reads would
# publish nothing.
MAX_COMMAND_CHARS = 2000
# Room left at the end of a clipped join for the count of the reads cut.
JOIN_TAIL_BUDGET_CHARS = 64
# What the `project/UNENUMERATED_PROJECTS` row says after its notes. The notes
# are clipped, not this: it is what says the fleet's size is unknown.
UNENUMERATED_TAIL = "How many other projects the fleet holds is unknown."
NOTE_SEPARATOR = "; "
SENTENCE_END = ". "

NAT_CHECK_SLUG = "cloud-nat-exhaustion"
MTU_CHECK_SLUG = "mtu-packet-fragmentation"
ARMOR_CHECK_SLUG = "cloud-armor-false-positive"
FIREWALL_CHECK_SLUG = "firewall-world-open-ingress"
# The project target's limitation when the fleet pass measured its rules
# without the instances of other projects whose `instances list` failed.
FLEET_INSTANCES_UNREAD_LIMITATION = (
    "firewall-world-open-ingress measured these rules without the instances of "
    "{count} other project(s), whose firewall or instance reads failed: {names}"
)
# The project target's limitation when its own `forwarding-rules list` failed
# and the firewall and instance reads passed: the check ran without the
# load-balancer path.
FORWARDING_UNREAD_LIMITATION = (
    "firewall-world-open-ingress did not measure the load-balancer path on this "
    "project, because the `forwarding-rules list` read failed"
)
# The project target's limitation when the fleet pass measured its rules
# without the forwarding rules of other projects.
FLEET_FORWARDING_UNREAD_LIMITATION = (
    "firewall-world-open-ingress measured these rules without the forwarding rules "
    "of {count} other project(s), whose `forwarding-rules list` read failed: {names}"
)
# The project target's limitation for a rule that opens a management port to
# the internet and reaches no instance the audit can see, where an instance
# the audit cannot see can get the rule: a target-scoped rule whose target no
# visible instance carries, or a rule without a target on a network that holds
# a GKE Autopilot cluster.
UNDECIDED_FIREWALL_LIMITATION = (
    "firewall-world-open-ingress could not decide {count} rule(s) that open a "
    "management port to the internet and reach no instance visible to the audit "
    "identity, which does not see GKE Autopilot nodes; confirm by hand that no "
    "instance gets the rule: {names}"
)
# The load-balancing scheme of an external passthrough Network Load Balancer.
# It keeps the load balancer IP as the destination of each packet, so the VPC
# firewall applies to the backends with that IP as the destination.
EXTERNAL_PASSTHROUGH_SCHEME = "EXTERNAL"
# The forwarding rule protocols that carry TCP to the backends.
PASSTHROUGH_TCP_PROTOCOLS = ("TCP", "L3_DEFAULT")
# The `target` of a passthrough forwarding rule names one of these. A proxy
# target (`targetTcpProxies`, `targetSslProxies`, an HTTP proxy) ends the
# connection at Google, so the backends never get the load balancer IP.
PASSTHROUGH_TARGET_MARKERS = ("/targetPools/", "/targetInstances/")
# The excerpt clause for an instance named through a load balancer.
LOAD_BALANCER_PATH_NOTE = (
    "an instance named through a load balancer is in the rule's target; the "
    "collector does not read the load balancer's backends"
)
# The instance fields that the firewall check reads. The projection keeps each
# stored instance small for the fleet pass.
FIREWALL_INSTANCES_FORMAT = "--format=json(name,selfLink,zone,status,networkInterfaces,tags,serviceAccounts)"
# The status an instance has when the read gives none.
INSTANCE_RUNNING = "RUNNING"
# The instance states that can accept a connection now or soon. An instance in
# any other state, such as TERMINATED, accepts no connection.
LIVE_INSTANCE_STATUSES = (INSTANCE_RUNNING, "STAGING", "PROVISIONING", "REPAIRING")
# The key that records the project of each stored instance. Only this module
# reads it.
ORIGIN_PROJECT_KEY = "_origin_project"
# The excerpt clause that names what the firewall check does not read.
FIREWALL_POLICY_BLIND_SPOT = "network firewall rules only; firewall policies were not read"
# The project target's limitation for one check whose read failed.
UNREAD_CHECK_LIMITATION = "{check} could not be evaluated on this project: {error}"
# The `--check` value that runs the project-level checks. `psc-routing-deadlock`
# was this flag's value when PSC was the only project-level check here, and the
# value is kept so a caller that passes it still runs PSC, now beside the rest.
PROJECT_CHECKS_CHOICE = PSC_CHECK_SLUG

# Cloud NAT's documented per-VM dynamic port ceiling, applied when `routers
# list` omits the field, which it does for any NAT that never overrode it.
DEFAULT_MAX_PORTS_PER_VM = 65536
# SOP 2.2: a VM drawing this share of its gateway's port ceiling is flagged.
NAT_PORT_RATIO_FLOOR = 0.8
NAT_AUTO_ONLY = "AUTO_ONLY"
# A VPC's MTU when nobody set one; `networks list` omits `mtu` for every
# network still on it, and absence is a value to compare, not a gap.
DEFAULT_NETWORK_MTU = 1460
PEERING_ACTIVE = "ACTIVE"
# Where a network reference starts carrying meaning: everything before it in a
# selfLink is API-version boilerplate two equal networks can disagree about.
NETWORK_URL_PROJECT_MARKER = "projects/"
# GCP's implicit default Cloud Armor rule, which every policy carries.
CLOUD_ARMOR_DEFAULT_RULE_PRIORITY = 2147483647
# A backend service whose name carries one of these is not production, and
# SOP 2.5's Do-NOT-flag limb excludes it.
NON_PRODUCTION_TOKENS = ("test", "staging", "stage", "dev", "sandbox", "qa")
# The separators that split a backend name into tokens.
NAME_TOKEN_SEPARATORS = re.compile(r"[^a-z0-9]+")
DIGITS = "0123456789"
# The two source ranges that mean "every host on the internet", one per
# address family: a rule or a deny covers a family only through its own range.
WORLD_IPV4_RANGE = "0.0.0.0/0"
WORLD_IPV6_RANGE = "::/0"
WORLD_SOURCE_RANGES = (WORLD_IPV4_RANGE, WORLD_IPV6_RANGE)
# The `version` that `ipaddress` gives an IPv4 address.
IPV4_VERSION = 4
# `IPProtocol` values that carry TCP: `all` is every protocol, and the API
# accepts the IANA number in place of the name.
TCP_PROTOCOL_TOKENS = ("tcp", "6", "all")
INGRESS = "INGRESS"
# GCP's `priority` when a firewall rule does not set one.
DEFAULT_FIREWALL_PRIORITY = 1000
# Ports whose service admits a caller on a credential, so opening one to the
# internet makes that credential the whole perimeter. Deliberately not 80 or
# 443: GKE opens those for every LoadBalancer Service on purpose.
MANAGEMENT_PORTS = {
    22: "SSH",
    1433: "MSSQL",
    2379: "etcd",
    2380: "etcd-peer",
    3306: "MySQL",
    3389: "RDP",
    5432: "PostgreSQL",
    6379: "Redis",
    9200: "Elasticsearch",
    10250: "kubelet",
    27017: "MongoDB",
}
# How many reachable instances an excerpt names before it summarises the rest.
MAX_NAMED_EXPOSED_INSTANCES = 3
PERCENT = 100

SEVERITY = {
    NAT_CHECK_SLUG: "critical",
    PSC_CHECK_SLUG: "major",
    MTU_CHECK_SLUG: "major",
    ARMOR_CHECK_SLUG: "minor",
    FIREWALL_CHECK_SLUG: "critical",
}
IMPACT = {
    NAT_CHECK_SLUG: (
        "VMs that exhaust their NAT port allocation see new outbound connections silently fail, "
        "which for a GKE node means pods lose egress with no error at the workload layer."
    ),
    PSC_CHECK_SLUG: (
        "Traffic aimed at this Private Service Connect endpoint cannot reach its target service; "
        "consumers see connection failures with no signal at the VPC layer."
    ),
    MTU_CHECK_SLUG: (
        "Packets crossing this peering at the larger MTU get fragmented or dropped, which shows up "
        "as intermittent, hard-to-diagnose latency and retransmits rather than a clean failure."
    ),
    ARMOR_CHECK_SLUG: (
        "A preview-mode rule on a production backend logs matches without enforcing them, so the "
        "WAF looks like it is protecting traffic it is only observing; conflicting priorities make "
        "the effective policy unpredictable."
    ),
    FIREWALL_CHECK_SLUG: (
        "Any host on the internet can open a TCP connection to these ports on the instances named, "
        "so whatever authenticates on the far side of the port is the entire perimeter."
    ),
}


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
    parsed, error, _stdout, _seconds = read_gcloud_json(cmd, warnings)
    return parsed, error


def read_gcloud_json(
    cmd: list[str], warnings: list[str] | None = None
) -> tuple[list[dict] | dict | str | None, str | None, str, float]:
    """`run_gcloud_json`'s answer plus the raw stdout and the seconds the read
    took, which the manifest's `commands` record digests and sums."""
    started = time.monotonic()
    rc, stdout, stderr = run_cmd(cmd)
    seconds = time.monotonic() - started
    if rc != 0:
        project = project_flag_value(cmd)
        if project and any(marker in (stderr or "") for marker in API_DISABLED_MARKERS):
            ours, why_not = refusal_names_project(project, stderr)
            if ours:
                return API_DISABLED, None, "", seconds
            stderr = f"{stderr.strip()} ({why_not})"
        sys.stderr.write(f"gcloud command failed ({rc}): {' '.join(cmd)}\n{stderr}\n")
        return None, f"{' '.join(cmd)} failed ({rc}): {stderr.strip()}", "", seconds
    if warnings is not None and (stderr or "").strip():
        warnings.append(stderr.strip())
    if not stdout.strip():
        return [], None, stdout, seconds
    try:
        return json.loads(stdout), None, stdout, seconds
    except Exception as e:
        sys.stderr.write(f"Error parsing gcloud output from {' '.join(cmd)}: {e}\n")
        return None, f"{' '.join(cmd)} returned unparsable JSON: {e}", "", seconds


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


def subnet_commands(project_id: str) -> dict[str, list[str]]:
    """The reads the subnet-capacity sweep issues against one project, by role."""
    project = f"{PROJECT_FLAG}={project_id}"
    return {
        "subnets": [GCLOUD, "compute", "networks", "subnets", "list", project, SUBNETS_FORMAT],
        "clusters": [GCLOUD, "container", "clusters", "list", project, CLUSTERS_FORMAT],
        # The same read as the project target's firewall check, so one run
        # lists each project's instances once (`collect_fleet` shares it).
        "instances": [GCLOUD, "compute", "instances", "list", project, FIREWALL_INSTANCES_FORMAT],
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


def read_subnet_usage(projects: list[str], instance_cache: dict | None = None) -> dict:
    """First subnet-ip-exhaustion pass: what every project in scope draws from any subnet.

    Clusters, VMs, internal addresses and forwarding rules are read in every
    project and keyed by the subnet's owning project, so a Shared VPC host
    subnet counts the nodes, Pods and load balancers of its service projects.
    Returns the merged Pod-range reports, the unique primary-range addresses
    per subnet, the projects whose reads named an address in each subnet, and
    the limitations of each project's failed or partial reads, keyed by project,
    and the projects whose clusters read failed or was partial. A caller that
    passes `instance_cache` gets each project's `instances list` result in it,
    so the project checks do not list the instances again.
    """
    pod_ranges: dict[tuple[str, str, str, str], dict] = {}
    used_ips: dict[tuple[str, str, str], set[str]] = {}
    readers: dict[tuple[str, str, str], set[str]] = {}
    limitations: dict[str, list[str]] = {}
    autopilot_networks: dict[str, set[str]] = {}
    clusters_unread: set[str] = set()
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
            clusters_unread.add(project_id)
        partial = next((w for w in warnings if any(m in w for m in INCOMPLETE_LISTING_MARKERS)), None)
        if partial:
            clusters_unread.add(project_id)
            own.append(
                f"Pod ranges partially read: `{shlex.join(cmds['clusters'])}` warned: "
                f"{partial[:ERROR_EXCERPT_CHARS]}"
            )
        pod_range_utilization(clusters, project_id, pod_ranges)
        for cluster in clusters:
            if isinstance(cluster, dict) and (cluster.get("autopilot") or {}).get("enabled"):
                network = (cluster.get("networkConfig") or {}).get("network") or cluster.get("network", "")
                autopilot_networks.setdefault(_network_key(network, project_id), set()).add(
                    f"{project_id}/{cluster.get('location', '')}/{cluster.get('name', '')}"
                )

        primary_reads = {}
        for role, read in (("instances", "VM NICs"), ("addresses", "internal addresses"),
                           ("forwarding_rules", "forwarding rules")):
            items, error = run_gcloud_json(cmds[role])
            if role == "instances" and instance_cache is not None:
                instance_cache[project_id] = (items, error)
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
    return {
        "pod_ranges": pod_ranges,
        "used_ips": used_ips,
        "readers": readers,
        "limitations": limitations,
        # Network key to the Autopilot clusters on it, from the clusters reads
        # that passed. The firewall check marks an untargeted rule on such a
        # network undecided, because the audit cannot see Autopilot nodes.
        "autopilot_networks": autopilot_networks,
        # The projects whose clusters read failed or was partial. Their
        # Autopilot clusters are not known.
        "clusters_unread": sorted(clusters_unread),
    }


def subnet_entry(target: str, region: str, project_id: str, command: str, limitations: list[str]) -> dict:
    """A `<project>/<region>/<subnet>` scope entry.

    It declares nothing inapplicable: `AuditSpec.scopes` already says a subnet
    target owes `subnet-ip-exhaustion` alone, so a row per subnet per
    project-level check would restate the target shape on every run.
    """
    entry = {
        "name": target,
        "location": region,
        "project": project_id,
        "checks_run": [{"check": SUBNET_CHECK_SLUG, "command": command}],
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
    # Only this sweep's own entries say whether a project's failed reads have a
    # subnet entry to be named on.
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


# --------------------------------------------------------------------------- #
# The project-level checks: pure functions over already-read `gcloud` JSON.
# --------------------------------------------------------------------------- #


def _last_segment(url: str) -> str:
    return (url or "").rstrip("/").split("/")[-1]


def _nat_status_entry(status: dict | None, nat_name: str) -> dict | None:
    for entry in ((status or {}).get("result") or {}).get("natStatus") or []:
        if isinstance(entry, dict) and entry.get("name") == nat_name:
            return entry
    return None


def check_router_nat(router: dict, status: dict | None, mappings: dict | None) -> dict | None:
    """SOP 2.2 against one router: `status` is its `get-status` response,
    `mappings` maps each dynamic gateway's name to its own
    `get-nat-mapping-info --nat-name` response. One hit per router, naming
    every gateway on it that is AUTO_ONLY with no auto-allocated IP or has a VM
    at `NAT_PORT_RATIO_FLOOR` of its port ceiling.

    Keyed by gateway because an unfiltered mapping read returns every VM behind
    any gateway on the router, and measuring those against each gateway's own
    ceiling in turn attributes one gateway's VMs to another's limit. Only a
    dynamic-allocation gateway's ports are measured: with dynamic allocation
    off, each VM holds exactly `minPortsPerVm`, so the ratio is the constant 1.0
    and every VM behind every stock gateway would read as exhausted.

    The object names the region too: a router name is unique per region, so the
    bare name collides across regions inside one project.
    """
    router_name = router.get("name", "")
    region = _last_segment(router.get("region", ""))
    problems = []
    for nat in router.get("nats") or []:
        nat_name = nat.get("name", "")
        if nat.get("natIpAllocateOption") == NAT_AUTO_ONLY:
            entry = _nat_status_entry(status, nat_name)
            if entry is not None and not entry.get("autoAllocatedNatIps"):
                problems.append(f"{nat_name}: {NAT_AUTO_ONLY} with no auto-allocated external IP")
                continue
        if not nat.get("enableDynamicPortAllocation"):
            continue
        ceiling = nat.get("maxPortsPerVm") or DEFAULT_MAX_PORTS_PER_VM
        mapping = (mappings or {}).get(nat_name) or []
        for vm in mapping if isinstance(mapping, list) else []:
            for iface in vm.get("interfaceNatMappings") or []:
                total = iface.get("numTotalNatPorts")
                if isinstance(total, (int, float)) and total / ceiling >= NAT_PORT_RATIO_FLOOR:
                    problems.append(
                        f"{nat_name}: {vm.get('instanceName', '?')} using {total}/{ceiling} "
                        f"ports ({total / ceiling * PERCENT:.0f}%)"
                    )
    if not problems:
        return None
    return {"object": f"Router/{region or GLOBAL_LOCATION}/{router_name}", "excerpt": NOTE_SEPARATOR.join(problems)}


def check_psc_routing(forwarding_rules: list) -> list[dict]:
    """SOP 2.3: one hit per forwarding rule whose target is a service attachment
    and whose `pscConnectionStatus` is REJECTED or CLOSED. Selected here rather
    than by `--filter`, so a change in gcloud's filter semantics cannot turn the
    check silent. Scoped by region in the object, because a forwarding rule's
    name is unique per region and the bare name collides across regions."""
    hits = []
    for rule in forwarding_rules or []:
        if not isinstance(rule, dict):
            continue
        name = rule.get("name", "")
        scope = _last_segment(rule.get("region", "")) or GLOBAL_LOCATION
        target = rule.get("target", "")
        status = rule.get("pscConnectionStatus", "")
        if target and SERVICE_ATTACHMENT_SUBSTR in target and status in PSC_REJECTED_STATUSES:
            hits.append({"object": f"ForwardingRule/{scope}/{name}", "excerpt": f"pscConnectionStatus: {status}"})
    return hits


def _network_key(url_or_name: str, project: str) -> str:
    """A network reference as `projects/<project>/global/networks/<name>`.

    The project stays in the key because a peering can name another project's
    `default`, and matching on the last segment would compare the wrong two
    MTUs. A reference with no `projects/` segment is relative to `project`.
    """
    text = (url_or_name or "").rstrip("/")
    index = text.find(NETWORK_URL_PROJECT_MARKER)
    if index != -1:
        return text[index:].lower()
    return f"{NETWORK_URL_PROJECT_MARKER}{project}/global/networks/{_last_segment(text)}".lower()


def _network_mtu(network: dict | None) -> int | None:
    """A listed network's MTU, an absent key read as GCP's default; None only
    for a network the listing does not hold."""
    if network is None:
        return None
    mtu = network.get("mtu")
    return DEFAULT_NETWORK_MTU if mtu is None else mtu


def check_mtu_mismatch(networks: list, project: str) -> list[dict]:
    """SOP 2.4: one hit per unordered pair of ACTIVE-peered networks whose MTUs
    differ. A peer outside this project's listing is unread, not defaulted."""
    by_key = {
        _network_key(n.get("selfLink") or n.get("name", ""), project): n
        for n in networks or []
        if isinstance(n, dict)
    }
    seen_pairs = set()
    hits = []
    for net in networks or []:
        if not isinstance(net, dict):
            continue
        name, mtu = net.get("name"), _network_mtu(net)
        for peering in net.get("peerings") or []:
            if peering.get("state") != PEERING_ACTIVE:
                continue
            peer = by_key.get(_network_key(peering.get("network", ""), project))
            peer_mtu = _network_mtu(peer)
            if peer_mtu is None or mtu == peer_mtu:
                continue
            peer_name = peer.get("name", "")
            pair = tuple(sorted((name, peer_name)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            mtus = {name: mtu, peer_name: peer_mtu}
            hits.append({
                "object": f"NetworkPeering/{pair[0]}--{pair[1]}",
                "excerpt": f"{pair[0]} mtu={mtus[pair[0]]} peered with {pair[1]} mtu={mtus[pair[1]]}",
            })
    return hits


def _looks_non_production(name: str) -> bool:
    """Whether a name token, with its trailing digits removed, is a
    non-production token. A token match, not a substring match: `device` and
    `latest` are not `dev` and `test`."""
    tokens = NAME_TOKEN_SEPARATORS.split((name or "").lower())
    return any(token.rstrip(DIGITS) in NON_PRODUCTION_TOKENS for token in tokens if token)


def check_cloud_armor(policies: list, backend_services: list) -> list[dict]:
    """SOP 2.5: a policy attached to a production-looking backend that carries a
    `preview` rule (the implicit default rule aside) or two rules sharing one
    `priority`. The production gate governs both limbs: a policy on a `dev`
    backend, or on none, governs no production traffic."""
    attached_by_policy: dict[str, list[str]] = {}
    for svc in backend_services or []:
        if not isinstance(svc, dict):
            continue
        policy_ref = svc.get("securityPolicy") or ""
        if policy_ref:
            attached_by_policy.setdefault(_last_segment(policy_ref), []).append(svc.get("name", ""))

    hits = []
    for policy in policies or []:
        if not isinstance(policy, dict):
            continue
        name = policy.get("name", "")
        rules = policy.get("rules") or []
        production = [svc for svc in attached_by_policy.get(name, []) if not _looks_non_production(svc)]
        if not production:
            continue
        problems = []
        preview = [
            r.get("priority") for r in rules
            if r.get("preview") and r.get("priority") != CLOUD_ARMOR_DEFAULT_RULE_PRIORITY
        ]
        if preview:
            problems.append(
                f"attached to production backend(s) {', '.join(production)} with rule(s) in "
                f"preview: {', '.join(str(p) for p in preview)}"
            )
        priorities = [r.get("priority") for r in rules if r.get("priority") is not None]
        dupes = sorted({p for p in priorities if priorities.count(p) > 1})
        if dupes:
            problems.append(f"conflicting rule priorities: {dupes}")
        if problems:
            hits.append({"object": f"SecurityPolicy/{name}", "excerpt": NOTE_SEPARATOR.join(problems)})
    return hits


def _tcp_management_ports(block: list) -> set[int]:
    """Which `MANAGEMENT_PORTS` an `allowed[]` or `denied[]` block covers over
    TCP. An entry with no `ports` key covers every port for its protocol: that
    is how `default-allow-internal` and every `gke-<hash>-all` rule are written."""
    covered: set[int] = set()
    for entry in block or []:
        if str(entry.get("IPProtocol", "")).lower() not in TCP_PROTOCOL_TOKENS:
            continue
        specs = entry.get("ports")
        if not specs:
            covered.update(MANAGEMENT_PORTS)
            continue
        for spec in specs:
            low_text, _, high_text = str(spec).partition("-")
            try:
                low, high = int(low_text), int(high_text or low_text)
            except ValueError:
                continue
            covered.update(port for port in MANAGEMENT_PORTS if low <= port <= high)
    return covered


def _world_families(rule: dict) -> set[str]:
    """The world source ranges a rule names, one per address family."""
    return {r for r in rule.get("sourceRanges") or [] if r in WORLD_SOURCE_RANGES}


def _shadowed_ports(rule: dict, firewalls: list, project: str) -> set[int]:
    """Ports an unconditional DENY of higher or equal priority already blocks on
    this rule: same network, no target restriction, no destination ranges, and
    a world source range for every address family the rule opens to the world.
    GCP lets a deny win at equal priority. A DENY with a target or with
    destination ranges applies only to some instances, so
    `_denied_at` subtracts it for each address of each instance."""
    network = _network_key(rule.get("network", ""), project)
    priority = rule.get("priority", DEFAULT_FIREWALL_PRIORITY)
    families = _world_families(rule)
    blocked: set[int] = set()
    for other in firewalls or []:
        if not isinstance(other, dict):
            continue
        if other.get("disabled") or (other.get("direction") or INGRESS).upper() != INGRESS:
            continue
        if other.get("targetTags") or other.get("targetServiceAccounts") or other.get("destinationRanges"):
            continue
        if _network_key(other.get("network", ""), project) != network:
            continue
        if other.get("priority", DEFAULT_FIREWALL_PRIORITY) > priority:
            continue
        if not families or not families <= _world_families(other):
            continue
        blocked |= _tcp_management_ports(other.get("denied") or [])
    return blocked


def _external_addresses(inst: dict, network: str, project: str) -> list[tuple[str, object]]:
    """`(family, address)` for each external address of `inst` on the interface
    that is on `network`. `family` is the world source range of the address
    family. Internet traffic arrives only at an external address, so internal
    addresses and the addresses on other networks are not in this list."""
    found = []
    for nic in inst.get("networkInterfaces") or []:
        if _network_key(nic.get("network", ""), project) != network:
            continue
        values = [(WORLD_IPV4_RANGE, a.get("natIP")) for a in nic.get("accessConfigs") or []]
        values += [(WORLD_IPV6_RANGE, a.get("externalIpv6")) for a in nic.get("ipv6AccessConfigs") or []]
        for family, value in values:
            try:
                found.append((family, ipaddress.ip_address(str(value))))
            except ValueError:
                continue
    return found


def _in_ranges(address: object, rule: dict) -> bool:
    """Whether the `destinationRanges` of `rule`, if it has any, contain
    `address`. A rule without destination ranges applies to every address."""
    ranges = rule.get("destinationRanges") or []
    if not ranges:
        return True
    for value in ranges:
        try:
            net = ipaddress.ip_network(str(value), strict=False)
        except ValueError:
            continue
        if address.version == net.version and address in net:
            return True
    return False


def _denied_at(rule: dict, inst: dict, family: str, address: object, firewalls: list, project: str) -> set[int]:
    """Ports a DENY of higher or equal priority blocks for internet traffic to
    `address` of `inst` through the allow `rule`. The DENY must be on the same
    network, have a world source range of `family`, have a target that includes
    `inst`, and have destination ranges, if any, that contain `address`. GCP
    lets a deny win at equal priority."""
    network = _network_key(rule.get("network", ""), project)
    priority = rule.get("priority", DEFAULT_FIREWALL_PRIORITY)
    blocked: set[int] = set()
    for other in firewalls or []:
        if not isinstance(other, dict) or not other.get("denied"):
            continue
        if other.get("disabled") or (other.get("direction") or INGRESS).upper() != INGRESS:
            continue
        if _network_key(other.get("network", ""), project) != network:
            continue
        if other.get("priority", DEFAULT_FIREWALL_PRIORITY) > priority:
            continue
        if family not in _world_families(other):
            continue
        if not _in_target(other, inst, project) or not _in_ranges(address, other):
            continue
        blocked |= _tcp_management_ports(other.get("denied") or [])
    return blocked


def _in_target(rule: dict, inst: dict, project: str) -> bool:
    """Whether `inst` is on the rule's network and inside the rule's target
    scope. The status and the external address are not part of this test."""
    wanted = _network_key(rule.get("network", ""), project)
    if not any(_network_key(nic.get("network", ""), project) == wanted for nic in inst.get("networkInterfaces") or []):
        return False
    tags = {t.lower() for t in rule.get("targetTags") or []}
    if tags and not tags & {t.lower() for t in (inst.get("tags") or {}).get("items") or []}:
        return False
    accounts = {a.lower() for a in rule.get("targetServiceAccounts") or []}
    if accounts and not accounts & {(sa.get("email") or "").lower() for sa in inst.get("serviceAccounts") or []}:
        return False
    return True


def _forwarding_rule_ports(forwarding_rule: dict) -> set[int]:
    """The `MANAGEMENT_PORTS` a forwarding rule sends to its backends."""
    if forwarding_rule.get("allPorts"):
        return set(MANAGEMENT_PORTS)
    specs = list(forwarding_rule.get("ports") or [])
    if forwarding_rule.get("portRange"):
        specs.append(forwarding_rule["portRange"])
    covered: set[int] = set()
    for spec in specs:
        low_text, _, high_text = str(spec).partition("-")
        try:
            low, high = int(low_text), int(high_text or low_text)
        except ValueError:
            continue
        covered.update(port for port in MANAGEMENT_PORTS if low <= port <= high)
    return covered


def _single_addresses(rule: dict) -> set:
    """The addresses that the rule's `destinationRanges` name one by one (a
    /32 or a /128 range)."""
    found = set()
    for value in rule.get("destinationRanges") or []:
        try:
            net = ipaddress.ip_network(str(value), strict=False)
        except ValueError:
            continue
        if net.num_addresses == 1:
            found.add(net.network_address)
    return found


def _load_balancer_paths(rule: dict, forwarding_rules: list) -> list[tuple[str, str, object, set[int], str]]:
    """`(name, family, address, ports, origin project)` for each external
    passthrough load balancer that this allow names in its `destinationRanges`.

    A GKE LoadBalancer Service writes this shape: an allow from the internet on
    the node tag, with the load balancer IP as its only destination range. The
    backends can have no external IP, because the packets keep the load
    balancer IP as their destination. The allow must name the load balancer IP
    as a single address: a wider range names no particular load balancer. The
    forwarding rule must send to a backend service, a target pool or a target
    instance, not to a proxy."""
    singles = _single_addresses(rule)
    if not singles:
        return []
    families = _world_families(rule)
    paths = []
    for forwarding_rule in forwarding_rules or []:
        if not isinstance(forwarding_rule, dict):
            continue
        if str(forwarding_rule.get("loadBalancingScheme", "")).upper() != EXTERNAL_PASSTHROUGH_SCHEME:
            continue
        if str(forwarding_rule.get("IPProtocol", "")).upper() not in PASSTHROUGH_TCP_PROTOCOLS:
            continue
        target = str(forwarding_rule.get("target", ""))
        if not forwarding_rule.get("backendService") and not any(m in target for m in PASSTHROUGH_TARGET_MARKERS):
            continue
        try:
            address = ipaddress.ip_address(str(forwarding_rule.get("IPAddress", "")))
        except ValueError:
            continue
        family = WORLD_IPV4_RANGE if address.version == IPV4_VERSION else WORLD_IPV6_RANGE
        if family not in families or address not in singles:
            continue
        ports = _forwarding_rule_ports(forwarding_rule)
        if ports:
            origin = str(forwarding_rule.get(ORIGIN_PROJECT_KEY, ""))
            paths.append((str(forwarding_rule.get("name", "")), family, address, ports, origin))
    return paths


def _reachable_instances(
    rule: dict,
    instances: list,
    project: str,
    ports: list[int],
    firewalls: list | None = None,
    forwarding_rules: list | None = None,
) -> list[tuple[str, str, set[int], set[str]]]:
    """`(label, origin project, ports, load balancer projects)` for each
    instance that this rule admits internet traffic to on at least one of
    `ports`. The last item names the projects of the forwarding rules that
    made the instance reachable.

    The test is done for each external address of the instance on the rule's
    network. The address must be in a family that the rule opens to the world
    and inside the rule's destination ranges, if any. A DENY removes the ports
    it blocks for that address. The instance must be live and inside the rule's
    target. An external passthrough load balancer that the allow names in its
    destination ranges is a second path (`_load_balancer_paths`): through it,
    a target instance is reachable with or without an external IP. This is a
    floor, not a total: GKE Autopilot node VMs are not returned to
    `roles/compute.viewer` (see `gce_compute_fleet_sop.md` §1)."""
    network = _network_key(rule.get("network", ""), project)
    families = _world_families(rule)
    paths = _load_balancer_paths(rule, forwarding_rules or [])
    exposed = []
    for inst in instances or []:
        if not isinstance(inst, dict):
            continue
        if inst.get("status", INSTANCE_RUNNING) not in LIVE_INSTANCE_STATUSES:
            continue
        if not _in_target(rule, inst, project):
            continue
        open_ports: set[int] = set()
        first = None
        lb_origins: set[str] = set()
        for family, address in _external_addresses(inst, network, project):
            if family not in families or not _in_ranges(address, rule):
                continue
            reach = set(ports) - _denied_at(rule, inst, family, address, firewalls or [], project)
            if reach:
                open_ports |= reach
                first = first or address
        for lb_name, family, address, lb_ports, lb_origin in paths:
            reach = (set(ports) & lb_ports) - _denied_at(rule, inst, family, address, firewalls or [], project)
            if reach:
                open_ports |= reach
                first = first or f"through load balancer {lb_name} {address}"
                if lb_origin:
                    lb_origins.add(lb_origin)
        if open_ports:
            label = f"{inst.get('name', '')} ({first})"
            exposed.append((label, inst.get(ORIGIN_PROJECT_KEY, project), open_ports, lb_origins))
    return sorted(exposed, key=lambda item: (item[0], item[1]))


def _target_scope_phrase(rule: dict) -> str:
    if rule.get("targetTags"):
        return f"targetTags {sorted(rule['targetTags'])}"
    if rule.get("targetServiceAccounts"):
        return f"targetServiceAccounts {sorted(rule['targetServiceAccounts'])}"
    return "no target restriction, so every instance on the network"


def check_world_open_ingress(
    firewalls: list,
    instances: list,
    project: str,
    forwarding_rules: list | None = None,
    autopilot_networks: dict | None = None,
) -> list[dict]:
    """SOP 2.6: one hit per enabled INGRESS rule opening a management port to a
    world source range on at least one live instance, through its external IP
    or through an external passthrough load balancer. One per rule, not per
    port: the rule is what a reader goes and changes."""
    return world_open_ingress(firewalls, instances, project, forwarding_rules, autopilot_networks)[0]


def world_open_ingress(
    firewalls: list,
    instances: list,
    project: str,
    forwarding_rules: list | None = None,
    autopilot_networks: dict | None = None,
    clusters_unread: list | set | None = None,
) -> tuple[list[dict], list[str]]:
    """`(hits, undecided)`: the §2.6 hits, and the rules that open a management
    port to the world, reach no instance the audit can see, and can reach an
    instance it cannot see. The audit identity does not see GKE Autopilot
    nodes. Thus a target-scoped rule whose target no visible instance carries
    is undecided, and so is a rule without a target on a network that holds an
    Autopilot cluster (`autopilot_networks`: network key to cluster names).
    When the clusters read failed in the rule's project or in the network's
    project (`clusters_unread`), the Autopilot clusters on the network are not
    known, so a rule without a target there is undecided too."""
    hits = []
    undecided = []
    for rule in firewalls or []:
        if not isinstance(rule, dict):
            continue
        if rule.get("disabled") or (rule.get("direction") or INGRESS).upper() != INGRESS:
            continue
        world = [r for r in rule.get("sourceRanges") or [] if r in WORLD_SOURCE_RANGES]
        if not world:
            continue
        ports = sorted(_tcp_management_ports(rule.get("allowed") or []) - _shadowed_ports(rule, firewalls, project))
        if not ports:
            continue
        reachable = _reachable_instances(rule, instances, project, ports, firewalls, forwarding_rules)
        exposed = [item[0] for item in reachable]
        if not exposed:
            # Undecided only when no visible instance carries the target. A
            # target whose instances have no external IP, are stopped, or are
            # behind a DENY is clear: nothing on the internet can dial them.
            target_scoped = rule.get("targetTags") or rule.get("targetServiceAccounts")
            if target_scoped and not any(
                isinstance(inst, dict) and _in_target(rule, inst, project) for inst in instances or []
            ):
                undecided.append(str(rule.get("name", "")))
            elif not target_scoped:
                network = _network_key(rule.get("network", ""), project)
                clusters = (autopilot_networks or {}).get(network)
                unknown = sorted(
                    {project, network.split("/")[1]} & set(clusters_unread or ())
                )
                if clusters:
                    undecided.append(f"{rule.get('name', '')} (Autopilot cluster(s) {', '.join(sorted(clusters))})")
                elif unknown:
                    undecided.append(f"{rule.get('name', '')} (clusters read failed in {', '.join(unknown)})")
            continue
        remainder = len(exposed) - MAX_NAMED_EXPOSED_INSTANCES
        # The ports that some reported instance can be reached on, not every
        # port the rule names: a DENY can block some of them.
        open_ports = sorted(set().union(*(item[2] for item in reachable)))
        through_lb = any("(through load balancer " in label for label in exposed)
        hits.append({
            "object": f"FirewallRule/{rule.get('name', '')}",
            "excerpt": (
                f"sourceRanges {', '.join(world)} allows tcp:"
                f"{', '.join(f'{p} ({MANAGEMENT_PORTS[p]})' for p in open_ports)}; "
                f"{_target_scope_phrase(rule)}; {len(exposed)} instance(s) visible to the audit "
                f"identity are reachable today: "
                f"{', '.join(exposed[:MAX_NAMED_EXPOSED_INSTANCES])}"
                + (f", +{remainder} more" if remainder > 0 else "")
                + (f"; {LOAD_BALANCER_PATH_NOTE}" if through_lb else "")
                + f"; {FIREWALL_POLICY_BLIND_SPOT}"
            ),
            "projects": sorted({item[1] for item in reachable}),
            "lb_projects": sorted(set().union(*(item[3] for item in reachable))),
        })
    return hits, sorted(undecided)


def undecided_limitation(undecided: list[str]) -> str:
    """The project target's limitations sentence for undecided rules, or ""."""
    if not undecided:
        return ""
    return UNDECIDED_FIREWALL_LIMITATION.format(count=len(undecided), names=", ".join(undecided))


def instance_union(groups: list[list]) -> list[dict]:
    """Every instance in `groups`, each once, keyed by its selfLink, or by name
    and zone where a test fixture carries no link."""
    seen: dict[str, dict] = {}
    for group in groups:
        for inst in group or []:
            if isinstance(inst, dict):
                key = inst.get("selfLink") or f"{inst.get('zone', '')}/{inst.get('name', '')}"
                seen.setdefault(key, inst)
    return list(seen.values())


def firewall_command(project: str, hit: dict) -> str:
    """The evidence command for one §2.6 hit: the project's firewall read, the
    `instances list` read of each project that holds a reachable instance, and
    the `forwarding-rules list` read of each project whose load balancer made an
    instance reachable."""
    reads = FIREWALL_READS[project]
    parts = [reads["firewalls_command"], reads["instances_command"]]
    for origin in hit.get("projects") or []:
        if origin != project and origin in FIREWALL_READS:
            parts.append(FIREWALL_READS[origin]["instances_command"])
    for origin in hit.get("lb_projects") or []:
        command = (FIREWALL_READS.get(origin) or {}).get("forwarding_command")
        if command and command not in parts:
            parts.append(command)
    return joined_command(parts)


def apply_fleet_firewall_reads(
    entries: list[dict],
    projects: list[str] = (),
    api_off: list[str] = (),
    autopilot_networks: dict | None = None,
    clusters_unread: list | None = None,
) -> None:
    """Measure again the §2.6 rules of each collected project target against the
    instances of every project, not its own only. A Shared VPC host holds the
    firewall rules, and its service projects hold the VMs.

    A project in scope with no firewall reads had a read that failed, and its
    instances are not in the fleet list. Each target names those projects in
    its `limitations`, so its verdict does not rest on a blind read. A project
    whose Compute Engine API is off holds no instance, so it is not named.

    The forwarding rules of every project are a second fleet list: a service
    project can hold the load balancer that a host's rule names. The Autopilot
    networks come from the subnet sweep's clusters read, when the run made it.
    A project whose `forwarding-rules list` failed adds no forwarding rule, so
    each other target names it in its `limitations`."""
    fleet_instances = instance_union([reads["instances"] for reads in FIREWALL_READS.values()])
    fleet_forwarding_rules = [
        rule for reads in FIREWALL_READS.values() for rule in reads.get("forwarding_rules") or []
    ]
    unread_projects = sorted(set(projects) - set(FIREWALL_READS) - set(api_off))
    forwarding_unread = sorted(p for p, reads in FIREWALL_READS.items() if reads.get("forwarding_unread"))
    for entry in entries:
        project = entry.get("project", "")
        if entry.get("outcome") != OUTCOME_COLLECTED or project not in FIREWALL_READS:
            continue
        reads = FIREWALL_READS[project]
        hits, undecided = world_open_ingress(
            reads["firewalls"], fleet_instances, project, fleet_forwarding_rules, autopilot_networks, clusters_unread
        )
        entry["candidates"] = [c for c in entry["candidates"] if c["check"] != FIREWALL_CHECK_SLUG] + [
            emit(FIREWALL_CHECK_SLUG, hit, firewall_command(project, hit)) for hit in hits
        ]
        others = [other for other in unread_projects if other != project]
        fleet_gap = (
            FLEET_INSTANCES_UNREAD_LIMITATION.format(count=len(others), names=", ".join(others)) if others else ""
        )
        others_fwd = [other for other in forwarding_unread if other != project]
        fleet_fwd_gap = (
            FLEET_FORWARDING_UNREAD_LIMITATION.format(count=len(others_fwd), names=", ".join(others_fwd))
            if others_fwd else ""
        )
        limitations = [*reads["limitations"], undecided_limitation(undecided), fleet_gap, fleet_fwd_gap]
        limitations = [text for text in limitations if text]
        if limitations:
            entry["limitations"] = LIMITATION_JOINER.join(limitations)
        else:
            entry.pop("limitations", None)


# --------------------------------------------------------------------------- #
# The manifest.
# --------------------------------------------------------------------------- #


class SlugReads:
    """The reads that backed one check: their commands, their summed time, and
    a running digest of their output. Each body is hashed as it arrives and not
    kept, so a project's reads do not pile up in memory; hashing in order equals
    hashing the concatenation."""

    def __init__(self) -> None:
        self.commands: list[str] = []
        self.duration_s = 0.0
        self._digest = hashlib.sha256()

    def add(self, command: str, stdout: str, seconds: float) -> None:
        self.commands.append(command)
        self.duration_s += seconds
        self._digest.update((stdout or "").encode("utf-8"))

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def joined_command(parts: list[str]) -> str:
    """`parts` joined as one pasteable line, clipped at a join boundary with
    the cut reads counted rather than dropped."""
    joined = COMMAND_JOINER.join(parts)
    if len(joined) <= MAX_COMMAND_CHARS:
        return joined
    kept = parts[:1]
    for part in parts[1:]:
        if len(COMMAND_JOINER.join([*kept, part])) > MAX_COMMAND_CHARS - JOIN_TAIL_BUDGET_CHARS:
            break
        kept.append(part)
    return (COMMAND_JOINER.join(kept) + f"  # and {len(parts) - len(kept)} more read(s) of the same shape")[
        :MAX_COMMAND_CHARS
    ]


def joined_record(reads: SlugReads) -> dict:
    """One `commands` entry covering every read that backed one check. `rc` is
    0 because only reads that passed their gate are recorded."""
    return {
        "command": joined_command(reads.commands),
        "rc": 0,
        "duration_s": round(reads.duration_s, 2),
        "output_sha256": reads.hexdigest(),
    }


class GateFailure(Exception):
    """A read failed. The project target records the checks that use this read
    as unevaluated, and the other checks keep their verdicts."""


class ComputeApiDisabled(Exception):
    """The project's own Compute Engine API is off: nothing here to audit."""


def emit(slug: str, hit: dict, command: str) -> dict:
    """A manifest candidate. `command` is the read that produced this hit, so
    `adopt_collector_evidence` publishes a command that reproduces it."""
    return {
        "check": slug,
        "namespace": "",
        "object": hit["object"],
        "severity": SEVERITY[slug],
        "excerpt": hit["excerpt"],
        "impact": IMPACT[slug],
        "command": command,
    }


def project_commands(project: str) -> dict[str, list[str]]:
    """The project target's list reads, by role."""
    flag, fmt = f"{PROJECT_FLAG}={project}", JSON_FORMAT
    return {
        "routers": [GCLOUD, "compute", "routers", "list", flag, fmt],
        "forwarding_rules": [GCLOUD, "compute", "forwarding-rules", "list", flag, fmt],
        "networks": [GCLOUD, "compute", "networks", "list", flag, fmt],
        "security_policies": [GCLOUD, "compute", "security-policies", "list", flag, fmt],
        "backend_services": [GCLOUD, "compute", "backend-services", "list", flag, fmt],
        "firewall_rules": [GCLOUD, "compute", "firewall-rules", "list", flag, fmt],
        "instances": [GCLOUD, "compute", "instances", "list", flag, FIREWALL_INSTANCES_FORMAT],
    }


def router_commands(project: str, router: str, region: str, nat: str | None = None) -> list[str]:
    """A router's `get-status`, or one gateway's `get-nat-mapping-info`."""
    tail = [f"--region={region}", f"{PROJECT_FLAG}={project}", JSON_FORMAT]
    if nat is None:
        return [GCLOUD, "compute", "routers", "get-status", router, *tail]
    return [GCLOUD, "compute", "routers", "get-nat-mapping-info", router, f"--nat-name={nat}", *tail]


def collect_project_target(project: str, instance_cache: dict | None = None) -> dict | None:
    """The `project/<project>` entry for the five project-level checks, or None
    for a project whose own Compute Engine API is off.

    A failed read costs only the checks that use it. Each such check goes in
    `checks_unevaluated` with a `limitations` sentence, and the other checks
    keep their verdicts. The target is `gate-failed` only when no check has a
    read that passed. `instance_cache` holds the `instances list` result that
    the subnet sweep already read for this project, so the run reads it once.
    """
    name = f"{PROJECT_TARGET_PREFIX}{project}"
    reads: dict[str, SlugReads] = {}
    candidates: list[dict] = []
    unevaluated: list[dict] = []
    limitations: list[str] = []
    errors: list[str] = []
    cmds = project_commands(project)

    def gated(cmd: list[str], slug: str, shape: type = list):
        parsed, error, stdout, seconds = read_gcloud_json(cmd)
        if parsed == API_DISABLED:
            raise ComputeApiDisabled()
        if error is not None or not isinstance(parsed, shape):
            raise GateFailure(error or f"`{shlex.join(cmd)}` returned {type(parsed).__name__}")
        reads.setdefault(slug, SlugReads()).add(shlex.join(cmd), stdout, seconds)
        return parsed

    def unread(slug: str, exc: GateFailure) -> None:
        reads.pop(slug, None)
        candidates[:] = [c for c in candidates if c["check"] != slug]
        errors.append(f"{slug}: {exc}")
        reason = str(exc)[:ERROR_EXCERPT_CHARS]
        unevaluated.append({"check": slug, "reason": reason})
        limitations.append(UNREAD_CHECK_LIMITATION.format(check=slug, error=reason))

    def nat() -> None:
        routers = gated(cmds["routers"], NAT_CHECK_SLUG)
        for router in routers:
            if not isinstance(router, dict) or not router.get("nats"):
                continue
            router_name = router.get("name", "")
            region = _last_segment(router.get("region", ""))
            router_reads = [cmds["routers"], router_commands(project, router_name, region)]
            status = gated(router_reads[-1], NAT_CHECK_SLUG, dict)
            mappings = {}
            for gateway in router["nats"]:
                if not gateway.get("enableDynamicPortAllocation"):
                    continue
                nat_name = gateway.get("name", "")
                router_reads.append(router_commands(project, router_name, region, nat_name))
                mappings[nat_name] = gated(router_reads[-1], NAT_CHECK_SLUG)
            hit = check_router_nat(router, status, mappings)
            if hit:
                candidates.append(emit(NAT_CHECK_SLUG, hit, joined_command([shlex.join(c) for c in router_reads])))

    project_forwarding_rules: list = []
    forwarding_read: dict = {}

    def psc() -> None:
        forwarding_rules = gated(cmds["forwarding_rules"], PSC_CHECK_SLUG)
        project_forwarding_rules.extend(
            {**rule, ORIGIN_PROJECT_KEY: project} for rule in forwarding_rules if isinstance(rule, dict)
        )
        forwarding_read["stdout"] = json.dumps(forwarding_rules)
        candidates.extend(
            emit(PSC_CHECK_SLUG, hit, shlex.join(cmds["forwarding_rules"]))
            for hit in check_psc_routing(forwarding_rules)
        )

    def mtu() -> None:
        networks = gated(cmds["networks"], MTU_CHECK_SLUG)
        candidates.extend(
            emit(MTU_CHECK_SLUG, hit, shlex.join(cmds["networks"])) for hit in check_mtu_mismatch(networks, project)
        )

    def armor() -> None:
        policies = gated(cmds["security_policies"], ARMOR_CHECK_SLUG)
        backends = gated(cmds["backend_services"], ARMOR_CHECK_SLUG)
        armor_command = command_text(cmds["security_policies"], cmds["backend_services"])
        candidates.extend(emit(ARMOR_CHECK_SLUG, hit, armor_command) for hit in check_cloud_armor(policies, backends))

    undecided: list[str] = []

    def cached_instances() -> list:
        cached = (instance_cache or {}).get(project)
        if cached is None:
            return gated(cmds["instances"], FIREWALL_CHECK_SLUG)
        items, error = cached
        if items == API_DISABLED:
            raise ComputeApiDisabled()
        if error is not None or not isinstance(items, list):
            raise GateFailure(error or f"`{shlex.join(cmds['instances'])}` returned {type(items).__name__}")
        reads.setdefault(FIREWALL_CHECK_SLUG, SlugReads()).add(shlex.join(cmds["instances"]), json.dumps(items), 0.0)
        return items

    def firewall() -> None:
        firewalls = gated(cmds["firewall_rules"], FIREWALL_CHECK_SLUG)
        instances = [
            {**inst, ORIGIN_PROJECT_KEY: project}
            for inst in cached_instances()
            if isinstance(inst, dict)
        ]
        forwarding_unread = "stdout" not in forwarding_read
        if forwarding_unread:
            limitations.append(FORWARDING_UNREAD_LIMITATION)
        else:
            reads[FIREWALL_CHECK_SLUG].add(shlex.join(cmds["forwarding_rules"]), forwarding_read["stdout"], 0.0)
        FIREWALL_READS[project] = {
            "firewalls": firewalls,
            "instances": instances,
            "firewalls_command": shlex.join(cmds["firewall_rules"]),
            "instances_command": shlex.join(cmds["instances"]),
            "limitations": limitations,
            "forwarding_rules": project_forwarding_rules,
            "forwarding_command": None if forwarding_unread else shlex.join(cmds["forwarding_rules"]),
            "forwarding_unread": forwarding_unread,
        }
        hits, rules = world_open_ingress(firewalls, instances, project, project_forwarding_rules)
        candidates.extend(emit(FIREWALL_CHECK_SLUG, hit, firewall_command(project, hit)) for hit in hits)
        undecided.extend(rules)

    try:
        for slug, check in (
            (NAT_CHECK_SLUG, nat),
            (PSC_CHECK_SLUG, psc),
            (MTU_CHECK_SLUG, mtu),
            (ARMOR_CHECK_SLUG, armor),
            (FIREWALL_CHECK_SLUG, firewall),
        ):
            try:
                check()
            except GateFailure as exc:
                unread(slug, exc)
    except ComputeApiDisabled:
        FIREWALL_READS.pop(project, None)
        sys.stderr.write(f"{project}: Compute Engine API is not enabled; nothing here to audit\n")
        return None
    if not reads:
        return {
            "name": name,
            "project": project,
            "location": GLOBAL_LOCATION,
            "outcome": OUTCOME_GATE_FAILED,
            # The first failure in full: it names the read that every later
            # read then also failed.
            "error": errors[0],
        }
    entry = {
        "name": name,
        "project": project,
        "location": GLOBAL_LOCATION,
        "outcome": OUTCOME_COLLECTED,
        "commands": [{"check": slug, **joined_record(slug_reads)} for slug, slug_reads in reads.items()],
        "candidates": candidates,
        "checks_not_applicable": [],
    }
    if unevaluated:
        entry["checks_unevaluated"] = unevaluated
    all_limitations = [*limitations, undecided_limitation(undecided)]
    all_limitations = [text for text in all_limitations if text]
    if all_limitations:
        entry["limitations"] = LIMITATION_JOINER.join(all_limitations)
    return entry


def candidate_from_finding(finding: dict) -> dict:
    """A subnet sweep finding as a manifest candidate: the identity, the
    computed evidence and the impact. The title and recommendation are the
    worker's to write."""
    evidence = finding.get("evidence") or {}
    return {
        "check": finding["check"],
        "namespace": finding.get("namespace", ""),
        "object": finding["object"],
        "severity": finding["severity"],
        "excerpt": evidence.get("excerpt", ""),
        "impact": finding.get("impact", ""),
        "command": evidence.get("command", ""),
    }


def subnet_targets(projects: list[str], usage: dict | None = None) -> list[dict]:
    """The subnet sweep's entries, skipped rows and findings as manifest
    targets: each evaluated subnet `collected` with its findings as candidates,
    each skipped row `gate-failed` with its reason as the error."""
    skipped: list[dict] = []
    active: list[dict] = []
    usage = usage if usage is not None else read_subnet_usage(projects)
    findings = audit_subnet_capacity(projects, usage, skipped, active)
    by_target: dict[str, list[dict]] = {}
    for finding in findings:
        by_target.setdefault(finding["cluster"], []).append(candidate_from_finding(finding))
    entries = []
    for entry in active:
        target = {
            "name": entry["name"],
            "project": entry["project"],
            "location": entry["location"],
            "outcome": OUTCOME_COLLECTED,
            "commands": [{"check": run["check"], "command": run["command"], "rc": 0} for run in entry["checks_run"]],
            "candidates": by_target.get(entry["name"], []),
            "checks_not_applicable": [],
        }
        if entry.get("limitations"):
            target["limitations"] = entry["limitations"]
        entries.append(target)
    for row in skipped:
        entries.append({
            "name": row["name"],
            "project": row["project"],
            "location": row["location"],
            "outcome": OUTCOME_GATE_FAILED,
            "error": row["reason"],
        })
    return entries


def unenumerated_entry(notes: list[str]) -> dict:
    """One `project/UNENUMERATED_PROJECTS` row for everything that narrowed or
    lost part of the fleet; its notes are clipped, not its tail."""
    budget = ERROR_EXCERPT_CHARS - len(UNENUMERATED_TAIL) - len(SENTENCE_END)
    name = f"{PROJECT_TARGET_PREFIX}{UNENUMERATED_PROJECTS}"
    return {
        "name": name,
        "project": UNENUMERATED_PROJECTS,
        "location": GLOBAL_LOCATION,
        "outcome": OUTCOME_GATE_FAILED,
        "error": f"{NOTE_SEPARATOR.join(notes)[:budget]}{SENTENCE_END}{UNENUMERATED_TAIL}",
    }


def collect_fleet(project_id: str | None = None, check: str = CHECK_ALL) -> dict:
    """The run manifest for every project in scope."""
    started_at = time.strftime(TIMESTAMP_FORMAT, time.gmtime())
    notes: list[str] = []
    projects = get_target_projects(project_id, notes)
    for note in notes:
        sys.stderr.write(f"{note}; auditing {projects or 'no project'}\n")
    entries: list[dict] = []
    api_off: list[str] = []
    FIREWALL_READS.clear()
    # The subnet sweep's reads come first: the firewall check uses its clusters
    # read to find the networks that hold an Autopilot cluster.
    instance_cache: dict = {}
    usage = read_subnet_usage(projects, instance_cache) if check in (CHECK_ALL, SUBNET_CHECK_SLUG) else None
    if check in (CHECK_ALL, PROJECT_CHECKS_CHOICE):
        for project in projects:
            entry = collect_project_target(project, instance_cache)
            if entry is None:
                api_off.append(project)
            else:
                entries.append(entry)
        apply_fleet_firewall_reads(
            entries, projects, api_off, (usage or {}).get("autopilot_networks"), (usage or {}).get("clusters_unread")
        )
    if check in (CHECK_ALL, SUBNET_CHECK_SLUG):
        entries.extend(subnet_targets(projects, usage))
    if notes:
        entries.append(unenumerated_entry(notes))
    if not projects:
        sys.stderr.write("No target projects resolved from CLI, environment, or gcloud.\n")
        entries.append({
            "name": f"{PROJECT_TARGET_PREFIX}{UNKNOWN_PROJECT}",
            "project": UNKNOWN_PROJECT,
            "location": GLOBAL_LOCATION,
            "outcome": OUTCOME_GATE_FAILED,
            "error": "No GCP project ID configured or resolved",
        })
    manifest = {
        "version": MANIFEST_VERSION,
        "checks_revision": CHECKS_REVISION,
        "audit": AUDIT_SLUG,
        "started_at": started_at,
        "finished_at": time.strftime(TIMESTAMP_FORMAT, time.gmtime()),
        "clusters": entries,
    }
    # No target was read. The design's top-level `error` says so, and the SOP
    # answers it by not calling `finish`: the run has nothing to publish.
    if not any(entry.get("outcome") == OUTCOME_COLLECTED for entry in entries):
        reasons = [str(entry["error"]) for entry in entries if entry.get("error")]
        if api_off:
            reasons.insert(0, f"the Compute Engine API is off in {', '.join(sorted(api_off))}")
        manifest["error"] = (
            f"no target could be read: {len(entries)} target(s), none collected"
            + (f"{NOTE_SEPARATOR}{reasons[0]}" if reasons else "")
        )[:ERROR_EXCERPT_CHARS]
    return manifest


def write_atomically(path: str, text: str) -> None:
    """`text` to `path` through a sibling temporary file, so a run killed while
    writing leaves no half a manifest behind."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    partial = f"{path}.partial"
    with open(partial, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(partial, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect the GCP networking fabric audit's run manifest")
    parser.add_argument("--project-id", help="audit one project; omit to resolve the scope (SOP §1)")
    parser.add_argument("--output", help="write the manifest here and print a summary instead of it")
    parser.add_argument("--check", choices=CHECK_CHOICES, default=CHECK_ALL,
                        help=f"run the subnet sweep, the project-level checks ({PROJECT_CHECKS_CHOICE}), or both")
    args = parser.parse_args(argv)
    # A run killed before it writes must not leave the last run's manifest to
    # be read as this one's.
    if args.output:
        pathlib.Path(args.output).unlink(missing_ok=True)
    manifest = collect_fleet(args.project_id, args.check)
    text = json.dumps(manifest, indent=JSON_INDENT)
    if args.output:
        try:
            write_atomically(args.output, text)
        except OSError as exc:
            sys.stderr.write(f"Failed to write output to {args.output}: {exc}\n")
            return 1
        collected = sum(1 for e in manifest["clusters"] if e.get("outcome") == OUTCOME_COLLECTED)
        found = sum(len(e.get("candidates") or []) for e in manifest["clusters"])
        print(f"Collected {collected} of {len(manifest['clusters'])} target(s), {found} candidate(s); "
              f"wrote {args.output}" + (f"; error: {manifest['error']}" if manifest.get("error") else ""))
    else:
        print(text)
    return 1 if manifest.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
