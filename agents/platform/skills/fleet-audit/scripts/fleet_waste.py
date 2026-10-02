#!/usr/bin/env python3
"""fleet_waste.py — Procedural collector for the Fleet Waste Audit
(`fleet-wide-cost-analysis`).

Its manifest is the contract in docs/designs/fleet-audit-collector-manifest.md,
which `audit_report.py finish --manifest-file` cross-checks the published
document against; the checks it runs are defined in
governance/fleet_wide_cost_analysis_sop.md.

This stream's own collector: its targets are both GKE clusters (the fifteen
`kubectl` object kinds in `CLUSTER_DUMP_KINDS`, a Cloud
Monitoring usage read, `gcloud container node-pools list`, and §3.7's
`gcloud container operations list` for `CREATE_NODE_POOL`, which dates each
pool; a pool with no such operation is dated from the cluster's `createTime`,
and only a failed operations read falls back to node age, with a limitation)
and GCP projects
(`gcloud compute disks/addresses/forwarding-rules/target-pools/backend-services`
and `gcloud artifacts repositories list`),
so its manifest mixes cluster-named entries with `project/<id>` entries the
same way `networking_audit.py` does (§3's "project-scoped GCP objects" rule).

§1 scopes this to "every project the agent can see", so a bare invocation
reads the active project plus every listed project, whether or not it holds a
cluster -- a project whose last cluster was deleted is where its disks and
addresses are left behind -- rather than auditing only the active gcloud
project; `--project` overrides discovery for a scoped run. Each project's
cluster listing runs in a pool before the clusters are read, and its disk,
address, load-balancer and registry reads share the cluster pool; neither is
started after `PROJECT_READ_DEADLINE_S`, so the run aims to end inside its
terminal timeout with a manifest. Project-scoped facts (live PV handles, Service
names, referenced addresses) are unioned only across the clusters in the
same project before that project's disk/address/LB checks run — a project
never sees another project's cluster state.

**Usage comes from Cloud Monitoring, not from sampling.** §2 used to require
three `kubectl top` reads five minutes apart per cluster, and the ten minutes
of wall clock that bought was the smaller of its two costs. The larger one was
what a ten-minute Monday-morning window cannot see: a nightly batch peak, a
weekday traffic curve, anything that makes a workload look idle at the moment
you happen to look at it. Every caveat §2 carried — require all three samples
to agree, take the peak and never the mean, keep an absolute floor, never
propose a request below 2x the observed peak — was scaffolding around that
blind spot.

GKE already ships per-container CPU and memory to Cloud Monitoring on every
cluster, retained for weeks. `fetch_usage_peaks` asks it for the peak over the
trailing `USAGE_WINDOW_HOURS` instead, which is both faster (a handful of HTTP
reads per cluster, no sleeping at all) and strictly better evidence: a week-long
peak has already seen the batch job the sample window missed. It reads through
the credential broker's read-only Cloud API relay
(`docs/designs/gcp-api-relay.md`), so no token is ever materialized in the
sandbox, and `roles/monitoring.viewer` on the broker's identity is the only
grant it needs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple

MANIFEST_VERSION = 1
# The manifest's `audit` field: the stream this collector feeds.
AUDIT_NAME = "fleet-wide-cost-analysis"
# The one `kubectl get` each collected cluster costs. The SOP quotes this list
# to tell the model which kinds not to read again.
CLUSTER_DUMP_KINDS = "nodes,pods,pvc,pv,svc,jobs,cronjobs,pdb,ns,resourcequota,sts,deploy,hpa,limitrange,ingress"

# A digest of this file, published as `checks_revision`. The manifest contract
# (docs/designs/fleet-audit-collector-manifest.md §2) carries it unread today,
# reserved for the run-over-run comparison that tells a finding that stopped
# reproducing from a check that stopped looking. Long enough that two collector
# sources will not collide, short enough to read in a log line, and the same
# width in every collector: a file that truncated differently would report a
# moved collector on the run that changed it.
REVISION_DIGEST_CHARS = 12
CHECKS_REVISION = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[
    :REVISION_DIGEST_CHARS
]

KUBECONFIG_DIR = Path(os.environ.get("HERMES_HOME") or "/opt/data") / ".kubeconfigs"
DEFAULT_TIMEOUT_S = 60
# The exit status a `Run` reports for a command `subprocess.run` killed on
# its timeout -- coreutils `timeout`'s, and the one collect.py, fleet_drift.py
# and patch_readiness.py report, so a timeout reads the same in every manifest.
TIMEOUT_RC = 124
# Was 64, sized so every cluster's ten-minute sampling window ran
# concurrently rather than queuing behind an earlier one. Nothing sleeps any
# more -- per-cluster work is a handful of subprocess reads and a few HTTP
# reads -- so this drops back to the 8 every other collector in the stream
# uses, which also keeps the shared Monitoring session inside urllib3's
# default connection pool.
MAX_WORKERS = 8

# What the manifest calls a target. A GKE name is unique only inside one
# project and location, so every cluster is `<project>/<location>/<name>`, and
# a failed or narrowed project discovery is one `project/UNENUMERATED_PROJECTS`
# target. Copied from `collect.py` under the standalone-collector rule below;
# `audit_report.py` reads both shapes, so keep them in step with it.
QUALIFIED_TARGET_SEPARATOR = "/"
PROJECT_TARGET_PREFIX = "project/"
# A cluster the collector never read: not running, or its credentials failed.
UNREACHABLE_OUTCOME = "unreachable"
# On a `project/<id>` entry whose `clusters list` completed and came back
# empty -- never a failed or zone-incomplete one. `audit_report.py` reads it
# (as `CLUSTERS_LISTED_KEY`) to tell a fleet with no clusters from a run that
# lost them, so the cluster checks' kind gap does not pin the run partial.
CLUSTERS_LISTED_KEY = "clusters_listed"
UNENUMERATED_PROJECTS_TARGET = PROJECT_TARGET_PREFIX + "UNENUMERATED_PROJECTS"
NO_PROJECT_IN_SCOPE_ERROR = (
    "no project in scope: there is no active gcloud project and `gcloud projects list` "
    "returned none, so this credential sees nothing to audit"
)
SCOPED_RUN_NOTE = (
    "scope narrowed to project {project!r} by `--project`: discovery was skipped, so no other "
    "project in this fleet was named or read, and this run cannot speak for their clusters."
)
ERROR_EXCERPT_CHARS = 300
# §3.2's addon exclusion: addon-manager stamps this as a label; an
# annotation is read too, as before.
ADDON_MANAGER_KEY = "addonmanager.kubernetes.io/mode"
# The shorter excerpt a stderr gets where it sits inside a longer sentence.
DETAIL_EXCERPT_CHARS = 200
# gcloud's words for a project whose Kubernetes Engine API is off. Such a
# project cannot hold a cluster, so its `clusters list` failure is an answer
# rather than a read that failed; otherwise every non-GKE project a credential
# can see is a permanent `gate-failed` target. Copied from `collect.py`.
API_DISABLED_MARKERS = ("SERVICE_DISABLED", "accessNotConfigured", "has not been used in project")
# The project an API refusal names, which gcloud gives by number ("has not been
# used in project 123456789", "consumer: projects/123456789"). The refusal is
# the consumer project's, and with a quota project set (`billing/quota_project`)
# that is not the project being listed.
REFUSED_PROJECT_NUMBER_RE = re.compile(r"\bprojects?[ /](\d+)\b")
# The project *id* a refusal names, when it names one rather than a number,
# in gcloud's phrasings: `project <id> before`, `Project <id> is not found`,
# `projects/<id>`, then punctuation, a quote or the end. A project id is 6-30
# lowercase letters, digits and hyphens, starting with a letter and not ending
# in a hyphen. Used only to say which project a refusal was about.
REFUSED_PROJECT_ID_RE = re.compile(
    r"\b(?i:projects?)[ /]['\"\[]?([a-z][a-z0-9-]{4,28}[a-z0-9])"
    r"(?=\s+(?:before|is|was|has|does)\b|['\"\],.;:)]|\s*$)"
)
# English words of id shape that gcloud's prose puts where an id could sit
# ("... on this project either."): never read as the project a refusal names.
REFUSED_PROJECT_ID_STOPWORDS = frozenset({"before", "either", "itself", "number", "should", "settings"})
# The read `refusal_names_project` makes to turn a project id into the number
# a refusal names. `collect_fleet` answers a repeat of it from the first answer.
PROJECT_DESCRIBE_ARGV = ["gcloud", "projects", "describe"]
# The zone or region and name in a PV's disk handle
# (`projects/p/zones/us-central1-a/disks/data-1`, `.../regions/r/disks/n`).
PV_DISK_HANDLE_RE = re.compile(r"(?:^|/)(?:zones|regions)/([^/]+)/disks/([^/]+)$")
# §3.5's description exclusion: an address held for DR, failover, or a planned
# migration is waiting on purpose. Whole words, any case, so `DR` does not
# match inside `address` or `drain`.
HELD_ADDRESS_DESCRIPTION_RE = re.compile(r"\b(?:dr|disaster[\s-]+recovery|fail-?over|migrat(?:e[ds]?|ions?|ing))\b", re.IGNORECASE)
# The region (absent for a global one) and name in a backend service URL,
# `.../regions/<r>/backendServices/<n>` or `.../global/backendServices/<n>`.
BACKEND_SERVICE_URL_RE = re.compile(r"(?:regions/([^/]+)|global)/backendServices/([^/]+)$")
# gcloud's word for a zone that timed out during `clusters list`: the command
# still exits 0, with the clusters the other zones returned and this line on
# stderr, so the silent zone's clusters would read as nonexistent. See
# `fleet_drift.ZONE_TIMEOUT_MARKER`.
ZONE_TIMEOUT_MARKER = "did not respond"
# The same answer from the Compute Engine or Artifact Registry API: nothing
# §3.4-§3.6 or §3.14 looks for can exist in that project, so the check has
# nothing to run against there rather than a read that failed. An
# organisation-wide credential sees many such projects now that discovery
# keeps every listed one, and each would otherwise pin the run partial.
COMPUTE_CHECKS = ("idle-address", "orphan-lb", "unattached-disk")
COMPUTE_DISABLED_REASON = (
    "the Compute Engine API is not enabled in project {project!r}, so no disk, address, "
    "forwarding rule or backend service can exist there"
)
REGISTRY_DISABLED_REASON = (
    "the Artifact Registry API is not enabled in project {project!r}, so no repository can exist there"
)
# When the collector stops starting project reads, in seconds from its own
# start. §2 runs it as one foreground terminal call of 600 s, and a call that
# overruns is killed with no manifest at all; every listed project costs a
# handful of gcloud reads, so a credential that sees hundreds of projects
# would get there. A project not reached by this point becomes a
# `gate-failed` `project/<p>` target, which the document carries into
# `scope.skipped`, and the run reports partial rather than nothing. This is a
# best-effort cutoff, not a bound. The 180 s left before the terminal timeout
# is for the project reads already in flight, which take seconds on a healthy
# API, but one project's reads run in sequence: its gcloud reads under
# `DEFAULT_TIMEOUT_S` each, then `fetch_lb_traffic`'s three Monitoring metrics,
# each paged with every page under `MONITORING_TIMEOUT_S`, so a single slow
# project can outlast the margin on its own. The cluster reads, which this does
# not bound either, finish on their own time as they did before it.
PROJECT_READ_DEADLINE_S = 420
# `gcloud projects list`'s own timeout. Under `DEFAULT_TIMEOUT_S` a credential
# that sees hundreds of projects was killed mid-listing, and the run fell back
# to the active project alone -- the case `PROJECT_READ_DEADLINE_S` is sized
# for, made unreachable. The listing runs before any project read and counts
# against that deadline, so this leaves project reads time to start.
PROJECTS_LIST_TIMEOUT_S = 240
PROJECT_DEADLINE_ERROR = (
    "not read: the collector stops starting project reads {budget} s after it starts, so the "
    "run can end inside its terminal timeout with a manifest, and this project's turn came "
    "after that. Rerun with `--project {project}` to read it on its own."
)
NOTHING_COLLECTED_ERROR = (
    "nothing collected: none of the {count} project(s) in scope yielded a target -- each failed "
    "its cluster listing, went unread past the deadline, or has neither the Compute Engine nor "
    "the Artifact Registry API on and no running cluster. First: {first}"
)
# The opening of the note a filtered `projects list` leaves: the listing
# succeeded, so like the `--project` note it says what a run may have
# missed, never why nothing was collected.
FILTERED_LISTING_NOTE = "`gcloud projects list` rc=0 did not name the active project"

# `NOTHING_COLLECTED_ERROR`'s `first` when no target carries an error: the one
# way a project yields nothing without recording why.
NO_TARGET_REASON = (
    "no project in scope recorded an error, so each holds no cluster and has neither the Compute Engine nor the Artifact Registry API on"
)

# Where a GitOps clone keeps the manifests applied to one cluster:
# `clusters/<cluster>/...`, so a path shorter than two parts names no cluster.
# Copied from `collect.py` rather than imported: every collector in this
# directory runs standalone under `python3 <file>`, and each one that has
# needed a constant a sibling also declares has duplicated it (`SYSTEM_NAMESPACES`
# is the same value in three of them). Keep these three in step with
# `collect.py`'s if the repository layout moves.
GITOPS_CLUSTER_TREE_ROOT = "clusters"
GITOPS_CLUSTER_TREE_DEPTH = 2
GIT_DIR_NAME = ".git"
# Config Connector's API group. Its objects name a GCP resource, which
# `spec.resourceID` can override, not a workload, so `workload_declarations`
# skips them rather than resolving a `Cluster/<name>` finding against one.
KCC_API_GROUP_SUFFIX = "cnrm.cloud.google.com"
# `release_declarations` indexes the objects that render a workload a GitOps
# repo holds no manifest for -- an Argo CD `Application`, from either a chart
# or a Kustomize overlay, and a Flux `HelmRelease` -- plus the two it needs to
# resolve them: Argo CD's cluster registration Secret (which is how
# `spec.destination.server` becomes a cluster name) and Flux's `HelmRepository`
# (which is how a `HelmRelease`'s chart reference becomes a URL `helm show
# values` can read). Copied from `collect.py` under the same
# standalone-collector rule as the three above.
ARGOCD_APPLICATION_KIND = "Application"
ARGOCD_CLUSTER_SECRET_LABEL = "argocd.argoproj.io/secret-type"
ARGOCD_CLUSTER_SECRET_VALUE = "cluster"
ARGOCD_IN_CLUSTER_SERVER = "https://kubernetes.default.svc"
FLUX_HELM_RELEASE_KIND = "HelmRelease"
FLUX_HELM_REPOSITORY_KIND = "HelmRepository"
# The two shapes a key in that index takes. An Argo CD chart Application is
# found by the Application name its tracking id carries; a Helm release proper
# -- `helm install`, or Flux driving it -- by release namespace and name.
RELEASE_KEY_APPLICATION = "application"
RELEASE_KEY_RELEASE = "release"
# A third key on that same index, answering a different question: not "where
# is this object declared" but "where would a *new* object for this namespace
# go". Only a local Kustomize root can answer it, because only a local root is
# a directory in this repository that renders into the namespace. No check here
# creates an object, so nothing in this file reads the key; it is built anyway
# because `release_declarations` is a copy of `collect.py`'s and the copy is
# kept identical, not trimmed to what one stream happens to use.
RELEASE_KEY_NAMESPACE = "namespace"
# Where a values override goes, per reconciler. Argo CD accepts both a YAML
# string (`values`) and a structured block (`valuesObject`); this names the one
# already in the file, and `valuesObject` when neither is, because a structured
# block is what a programmatic edit can extend without reindenting a string.
ARGOCD_VALUES_OBJECT_FIELD = "valuesObject"
ARGOCD_VALUES_STRING_FIELD = "values"
FLUX_VALUES_FIELD = "values"
ARGOCD_KUSTOMIZE_PATCHES_FIELD = "patches"
# Which of those two overrides a declaration takes, since they are written
# differently: a values mapping the chart reads by key, or a list of patches
# Kustomize applies to a matched object. Carried on the entry so the SOP can
# branch on it rather than inferring one from the shape of `values_field`.
RENDERER_HELM = "helm"
RENDERER_KUSTOMIZE = "kustomize"
# What marks a directory as a Kustomize root. `kustomization.yaml` is what
# `kustomize create` writes; the other two are the legacy spellings `kustomize
# build` still accepts, and a repo that uses one is exactly as unresolvable
# without this as one that uses the first.
KUSTOMIZATION_FILE_NAMES = ("kustomization.yaml", "kustomization.yml", "Kustomization")

# How far back `fetch_usage_peaks` looks for a workload's peak. A week, to
# match this stream's weekly cadence: every finding is then "this controller
# has not needed that reservation since the last time we said so", and the
# window covers the weekday traffic curve and the weekly batch job that §2's
# ten-minute sample was blind to. Monitoring retains these metrics well past
# this, so the bound is a judgement about relevance, not availability.
USAGE_WINDOW_HOURS = 168
# The two dimensions of a `fetch_usage_peaks` tuple, by index, named as a
# limitation names them when one came back with no series at all.
USAGE_DIMENSIONS = (("CPU", 0), ("memory", 1))
# Under this much observation the measurement has not seen a full daily cycle,
# so a workload with any diurnal shape can read as idle for want of having been
# watched overnight. The finding still publishes -- it is usually right, and the
# stream re-runs -- but the excerpt says so, because what follows it is an
# instruction to shrink a request.
SHORT_OBSERVATION_HOURS = 24
# How a controller names the pods it owns, so `_observed_pod_keys` can find, in
# the Monitoring answer, the pods a controller has since replaced. The answer
# covers the whole window and is keyed by pod name, so those series are already
# in hand -- joining them to the *live* pod list is what threw them away, and
# with them most of the history the window claims to have read.
#
# Anchored at both ends and namespaced by the caller. The two-segment tail is
# what keeps a sibling out: `argocd-repo-server-5fcf7766cd-vjp6f` does not match
# `argocd`'s pattern, because the hash segment cannot span the hyphen that `repo` and
# `server` are separated by. A kind absent from this table is not widened, which
# costs the old, narrow answer rather than a wrong one.
#
# Both generated segments -- the pod-template-hash and the pod's own suffix --
# are drawn from Kubernetes' `utilrand` alphabet, which has no vowels and no
# 0, 1 or 3. Matching that rather than `[a-z0-9]` is what keeps out a hook
# Job's pods: `api-migrate-x7k2p` belongs to Job `api-migrate`, not to
# Deployment `api`, and `migrate` has vowels no ReplicaSet hash can carry.
# It keeps out most hook Jobs, not all: one whose name suffix is vowel-free
# (`api-db`) still matches, and nothing in a pod name tells the two apart.
K8S_GENERATED_CHARS = "[bcdfghjklmnpqrstvwxz2456789]"
# Controller kinds that recreate a pod under its old name, so the Monitoring
# series keyed by that name already spans the pod's earlier incarnations.
SAME_NAME_RECREATION_KINDS = frozenset({"StatefulSet"})
REPLACED_POD_PATTERNS = {
    "Deployment": rf"^{{name}}-{K8S_GENERATED_CHARS}+-{K8S_GENERATED_CHARS}{{{{5}}}}$",
    "StatefulSet": r"^{name}-[0-9]+$",
    "ReplicaSet": rf"^{{name}}-{K8S_GENERATED_CHARS}{{{{5}}}}$",
}
# Alignment happens twice. The primary period buckets each container's raw
# points before they are summed across the containers of a pod -- it has to
# be short enough that a spike stays a spike, since a wide bucket averages
# one away and understates the peak. The secondary pass then takes the max
# across those buckets, which is the number we want, and collapses the
# response to one point per pod: measured on a 137-pod cluster, one page and
# 0.3s rather than ten pages and 3.3s, with both routes agreeing on all 137.
USAGE_ALIGNMENT_S = 300
MONITORING_SCOPE = "https://www.googleapis.com/auth/monitoring.read"
# Where `credential_proxy_client` lives: the shared scripts dir in the image
# (see docker-entrypoint.sh), then the same directory in a source checkout.
# `audit_report.py` appends the same three.
SHARED_SCRIPT_DIRS = (
    "/opt/defaults/scripts",
    "/opt/data/scripts",
    str(Path(__file__).resolve().parents[3] / "scripts"),
)
CREDENTIAL_PROXY_URL_ENV = "CREDENTIAL_PROXY_URL"
NO_SESSION_MESSAGE = (
    "no Cloud Monitoring session: neither the credential broker's relay nor ADC "
    "was available at startup"
)
MONITORING_TIMEOUT_S = 120
MONITORING_TIMESERIES_URL = "https://monitoring.googleapis.com/v3/projects/{project}/timeSeries"
HTTP_OK = 200
MONITORING_PAGE_SIZE = "2000"
#: The failure a 200 whose body is not a JSON object reports. `ApiSession.get`
#: hands back a bare `requests.Response`, so a misbehaving intermediary's HTML
#: page arrives as a 200 and would otherwise raise out of the read.
NON_JSON_BODY = "HTTP 200 with a body that is not a JSON object"
#: What a rc-0 `gcloud ... list` read that `object_list` refused reports.
NOT_AN_OBJECT_LIST = "returned no JSON list of objects"
# Also the manifest's `started_at` / `finished_at` form: both are UTC to the second.
MONITORING_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
SECONDS_PER_HOUR = 3600
# The precision the usage reads round to before they are digested into the
# manifest's stdout stand-in: fine enough that a real change in usage moves
# the digest, coarse enough that float noise between two identical answers
# does not. vCPU to a tenth of a millicore, memory to a tenth of a MiB.
USAGE_DIGEST_CPU_DIGITS = 4
USAGE_DIGEST_MEM_DIGITS = 1
POD_GROUP_BY_FIELDS = ["resource.labels.namespace_name", "resource.labels.pod_name"]
# `--oauth2-bearer`, not an `Authorization: Bearer` header: `finish` redacts
# whatever follows `Bearer`, which cut the published command in half.
MONITORING_CURL_PREFIX = 'curl -sG --oauth2-bearer "$(gcloud auth print-access-token)"'
CPU_METRIC = "kubernetes.io/container/cpu/core_usage_time"
MEM_METRIC = "kubernetes.io/container/memory/used_bytes"
# Cloud Monitoring's aggregation enums, as the query string spells them.
ALIGN_RATE = "ALIGN_RATE"
ALIGN_MAX = "ALIGN_MAX"
ALIGN_MEAN = "ALIGN_MEAN"
ALIGN_SUM = "ALIGN_SUM"
REDUCE_SUM = "REDUCE_SUM"
# The keys `fetch_usage_peaks` files each metric's per-pod peaks under.
CPU_USAGE_KEY = "cpu"
MEM_USAGE_KEY = "mem"
NODEPOOL_LABEL = "cloud.google.com/gke-nodepool"
# Pod phases that do not count as a running replica of their controller.
NOT_A_REPLICA_PHASES = ("Pending", "Failed", "Succeeded")
# A pod younger than an hour has not settled into the usage it will run at.
POD_SETTLE_DAYS = 1 / 24
# `used_bytes` is split by `memory_type`, and the `evictable` half is page
# cache the kernel reclaims under pressure. Kubelet evicts on the working set,
# so summing the two would size requests, and flag underrequest, on cache.
MEM_NON_EVICTABLE_FILTER = ' AND metric.labels.memory_type="non-evictable"'
# What §3.13 reads to say what reached the load balancer in front of an idle
# workload. Three DELTA counters on the L4 external forwarding rule, which is
# what a GKE `type: LoadBalancer` Service creates.
LB_INGRESS_PACKETS_METRIC = "loadbalancing.googleapis.com/l3/external/ingress_packets_count"
LB_EGRESS_PACKETS_METRIC = "loadbalancing.googleapis.com/l3/external/egress_packets_count"
LB_EGRESS_BYTES_METRIC = "loadbalancing.googleapis.com/l3/external/egress_bytes_count"
# A *resource* label, and the qualifier is the whole reason this is a named
# constant rather than a literal in the request. `metric.labels.forwarding_rule_name`
# is a perfectly valid thing to ask the Monitoring API to group by, and it does
# not fail: the field does not exist on these metrics, so every rule in the
# project collapses into one unlabelled series and the caller reads the fleet's
# entire traffic as belonging to whichever rule it was asking about. Verified
# against adamparco-kage on 2026-09-07 -- the metric grouping returned one
# series totalling 743,426 packets where the resource grouping returned three,
# the largest of them 374,158.
LB_RULE_LABEL = "resource.labels.forwarding_rule_name"
# §3.13's fifth traffic shape: ingress above the floor with an outbound series
# missing, so what the workload sent back is not known.
LB_EGRESS_UNMEASURED = "{what} is unmeasured, because Cloud Monitoring holds no outbound series for the rule"
# A query pair made only of characters a URL carries unescaped, which a
# Monitoring label may therefore pass as `-d` rather than `--data-urlencode`.
URL_SAFE_PAIR_RE = re.compile(r"^[A-Za-z0-9._~:-]+=[A-Za-z0-9._~:-]+$")
# A rule name is unique only per region, and `forwarding-rules list` returns
# every region, so the answer is grouped by region too: grouped by name alone,
# two regions' `web` rules folded into one total credited to one address.
# Both monitored resources the counters arrive under carry the label.
LB_REGION_LABEL = "resource.labels.region"
# The forwarding-rule scheme these metrics cover. An INTERNAL rule and a
# Private Service Connect attachment report nothing under `l3/external`, and an
# EXTERNAL_MANAGED (proxy) rule reports under `https/` instead -- so including
# them would return an empty answer indistinguishable from a rule that has one,
# which is the "unmeasured, not zero" mistake this whole read exists to stop
# making.
LB_EXTERNAL_SCHEME = "EXTERNAL"
# A day, against `USAGE_WINDOW_HOURS`' week: the read comes back as seven points
# per rule rather than one. The check sums them, but a rule that went quiet
# mid-window is then a question a reader can answer from the manifest without
# re-running anything.
LB_TRAFFIC_ALIGNMENT_S = 86400
# Under this many inbound packets across the window the rule metered nothing
# worth weighing. `USAGE_WINDOW_HOURS` is 10,080 minutes, so the floor is
# almost exactly one packet a minute -- which is less than a public address
# collects from internet scanners with nothing behind it at all.
LB_TRAFFIC_MIN_PACKETS = 10000
# Mean bytes per *outbound* packet, below which the rule answered without ever
# sending a payload. An IP+TCP header alone is 40 bytes and a SYN-ACK or RST
# with options runs to about 60, so a week that averages under this served
# nobody anything. It is the number that separates the two shapes of traffic
# §3.13 kept confusing: on 2026-09-07 the two forwarding rules whose findings
# merged a stand-down unattended had metered 374,158 and 367,736 inbound
# packets -- and answered at 63.9 and 75.6 bytes per packet, across 42 client
# countries. Busy, and serving nothing.
LB_TRAFFIC_PAYLOAD_BYTES_PER_PACKET = 100
# The materiality floor `check_overrequest` applies to the *request* on an idle
# dimension, so the check does not report a 10m sidecar whose 8m of headroom is
# arithmetically a 5x over-request and operationally nothing.
#
# It was 2 vCPU / 4 GiB, which is a node's worth of headroom inside a single
# controller, and on a fleet of ordinarily-sized services nothing ever reaches
# it: measured against the sixteen-cluster adamparco-kage fleet on 2026-09-05,
# seven controllers were under 20% of their requests on both dimensions --
# including two `ai-inference` Deployments requesting 0.5 vCPU / 2 GiB each and
# peaking at 0.001 vCPU / 0.15 GiB over a week -- and the floor dropped every
# one of them. The stream published a single finding, for an unused IP address,
# and an operator reading it would conclude the fleet had no rightsizing work.
#
# Lowering it to 250m / 512Mi fixed those two and left the shape of the bug in
# place, because the quantity was wrong as well as the number. The floor was
# compared against the reclaimable delta while its justification -- "about the
# smallest request a first-class service is given" -- describes a request, and
# the two coincide only where usage is near zero. So the clusters it kept
# silencing were the ones whose requests sit at or just under the floor and
# which are ~100% idle: on the same fleet, `github-token-minter` requests 200m
# and 256Mi, peaked at 0.001 vCPU and 24 MiB over a week -- half a percent of
# its CPU request -- and was dropped for a 199m delta, and `litellm` was
# dropped for a 176m one at 12% of its CPU request. Eight controllers in all.
# Comparing the request instead makes the sentence above true of what the code
# does, and drops the 20% of slack that made a 200m request fail a 250m test.
#
# 100m / 128Mi is an order of magnitude above the sidecar the floor is aimed at
# while admitting the ordinary small service. It excludes what it was written
# for on the live fleet -- `cert-manager` and its webhook at 10m/32Mi, both
# ~99% idle and both unshrinkable in any way worth an engineer's attention --
# and admits every controller above that. Severity is unchanged, so the
# newly-reachable findings arrive as `minor` and a node's worth of waste still
# grades `major`.
OVERREQUEST_FLOOR_VCPU = 0.1
OVERREQUEST_FLOOR_GIB = 0.125

# The floor the SOP's remediation clamps a resized request to: `ceil(peak x 2)`
# per replica, never below `50m` / `64Mi`. This is a different quantity from the
# materiality floors above, which decide whether a request is large enough to be
# worth reporting; these decide what the new request would be.
#
# A controller already sitting at this floor on every dimension the excerpt calls
# idle has a recommendation identical to the request it already declares, so the
# finding proposes no change and no manifest edit closes it. The 2026-09-05 cost
# report carried three of them -- `hello-world` on `adam-new-cluster`,
# `adamparco-gitops` and `ap-ap-deploy-test`, each 50m/64Mi per replica, each
# graded `major` by the Autopilot bump, and each answered by the model with
# "already at the sizing floor; no manifest resize is possible". Three findings a
# week that the audit's own remediation rule proves unactionable teach a reader
# to scroll past the section they are in.
OVERREQUEST_RESIZE_FLOOR_VCPU = 0.05
OVERREQUEST_RESIZE_FLOOR_MIB = 64.0

# The headroom multiplier §3.1 sizes a resized request at.
OVERREQUEST_PEAK_MULTIPLIER = 2

# `idle-workload` takes the population the two floors above drop between them:
# a controller whose resize is a no-op on every dimension it declares. The
# comment on the resize floors is right that no manifest edit closes such a
# finding, and wrong that there is therefore nothing to say. A workload sitting
# at the smallest request the platform will admit, using a twentieth of it for
# a month, is not mis-sized -- it is unused, and the edit that reclaims it is
# deleting the file, which is a pull request the resize never was.
#
# The same bar as `check_overrequest`, on purpose. What separates the two
# checks is not how idle a workload is but whether anything can be done about
# it: 3.1 takes the shrinkable ones, this takes the rest. A second, stricter
# threshold here would be a number to defend with nothing deciding on it, and
# the first attempt at one -- 5%, read off a report that quoted peaks rather
# than ratios -- excluded all four workloads the check exists to find. Their
# memory sits at 6.6% to 14.7% of a 64Mi request, because 64Mi is the floor
# and a process that does nothing still maps a few MiB of libc.
#
# 3.1 applies it per dimension; this applies it to every dimension the
# controller declares, which is what makes the finding "nobody is using this"
# rather than "one dimension is oversized". On the live fleet that distinction
# is what excludes `cert-manager` (17% CPU but 95% memory), its webhook (7%
# and 68%) and `kube-agents-controller-manager` (44% and 55%) while admitting
# the four `hello-world`-class Deployments.
IDLE_WORKLOAD_UTILISATION = 0.2
# Long enough that a staging environment idle over a release freeze, or
# anything else with a duty cycle measured in days, has had a chance to run.
# Measured against the controller's own `creationTimestamp`, not its pods':
# GKE recreates a pod on every node upgrade, so the four Deployments this
# check finds have run untouched for 28 to 35 days behind pods 4.7 to 7.8 days
# old. Gating on the pod would have excluded all four -- and would have gone
# on excluding them, since nothing on a managed platform keeps a pod for a
# fortnight.
IDLE_WORKLOAD_MIN_AGE_DAYS = 14

# The `needs_triage` marker on an idle controller a Service selects. Read by
# `triage_markers` in audit_report.py, which withholds these from the
# automatic sweep -- so the string has to match the one that file names, and
# the two files carry it separately because neither imports the other.
IDLE_SERVICE_TRIAGE = "service-fronted"

# The `needs_triage` marker on a sizing finding whose `major` grade the
# Autopilot bump supplied: an overrequest that is `minor` by magnitude, and
# every unsized workload on an Autopilot cluster. The bump moves a finding up
# the ledger because Autopilot bills on requests; it says nothing about
# whether the resize is safe to merge unread. Neither check is on
# `MAJOR_SWEEP_CHECKS` in audit_report.py, so a `major` waits regardless; the
# marker names the reason in the ledger, and keeps a platform attribute from
# opening pull requests by itself if either check ever joins that list. Same contract as
# `IDLE_SERVICE_TRIAGE`: `NO_SWEEP_TRIAGE` names it, `/remediate` still opens it.
AUTOPILOT_BUMP_TRIAGE = "autopilot-bumped"

# The `needs_triage` marker on every §3.13 stand-down a Service does not
# select (one it does carries `IDLE_SERVICE_TRIAGE`, the more specific
# reason). The fix is `spec.replicas: 0`, and CPU and memory near zero for a
# week is not evidence nothing needs the workload, so the marker keeps that
# pull request out of the sweep at any grade.
IDLE_STANDDOWN_TRIAGE = "scale-to-zero"

# The `needs_triage` marker on a §3.1 overrequest of a `Guaranteed` pod. The
# SOP has the model publish it as `manual`, which the sweep never opens; the
# marker is also in `NO_SWEEP_TRIAGE`, so a `manifest` written anyway stays
# out of the sweep rather than resting on that instruction alone.
GUARANTEED_QOS_TRIAGE = "guaranteed-qos"

# `check_underrequest`'s floor is on the overage -- how far sustained usage sits
# above the request -- rather than on the request, because that overage is the
# quantity doing the harm: it is what the scheduler failed to book on the node,
# and it is the key kubelet sorts Burstable pods by when it picks one to evict.
# A ratio test alone would flag every pod a megabyte over its request.
#
# 128 MiB is where the two live examples separate. On the sixteen-cluster
# adamparco-kage fleet on 2026-09-05, exactly three pods held a mean above their
# request: both `litellm` replicas, each requesting 512Mi and averaging ~0.95 GiB
# for a ~980 MiB overage across the controller, and `cert-manager-cainjector`,
# requesting 32Mi and averaging 65Mi. The cainjector is genuinely mis-sized and
# genuinely not worth reporting -- 33 MiB of under-booking distorts no node's
# scheduling, and it is an upstream chart default no operator here owns.
UNDERREQUEST_FLOOR_MIB = 128.0
# §3.11's `critical` arm: a sustained mean at or above this fraction of an
# enforced memory limit is an OOMKill waiting for one more request.
UNDERREQUEST_NEAR_LIMIT_FRACTION = 0.9

# §3.11 sizes the new memory request at 1.3x the observed per-replica peak.
# 1.3 and not §3.1's 2x because sizing up costs bookable capacity on every node
# the controller lands on, and the mean already establishes that this is the
# steady state rather than a spike. The number lived only in the SOP and in a
# prose comment below, which left the collector unable to apply it and the
# model recomputing it from an excerpt rounded to two decimals of a GiB.
UNDERREQUEST_PEAK_MULTIPLIER = 1.3

# Where the new request lands above the container's declared limit, §3.11
# raises the limit with it, to twice the new request. A request above its limit
# is rejected at admission, so a remediation that raises one and leaves the
# other stops the workload scheduling. Observed on 2026-09-07: `litellm` on
# `kube-agents-host` was published with a 2153Mi per-replica prescription
# against a 2048Mi per-replica limit, and its recommendation said "limits and
# CPU request untouched".
UNDERREQUEST_LIMIT_MULTIPLIER = 2

# `check_unsized`'s floor is not a materiality test like the two above -- it does
# not decide whether to report, only what number to recommend. Everything this
# check finds is worth reporting whatever its size, because the cost is the
# scheduler booking zero rather than the request being wrong by some margin. But
# 2x a near-silent sidecar's peak is `1m`/`1Mi`, which no scheduler decision
# turns on and which leaves the pod as `Burstable`-in-name-only. On the live
# fleet `argocd-dex-server` peaks at 0.4 millicores; recommending `1m` would
# have been a manifest edit that changes nothing observable.
UNSIZED_FLOOR_VCPU = 0.01
UNSIZED_FLOOR_MIB = 32.0
# §3.7's "pools created < 7 days ago" exclusion.
IDLE_NODEPOOL_MIN_AGE_DAYS = 7
# §3.7's "workload requests <= 15% of allocatable", per node and per dimension.
IDLE_NODEPOOL_REQUEST_FRACTION = 0.15
# The GKE operation type whose start time is a node pool's creation time.
CREATE_NODE_POOL_OPERATION = "CREATE_NODE_POOL"
# GKE operation timestamps carry nanoseconds; `fromisoformat` takes six digits.
OPERATION_FRACTION_RE = re.compile(r"(\.\d{6})\d+")
# Decimal places a resize target keeps before it is ceiled to a whole unit.
RESIZE_CEIL_DIGITS = 6
# Relative slack when comparing a per-replica quotient against a request or
# floor; see `_clearly_exceeds`.
PER_REPLICA_RELATIVE_TOLERANCE = 1e-9

SYSTEM_NAMESPACES = frozenset(
    {
        "kube-system", "kube-public", "kube-node-lease", "gmp-system", "gmp-public", "gke-gmp-system",
        "cnrm-system", "configconnector-operator-system", "krmapihosting-system", "istio-system",
        "asm-system", "anthos-identity-service", "gatekeeper-system", "composer-system",
    }
)

# §3.2/§3.3 grade a volume `major` at this size, or on an SSD class. GKE's
# SSD-backed class is `premium-rwo`, which names no `ssd` at all.
LARGE_VOLUME_GIB = 100
SSD_STORAGE_CLASS_MARKERS = ("ssd", "extreme", "premium")

# §3.10: a namespace under an active GitOps sync is the controller's to delete.
CONFIG_SYNC_MARKER_PREFIX = "configsync.gke.io/"
GITOPS_SYNC_MARKER_PREFIXES = (CONFIG_SYNC_MARKER_PREFIX, "kustomize.toolkit.fluxcd.io/")
#: §3.10's "explicit ownership or retention annotation": an annotation whose
#: key's name part (after any `prefix/`) holds one of these words once split
#: on `-`, `_` and `.` -- `owner`, `team-owner`, `example.com/retain`,
#: `retention-days`. Someone has said who keeps the namespace, or for how long.
RETENTION_ANNOTATION_WORDS = ("owner", "retain", "retention")

POD_TERMINAL_PHASES = ("Succeeded", "Failed")
SAFE_TO_EVICT_ANNOTATION = "cluster-autoscaler.kubernetes.io/safe-to-evict"
# What the cluster autoscaler itself compares against: the exact strings
# `"true"` and `"false"`, untrimmed and case-sensitive. §3.8 states the rule
# in those literals, and `gke-cluster-autoscaler`'s
# `find-scale-down-blockers.sh` selects on `== "false"` / `!= "true"`. Folding
# case or accepting `strconv.ParseBool`'s other spellings inverts the
# autoscaler both ways: a pod annotated `"False"` was published as pinning a
# node the autoscaler drains, and a local-storage pod annotated `"True"` had
# its pin dropped although the autoscaler still honours it.
SAFE_TO_EVICT_TRUE = "true"
SAFE_TO_EVICT_FALSE = "false"
# The autoscaler's per-volume form of `safe-to-evict`: local volume names it may
# discard on eviction, split on the separator and compared untrimmed, as the
# autoscaler compares them. A pod whose every local volume is listed no longer
# pins its node under `--skip-nodes-with-local-storage`.
SAFE_TO_EVICT_LOCAL_VOLUMES_ANNOTATION = "cluster-autoscaler.kubernetes.io/safe-to-evict-local-volumes"
SAFE_TO_EVICT_LOCAL_VOLUMES_SEPARATOR = ","
# The volume sources the autoscaler's `isLocalVolume` counts: a `hostPath`, or
# an `emptyDir` not backed by memory (a `/dev/shm` tmpfs holds nothing that
# outlives the pod on the node's disk).
EMPTY_DIR_VOLUME = "emptyDir"
HOST_PATH_VOLUME = "hostPath"
EMPTY_DIR_MEMORY_MEDIUM = "Memory"
# §3.5: this many idle addresses in one project become one per-project roll-up
# finding rather than one finding each.
IDLE_ADDRESS_ROLLUP_MIN = 10
# §3.6: the Service a GKE-created forwarding rule fronts, read from the JSON
# object the service controller writes as its description; see
# `check_orphan_lb` for why both quote shapes are accepted.
SERVICE_NAME_DESCRIPTION_RE = re.compile(r"""kubernetes\.io/service-name["']?\s*:\s*["']?([\w.-]+/[\w.-]+)""")
# Resource-quantity parsing -- the request and limit strings in pod specs.
CPU_RE = re.compile(r"^(\d+(?:\.\d+)?)(m)?$")
# Both suffix families a resource.Quantity accepts: `512M` and `1G` are as
# common in manifests as `512Mi`, and a request the regex rejects drops the
# container out of §3.1 and §3.11 without a word.
MEM_RE = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|Pi|Ei|k|M|G|T|P|E)?$")
MILLICORES_PER_CORE = 1000.0
BYTES_PER_MIB = 1024.0 * 1024.0
BINARY_UNIT_STEP = 1024.0
DECIMAL_UNIT_STEP = 1000.0
BINARY_UNITS = ("Ki", "Mi", "Gi", "Ti", "Pi", "Ei")
DECIMAL_UNITS = ("k", "M", "G", "T", "P", "E")
MEM_UNIT_TO_MIB = {
    **{unit: BINARY_UNIT_STEP ** (power + 1) / BYTES_PER_MIB for power, unit in enumerate(BINARY_UNITS)},
    **{unit: DECIMAL_UNIT_STEP ** (power + 1) / BYTES_PER_MIB for power, unit in enumerate(DECIMAL_UNITS)},
}
MIB_PER_GIB = 1024.0

IMPACT = {
    "overrequest": "This controller reserves far more than it uses, so the scheduler and autoscaler size the cluster for capacity nothing needs.",
    "unsized-workload": "With no nonzero CPU or memory request, the scheduler books nothing for this controller: it lands on nodes that are already full, it is BestEffort so kubelet evicts it first under pressure, and the autoscaler cannot count it when sizing the cluster.",
    "underrequest": "Sustained memory use above the request means the scheduler has under-booked every node this controller lands on, and kubelet ranks Burstable pods for eviction by exactly this overage — so it is the first thing evicted when any workload on that node needs memory.",
    "orphan-pv": "The backing disk still exists and no claim can bind it -- capacity paid for and unusable.",
    "unconsumed-pvc": "Provisioned storage sits bound with nothing reading or writing it.",
    "unattached-disk": "A persistent disk bills continuously whether or not anything is attached to it.",
    "idle-address": "A reserved external IP bills continuously whether or not anything answers on it.",
    "orphan-lb": "An orphaned forwarding rule keeps a load balancer, and usually an external IP, alive for nothing.",
    "idle-nodepool": "Nodes reserved by a non-zero autoscaler floor sit idle instead of being reclaimed.",
    "scaledown-blocked": "An unevictable pod on an under-allocated node blocks both scale-down and security patching.",
    "terminal-pods": "Finished objects accumulate in etcd and slow every full API-server list.",
    "idle-namespace": "A namespace with no running workload still holds a load balancer or bound storage.",
    # "nobody is calling it" was in this sentence for eleven days and nothing in
    # the collector ever measured a call. On 2026-09-07 three findings carrying
    # it auto-promoted, merged unattended, and stood down three Deployments --
    # two of them behind forwarding rules that had metered 837,460 and 785,748
    # inbound packets over the same week the finding quoted. The traffic turned
    # out to be internet background scanning, so the claim was probably true in
    # substance; it was certainly unmeasured, and an audit that guesses right is
    # still an audit a reader cannot check. What this check measures is CPU and
    # memory, so that is all it now asserts.
    "idle-workload": f"This controller's peak usage stayed at or below {IDLE_WORKLOAD_UTILISATION:.0%} of its requests on every dimension it declares over the measured window the excerpt names, and no resize can give any of the reservation back -- it, and any load balancer in front of it, bill for a reservation nothing draws on. "
    "Whether anything is still calling it is a separate question this check does not settle: the excerpt gives what the forwarding rule metered, and packets are not sessions. The excerpt also says which of the three no-resize reasons applies.",
    "registry-no-cleanup": "Artifact Registry bills for every byte it holds and deletes nothing on its own, so a repository with no cleanup policy costs more every time CI pushes and never costs less.",
}

# `RECONCILING` is not a cluster you cannot read. GKE sets it while work is in
# progress on an otherwise-operational cluster -- a control-plane upgrade, a
# node-pool resize, a setting change -- and the API server stays up throughout.
# It is also transient and ordinary: any config change puts a cluster there for
# minutes, so an audit that happened to fire during one dropped that cluster
# with no check evaluated against it. On the sixteen-cluster fleet that cost
# the only cluster with more than one node pool, and so the only one where
# `idle-nodepool` and `scaledown-blocked` can run at all, because a
# `gcpPublicCidrsAccessEnabled` edit had left it reconciling.
#
# The cost of reading one is that node state may be mid-transition. That is
# worth accepting rather than noting as a `limitations` string: a limitation
# makes the run `partial`, a partial run closes no ledger, and on a fleet this
# size with GitOps driving it something is reconciling often enough to pin the
# stream partial for good. The checks that could see a half-finished node pool
# already exclude `SchedulingDisabled` nodes and pools under seven days old,
# and a finding that was only ever transient disappears from next week's run.
#
# `PROVISIONING` has no API server yet and `STOPPING` is on its way out; both
# stay recorded rather than audited. A read that fails anyway still lands on
# the existing per-cluster `unreachable`/`gate-failed` path.
AUDITABLE_STATUSES = frozenset({"RUNNING", "RECONCILING"})
# The markers a reconciling controller stamps on an object it owns. Duplicated
# from `collect.reconciler_of` for the reason `declaration_for` below is: these
# collectors are standalone scripts, and none imports a sibling collector at
# module level.
#
# Helm 3 writes both halves of its pair on every object in a release, and the
# release namespace is not the object's. Argo CD's tracking id is
# `<application>:<group>/<Kind>:<namespace>/<name>`. `app.kubernetes.io/managed-by`
# is the fallback and only the fallback: Helm sets it to the literal `Helm`,
# which names no release.
_HELM_RELEASE_ANNOTATION = "meta.helm.sh/release-name"
_HELM_NAMESPACE_ANNOTATION = "meta.helm.sh/release-namespace"
_ARGOCD_TRACKING_ANNOTATION = "argocd.argoproj.io/tracking-id"
_MANAGED_BY_LABEL = "app.kubernetes.io/managed-by"
_HELM_MANAGED_BY = "helm"
SECONDS_PER_DAY = 86400.0
HOURS_PER_DAY = 24
BACKUP_ANNOTATION_PREFIXES = ("velero.io/", "gke.io/backup-")
ORPHAN_PV_RELEASED_DAYS = 7
ORPHAN_PV_UNCLAIMED_DAYS = 30
# Spans a deploy/rollback cycle and two runs of this weekly audit (SOP §3.3).
UNCONSUMED_PVC_MIN_AGE_DAYS = 14
MACHINE_TYPE_VCPU_RE = re.compile(r"^[a-z0-9]+-(?:standard|highmem|highcpu|megamem|ultramem|hypermem)-(\d+)(?:-\w+)?$")
# `-ext` is GCE's extended-memory custom form, `n2-custom-8-65536-ext`, and the
# only suffix a custom type takes. Shared-core custom E2 types
# (`e2-custom-medium-4096`) name no vCPU count and are left unparsed.
CUSTOM_MACHINE_TYPE_VCPU_RE = re.compile(r"^(?:[a-z0-9]+-)?custom-(\d+)-\d+(?:-ext)?$")
# `a2-highgpu-1g`, `a2-ultragpu-8g`, `a3-megagpu-8g`, `ct5lp-hightpu-4t`. The
# trailing number on an accelerator machine type counts GPUs or TPU chips, not
# vCPUs, so the family cannot go in the pattern above: `highgpu` sat in it and
# never matched anything, because the suffix is `8g` rather than `8`. Reading
# the digit as a vCPU count would be worse than not matching -- `a3-highgpu-8g`
# is a 208-vCPU machine and would have scored 8. Every member of these families
# is far past the 8-vCPU line §3.7's severity rule draws, so they are answered
# directly. `config.accelerators` covers most of them anyway, but not TPU pools:
# a TPU node pool carries its topology, not an `accelerators` list, and §3.7 is
# explicit that "an idle accelerator pool with a non-zero floor is the single
# largest reclaimable item this audit can find".
#
# TPU v6e (`ct6e-standard-4t`) and TPU7x (`tpu7x-standard-4t`) name their
# chips under `standard` instead, with a `t` suffix no vCPU-counted type
# carries, so the second alternative admits exactly that shape.
ACCELERATOR_MACHINE_RE = re.compile(
    r"^[a-z0-9]+-(?:(?:high|ultra|mega)(?:gpu|tpu)-\d+[a-z]?|standard-\d+t)$"
)
# §3.7's severity legs: a pool this many nodes large, or of this machine size.
IDLE_NODEPOOL_MAJOR_NODES = 3
BIG_MACHINE_VCPUS = 8
# How many drain-blocking pods the idle-pool excerpt names before it elides.
BLOCKERS_NAMED = 3
GC_OWNED_LABEL_PREFIXES = ("workflows.argoproj.io/", "tekton.dev/", "fluxcd.io/")
#: A namespace holding this many terminal pods is flagged whatever their age
#: (SOP §3.9).
TERMINAL_PODS_PILE = 50
#: How long a terminal pod or a finished Job is left before it is waste.
TERMINAL_MIN_AGE_DAYS = 7
#: `major` past these: etcd object growth and API-server list latency.
TERMINAL_PODS_MAJOR_NS = 500
TERMINAL_PODS_MAJOR_TOTAL = 2000
#: A CronJob keeping more finished Jobs than this is flagged itself.
CRONJOB_HISTORY_LIMIT_MAX = 10
#: The Job conditions that mean "this Job is over". `SuccessCriteriaMet` is
#: what a Job with a `successPolicy` gets instead of `Complete`.
JOB_TERMINAL_CONDITIONS = ("Complete", "Failed", "SuccessCriteriaMet")
# A monthly release cycle, and a pre-provisioned environment awaiting its first
# deploy (SOP §3.10).
IDLE_NAMESPACE_MIN_AGE_DAYS = 30
# §3.1's `major`: a reclaimable delta of a node's worth.
NODE_WORTH_VCPU = 8
NODE_WORTH_GIB = 32
# The `restartPolicy` that makes an init container a native sidecar, which
# runs beside the app containers and so counts in the pod's effective request.
SIDECAR_RESTART_POLICY = "Always"
LB_ANNOTATION_KEYS = ("kubernetes.io/ingress.global-static-ip-name", "networking.gke.io/load-balancer-ip", "cloud.google.com/load-balancer-ip", "networking.gke.io/addresses")
NON_WASTE_ADDRESS_PURPOSES = {"GCE_ENDPOINT", "VPC_PEERING", "PRIVATE_SERVICE_CONNECT", "NAT_AUTO", "SHARED_LOADBALANCER_VIP", "IPSEC_INTERCONNECT"}
#: §3.4's floor for a disk whose owning cluster is still there. The SOP
#: justifies it as outliving "node upgrades, pod rescheduling, and maintenance
#: windows" -- a full monthly GKE maintenance cycle, after which a reattach is
#: churn nobody should be paged about.
UNATTACHED_AGE_DAYS = 30
#: §3.4's `major` size, the SOP's ">=500 GiB of storage" magnitude.
UNATTACHED_DISK_MAJOR_GB = 500
#: §3.4's floor for a disk labelled for a cluster the project no longer runs.
#: Every clause of the 30-day justification is about something reattaching the
#: disk, and a deleted cluster reattaches nothing: there is no node pool to
#: upgrade, no scheduler to move a pod, no maintenance window. What is left to
#: outlive is the deletion itself -- GKE tears a cluster's PD-CSI volumes down
#: asynchronously, and a `Delete`-policy disk can outlive its cluster by
#: minutes -- plus the case where the same cluster is being recreated under the
#: same name in the same sitting. A week covers both and still catches the
#: waste three weeks before the 30-day floor would.
DEAD_CLUSTER_AGE_DAYS = 7
#: The label GKE stamps on every PD it provisions, naming the owning cluster.
GKE_CLUSTER_LABEL = "goog-k8s-cluster-name"
# §3.4's managed-service exclusions.
MANAGED_DISK_LABEL_PREFIXES = ("goog-composer", "goog-dataproc")
GKE_NODE_DISK_LABEL = "goog-gke-node"
#: The key PD-CSI writes into a provisioned disk's `description`, whose value is
#: a JSON object naming the PersistentVolumeClaim the disk was cut for.
CSI_DESCRIPTION_MARKER = "kubernetes.io/created-for"
#: GKE names the disks it creates for a cluster `gke-<cluster>-...`: §3.4's
#: second attribution rung, after the label.
GKE_DISK_NAME_PREFIX = "gke-"
GKE_DISK_NAME_SEPARATOR = "-"
# A copy of collect.py's pair: the two annotations GKE accepts for "give me an
# internal load balancer", the second the legacy spelling it still honours.
INTERNAL_LB_ANNOTATIONS = (
    "networking.gke.io/load-balancer-type",
    "cloud.google.com/load-balancer-type",
)
INTERNAL_LB_ANNOTATION_VALUE = "Internal"
#: How many of a roll-up's members the excerpt names before it stops. Enough to
#: start on without opening the console; short of the point where one finding's
#: evidence crowds the rest of the ledger out of the 60,000-character body §5
#: warns about.
ROLLUP_EXCERPT_MEMBERS = 12
# Two weeks outlasts a typical cutover window (SOP §3.5).
IDLE_ADDRESS_MIN_AGE_DAYS = 14
#: Below this, a repository is not worth a finding whatever its policy. A
#: registry is one of the few GCP resources whose cost is *only* size, so the
#: threshold is the bill: 50 GiB is about $5/month of Artifact Registry storage,
#: which is the order §3.5 already treats as `minor`, and a finding that
#: recommends work costing more than it saves is noise. Nothing is lost
#: permanently by setting it here rather than lower -- a repository with no
#: policy only grows, so a smaller one crosses the floor and reports later.
REGISTRY_SIZE_FLOOR_BYTES = 50 * 1024**3
#: A repository is billed for what it holds, so the size that matters is what it
#: will hold, not what it holds now. `major` needs a repository already large
#: enough that a cleanup pass returns real money.
REGISTRY_SIZE_MAJOR_BYTES = 500 * 1024**3
#: Or growing fast enough to get there. Average daily growth is `sizeBytes` over
#: the repository's age -- the only growth figure a single `list` can produce,
#: and enough to separate a repository that filled up once from one a CI job is
#: still pushing to. 2 GiB/day is 730 GiB a year.
REGISTRY_GROWTH_MAJOR_BYTES_PER_DAY = 2 * 1024**3
#: A repository younger than this has no history to clean. Its average-growth
#: figure is also noise -- one day of pushes divided by one day of age reads as
#: a runaway.
REGISTRY_MIN_AGE_DAYS = 30
#: Only a repository that stores its own artifacts. A `REMOTE_REPOSITORY` is a
#: pull-through cache whose contents GCP evicts on its own schedule, and a
#: `VIRTUAL_REPOSITORY` stores nothing at all -- it is a view over others, and
#: `sizeBytes` on one double-counts the upstreams this check already reads.
REGISTRY_BILLED_MODE = "STANDARD_REPOSITORY"
BYTES_PER_GIB = 1024**3
# The service controller tears LB resources down within minutes of the
# Service; a week is far past that (SOP §3.6).
ORPHAN_LB_MIN_AGE_DAYS = 7
# The path segment of a forwarding rule's `target` that names a Private
# Service Connect service attachment.
SERVICE_ATTACHMENT_PATH = "/serviceAttachments/"
# Every internal `loadBalancingScheme` starts with this: `INTERNAL`,
# `INTERNAL_MANAGED`, `INTERNAL_SELF_MANAGED`.
INTERNAL_SCHEME_PREFIX = "INTERNAL"


def _is_system_namespace(ns: str) -> bool:
    return ns in SYSTEM_NAMESPACES or ns.startswith("gke-") or ns.startswith("config-management-")


def log(msg: str) -> None:
    print(f"[fleet_waste] {msg}", file=sys.stderr, flush=True)


class Run(NamedTuple):
    argv: list[str]
    rc: int
    stdout: str
    stderr: str
    duration_s: float


RunFn = Callable[..., Run]
# Anything with a `requests`-shaped `.get(url, params=..., timeout=...)`. Kept
# structural rather than typed to `AuthorizedSession` so tests can hand in a
# stub without importing google.auth, which is not a test-time dependency.
SessionFn = Any


def _text(output: str | bytes | None) -> str:
    return output.decode(errors="replace") if isinstance(output, bytes) else (output or "")


def default_run(argv: list[str], *, env: dict | None = None, timeout: int = DEFAULT_TIMEOUT_S) -> Run:

    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout)
        return Run(argv, proc.returncode, proc.stdout, proc.stderr, time.monotonic() - t0)
    except subprocess.TimeoutExpired as exc:
        # `TimeoutExpired` carries whatever the child wrote as bytes, `text=True`
        # notwithstanding, and every consumer of `Run` searches and slices it as
        # str. `collect.py`'s `_text` does the same.
        return Run(argv, TIMEOUT_RC, _text(exc.stdout), _text(exc.stderr), time.monotonic() - t0)
    except Exception as exc:
        return Run(argv, -1, "", str(exc), time.monotonic() - t0)


def run_and_gate(argv: list[str], *, run: RunFn, env: dict | None = None) -> tuple[object | None, Run]:
    result = run(argv, env=env)
    if result.rc != 0 or not result.stdout.strip():
        return None, result
    try:
        return json.loads(result.stdout), result
    except json.JSONDecodeError:
        return None, result


def object_list(parsed: object) -> list | None:
    """`parsed` when it is a JSON list of objects, else `None`.

    What every `gcloud ... list` read here has to be before a check iterates
    it. rc 0 with any other shape is a read that did not answer: an empty
    object iterates to nothing and would record the check as run clean, and
    a string element crashes the first `.get`.
    """
    if isinstance(parsed, list) and all(isinstance(item, dict) for item in parsed):
        return parsed
    return None


def output_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record(argv_str: str, result: Run) -> dict:
    return {
        "command": argv_str,
        "rc": result.rc,
        "duration_s": round(result.duration_s, 2),
        "output_sha256": output_digest(result.stdout),
    }


def kubeconfig_path(project: str, cluster: str, location: str) -> Path:
    return KUBECONFIG_DIR / f"kubeconfig_{project}_{cluster}_{location}.yaml"


def fetch_credentials(project: str, cluster: str, location: str, *, run: RunFn) -> tuple[Path, Run]:
    kc = kubeconfig_path(project, cluster, location)
    kc.parent.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "KUBECONFIG": str(kc)}
    result = run(
        ["gcloud", "container", "clusters", "get-credentials", cluster, "--location", location, "--project", project],
        env=env,
    )
    return kc, result


def target_name(project: str, location: str, name: str) -> str:
    """`<project>/<location>/<name>`, as `collect.target_name` spells it.

    Every cluster is qualified, not only one that collides today: a name
    qualified only on collision moves when the rest of the fleet changes, and
    a finding's id moves with it. A candidate's `object` stays the bare
    resource, and the GitOps tree is still keyed by the bare name.
    """
    return QUALIFIED_TARGET_SEPARATOR.join([p for p in (project, location) if p] + [name])


class NoProjectInScope(Exception):
    """Discovery named no project at all, which is not a fleet of empty projects."""


def get_target_projects(cli_project: str | None, *, run: RunFn) -> tuple[list[str], str | None]:
    """§1's project scope: "every project the agent can see". A `--project`
    override skips discovery entirely, for a scoped or a test run; otherwise
    this names the active project plus every other listed project, and lists
    none of them -- `collect.py`'s `discover_fleet` takes the same scope.

    A listed project holding no cluster stays in scope on purpose. §3.4-§3.6
    and §3.14 look for disks, addresses, forwarding rules and repositories,
    and a project whose last cluster was deleted is exactly where those are
    left behind; dropped here, it left no `project/<p>` row, and `finish`
    read a finding filed there on an earlier run as resolved. Probing each
    candidate with `clusters list` to decide was also a serial read per
    listed project before any worker started, which `collect_fleet` now does
    once, in its pool.

    The second value is set when the scope is provably short of the fleet --
    `--project` skipped discovery, `gcloud projects list` failed, or it
    answered without naming the active project -- and
    `collect_fleet` turns it into an `UNENUMERATED_PROJECTS_TARGET` entry, so
    the loss is a row the document has to account for rather than a fleet that
    silently shrank to one project.

    Raises `NoProjectInScope` when there is no active project and the listing
    failed or named none: that credential sees nothing, which is not a fleet."""
    if cli_project:
        return [cli_project], SCOPED_RUN_NOTE.format(project=cli_project)

    result = run(["gcloud", "config", "get-value", "project"])
    base = result.stdout.strip() if result.rc == 0 else ""
    projects = [base] if base else []

    list_result = run(["gcloud", "projects", "list", "--format", "value(projectId)"], timeout=PROJECTS_LIST_TIMEOUT_S)
    if list_result.rc != 0:
        stderr = list_result.stderr.strip()[:ERROR_EXCERPT_CHARS] or "no stderr"
        if not base:
            # `collect.py`'s `discover_fleet` answers the same input the same way.
            raise NoProjectInScope(
                f"project discovery failed: `gcloud config get-value project` rc={result.rc} "
                f"named no project and `gcloud projects list` rc={list_result.rc}: {stderr}"
            )
        partial = (
            f"`gcloud projects list` rc={list_result.rc}: {stderr}. The scope fell back to "
            f"the active project {base!r}; how many other projects the fleet holds is unknown."
        )
        log(f"WARNING: {partial}")
        return projects, partial

    listed = [p.strip() for p in (list_result.stdout or "").splitlines() if p.strip()]
    candidates = [p for p in listed if p != base]
    if not base and not candidates:
        raise NoProjectInScope(NO_PROJECT_IN_SCOPE_ERROR)
    projects.extend(candidates)
    if base and base not in listed:
        # rc 0 and the active project absent from its own output: the listing
        # is filtered rather than complete, so the scope is provably short.
        # `collect.py`'s `discover_fleet` answers the same input the same way.
        partial = (
            f"{FILTERED_LISTING_NOTE} {base!r}, "
            f"so it is filtered rather than complete: it returned {len(listed)} "
            "project(s) and this run reads clusters in one it did not return. How "
            "many other projects the fleet holds is unknown."
        )
        log(f"WARNING: {partial}")
        return projects, partial
    return projects, None


def not_running_entry(c: dict, project: str) -> dict:
    """A manifest target for a cluster whose state rules out auditing it.

    Filtering `clusters list` down to `AUDITABLE_STATUSES` is right -- a
    PROVISIONING cluster has no API server to read. Dropping the rest without a
    trace is not: the manifest is the run's only account of the fleet it saw, so
    a cluster absent from it reads exactly like a cluster that does not exist,
    and the document can publish a fleet-wide all-clear over a fleet quietly
    missing it. DEGRADED is the case that makes this bite. Recorded as a
    non-`collected` target, the loss is something the document has to place in
    `scope.skipped` with a reason. `collect.py` carries the same helper.
    """
    location = c.get("location") or c.get("zone") or ""
    return {
        "name": target_name(project, location, c.get("name", "")),
        "project": project,
        "location": location,
        "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
        "outcome": UNREACHABLE_OUTCOME,
        "error": f"cluster status is {c.get('status') or 'unknown'}, which is neither RUNNING nor RECONCILING; no check was evaluated against it",
    }


class IncompleteEnumeration(RuntimeError):
    """`clusters list` answered, but a zone did not respond.

    Carries the clusters that did arrive, so they are still audited, while
    the project's own target reports the enumeration as incomplete: its
    checks compare against the project's whole cluster list, and a silent
    zone's clusters are missing from it. A caller that catches only
    `RuntimeError` still gets the conservative answer, the project unread."""

    def __init__(self, message: str, running: list[dict], not_running: list[dict]):
        super().__init__(message)
        self.running = running
        self.not_running = not_running


def refusal_names_project(project: str, stderr: str, *, run: RunFn) -> bool:
    """Whether an API-disabled refusal is `project`'s own. Only then is it the
    answer "no cluster can exist here": a refusal from a quota project with the
    API off names that project instead, and read as this one's it marked every
    project cluster-free, so `finish` closed an audit that read no cluster. A
    refusal naming no project, or one this project's number cannot be read
    for, is a failed read."""
    return refusal_owner(project, stderr, run=run)[0]


def refusal_owner(project: str, stderr: str, *, run: RunFn) -> tuple[bool, str]:
    """`refusal_names_project`'s answer, with why when it is no: which project
    the refusal named, or that this project's number could not be read. The
    two call for different fixes -- a quota-project setting, or the describe
    permission -- and collapsing them sent the operator after the wrong one."""
    numbers = set(REFUSED_PROJECT_NUMBER_RE.findall(stderr))
    if not numbers:
        # The keyword case-insensitively, as `REFUSED_PROJECT_ID_RE` reads it:
        # `Project acme` names acme as surely as `project acme` does.
        if re.search(rf"\b(?i:projects?)[ /]['\"\[]?{re.escape(project)}(?![\w-])", stderr):
            return True, ""
        others = sorted(set(REFUSED_PROJECT_ID_RE.findall(stderr)) - REFUSED_PROJECT_ID_STOPWORDS - {project})
        if others:
            return False, f"the refusal names another project ({', '.join(map(repr, others))}), so it cannot be tied to {project!r}"
        return False, f"the refusal names no project, so it cannot be tied to {project!r}"
    described = run([*PROJECT_DESCRIBE_ARGV, project, "--format", "value(projectNumber)"])
    if described.rc != 0:
        return False, (
            f"`gcloud projects describe {project}` failed (rc={described.rc}), so the refusal's project "
            f"number could not be compared with this project's: "
            f"{described.stderr.strip()[:ERROR_EXCERPT_CHARS] or 'no stderr'}"
        )
    if numbers == {described.stdout.strip()}:
        return True, ""
    return False, (
        f"the Kubernetes Engine API is off in a project other than {project!r}, such as a quota project"
    )


def _describing_once(run: RunFn) -> RunFn:
    """`run`, answering a repeated `gcloud projects describe` from its first answer.

    A project with the Kubernetes Engine, Compute Engine and Artifact Registry
    APIs all off is asked for its number three times -- once per refusal --
    and an organisation-wide credential lists many such projects inside one
    project-read budget. The number does not change within a run, and the
    cache lives only as long as the `collect_fleet` call that made it. Two
    threads racing on one project can both ask; that costs a call, not a
    wrong answer.
    """
    answers: dict[tuple[str, ...], Run] = {}

    def wrapped(argv: list[str], **kwargs) -> Run:
        if argv[: len(PROJECT_DESCRIBE_ARGV)] != PROJECT_DESCRIBE_ARGV:
            return run(argv, **kwargs)
        key = tuple(argv)
        if key not in answers:
            answers[key] = run(argv, **kwargs)
        return answers[key]

    return wrapped


def enumerate_clusters(project: str, *, run: RunFn) -> tuple[list[dict], list[dict]]:
    result = run(
        ["gcloud", "container", "clusters", "list", "--project", project, "--format", "json(name,location,status,autopilot.enabled,createTime)"]
    )
    if result.rc != 0:
        # Discovery lists no project, so this is where a project whose
        # Kubernetes Engine API is off first answers: with no cluster.
        if any(marker in result.stderr for marker in API_DISABLED_MARKERS):
            ours, why_not = refusal_owner(project, result.stderr, run=run)
            if ours:
                log(f"{project}: Kubernetes Engine API is not enabled; no cluster can exist here")
                return [], []
            raise RuntimeError(
                f"cluster enumeration refused (rc={result.rc}) and {why_not}, so this project's clusters are "
                f"unknown: {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}"
            )
        raise RuntimeError(f"cluster enumeration failed (rc={result.rc}): {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}")
    try:
        clusters = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cluster enumeration returned no parseable JSON: {exc}") from exc
    # Not only a crash guard: an rc-0 `{}` iterates nothing and would record
    # the project as holding no cluster.
    if not isinstance(clusters, list) or not all(isinstance(c, dict) for c in clusters):
        raise RuntimeError("cluster enumeration returned JSON that is not a list of clusters")
    running = [
        {
            "name": c["name"],
            "location": c.get("location"),
            "project": project,
            "autopilot": bool((c.get("autopilot") or {}).get("enabled")),
            "create_time": c.get("createTime") or "",
        }
        for c in clusters
        if c.get("status") in AUDITABLE_STATUSES
    ]
    not_running = [not_running_entry(c, project) for c in clusters if c.get("status") not in AUDITABLE_STATUSES]
    incomplete = [line.strip() for line in result.stderr.splitlines() if ZONE_TIMEOUT_MARKER in line]
    if incomplete:
        detail = " ".join(incomplete)[:ERROR_EXCERPT_CHARS]
        log(f"{project}: clusters list returned {len(clusters)} cluster(s) but is incomplete: {detail}")
        raise IncompleteEnumeration(f"clusters list rc=0 but incomplete: {detail}", running, not_running)
    return running, not_running


# --------------------------------------------------------------------------- #
# Resource-quantity parsing — the request and limit strings in pod specs.
# --------------------------------------------------------------------------- #


def parse_cpu_cores(s: str) -> float | None:
    m = CPU_RE.match((s or "").strip())
    if not m:
        return None
    value, unit = m.groups()
    return float(value) / MILLICORES_PER_CORE if unit == "m" else float(value)


def parse_mem_mib(s: str) -> float | None:
    m = MEM_RE.match((s or "").strip())
    if not m:
        return None
    value, unit = m.groups()
    if unit is None:
        # A Kubernetes resource.Quantity with no suffix is a byte count
        # (e.g. a container's `resources.requests.memory: "134217728"`),
        # never MiB -- treating it as already-MiB overstates a bare-byte
        # request by a factor of 2^20.
        return float(value) / BYTES_PER_MIB
    return float(value) * MEM_UNIT_TO_MIB[unit]


def default_monitoring_session() -> SessionFn:
    """A session for the Monitoring read API, on whichever credential this pod has.

    In the shell sandbox -- where the agent's code runs on a stock install --
    the pod holds no Google identity, so the read goes through the credential
    broker's relay (`credential_proxy_client.ApiSession`): the broker checks it
    against its table of permitted reads, attaches its own credential, and
    hands back the upstream response. `CREDENTIAL_PROXY_URL` is what marks that
    pod. Anywhere else (an unsandboxed install, a workstation) the pod's own
    ADC is the credential, imported lazily because `google.auth` is not a
    test-time dependency.

    Either way this is the reason the usage read does not go through `run`.
    The credential proxy that fronts `gcloud` refuses `auth print-access-token`
    outright (policy rule `gcp.access-token-disclosure`), and rightly so -- a
    token printed to stdout is a token in the model's context. Neither path
    materializes one.
    """
    if os.environ.get(CREDENTIAL_PROXY_URL_ENV):
        for directory in SHARED_SCRIPT_DIRS:
            if directory not in sys.path:
                sys.path.append(directory)
        import credential_proxy_client

        return credential_proxy_client.ApiSession()

    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(scopes=[MONITORING_SCOPE])
    return AuthorizedSession(credentials)


def _point_value(point: dict) -> float | None:
    value = point.get("value") or {}
    if value.get("doubleValue") is not None:
        return float(value["doubleValue"])
    if value.get("int64Value") is not None:
        return float(value["int64Value"])
    return None


def _cluster_filter(cluster: str, location: str | None) -> str:
    """The resource-label clause naming one cluster. A name is unique only per
    location, so without `location` two same-named clusters in one project have
    their pods' series summed together by the caller's `REDUCE_SUM`."""
    clause = f'resource.labels.cluster_name="{cluster}"'
    return f'{clause} AND resource.labels.location="{location}"' if location else clause


def _monitoring_command(project: str, requests: list[dict]) -> str:
    """The manifest's stand-in for the argv of a Cloud Monitoring read.

    The read is an HTTPS GET with no argv of its own, and `finish` accepts a
    `checks_run` command only if it names an inspection binary, so the label
    is the `curl` that issues the same requests outside the sandbox, one per
    metric, each with every parameter the collector sent but the page size.
    A list value repeats its key, as `requests` encodes it.
    """
    url = MONITORING_TIMESERIES_URL.format(project=project)
    curls = []
    for params in requests:
        # A pair with nothing to encode goes as the short `-d`, which `-G`
        # appends unchanged: the label has to fit `finish`'s per-command
        # limit, and the load-balancer read's three queries at the longest
        # project name did not.
        args = [
            f"-d {pair}" if URL_SAFE_PAIR_RE.match(pair) else f"--data-urlencode {shlex.quote(pair)}"
            for key, value in params.items()
            for item in (value if isinstance(value, list) else [value])
            for pair in [f"{key}={item}"]
        ]
        curls.append(" ".join([MONITORING_CURL_PREFIX, url, *args]))
    return " && ".join(curls)


def _pod_series_params(
    metric: str, cluster: str, location: str | None, *, start: datetime, now: datetime,
    primary: str, secondary: str, window_hours: int,
) -> dict:
    """The query `_read_pod_series` sends, less paging; its label renders the same dict."""
    return {
        "filter": f'metric.type="{metric}" AND {_cluster_filter(cluster, location)}' + (MEM_NON_EVICTABLE_FILTER if metric == MEM_METRIC else ""),
        "interval.startTime": start.strftime(MONITORING_TIME_FORMAT),
        "interval.endTime": now.strftime(MONITORING_TIME_FORMAT),
        "aggregation.alignmentPeriod": f"{USAGE_ALIGNMENT_S}s",
        "aggregation.perSeriesAligner": primary,
        "aggregation.crossSeriesReducer": REDUCE_SUM,
        "aggregation.groupByFields": POD_GROUP_BY_FIELDS,
        "secondaryAggregation.alignmentPeriod": f"{window_hours * SECONDS_PER_HOUR}s",
        "secondaryAggregation.perSeriesAligner": secondary,
    }


def _lb_traffic_params(metric: str, *, start: datetime, now: datetime) -> dict:
    """The query `_read_lb_series` sends, less paging; its label renders the same dict."""
    return {
        "filter": f'metric.type="{metric}"',
        "interval.startTime": start.strftime(MONITORING_TIME_FORMAT),
        "interval.endTime": now.strftime(MONITORING_TIME_FORMAT),
        "aggregation.alignmentPeriod": f"{LB_TRAFFIC_ALIGNMENT_S}s",
        "aggregation.perSeriesAligner": ALIGN_SUM,
        "aggregation.crossSeriesReducer": REDUCE_SUM,
        "aggregation.groupByFields": [LB_RULE_LABEL, LB_REGION_LABEL],
    }


def _json_body(response) -> dict | None:
    """The response's JSON object, or None when the body is not one.

    Raised inside the caller, the decode error escaped the read: under
    `_read_project` it cost the project every cluster, under `collect_cluster`
    every object-state check, for a metric the caller already treats as
    optional.
    """
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _read_pod_series(
    session: SessionFn,
    url: str,
    *,
    metric: str,
    cluster: str,
    location: str | None,
    start: datetime,
    now: datetime,
    primary: str,
    secondary: str,
    window_hours: int,
) -> tuple[dict[tuple[str, str], float], tuple[int, str] | None]:
    """One metric, summed to the pod and collapsed to a single number per pod.

    Returns `(series, None)` or `({}, (rc, message))`; the caller decides what a
    failure means, because `fetch_usage_peaks` treats it as §2's metrics
    degradation and `fetch_memory_means` treats it as one check going quiet.

    `secondary` is the collapse: `ALIGN_MAX` gives the peak over the window and
    `ALIGN_MEAN` the sustained level. The percentile aligners would be the
    better middle ground and are not available -- both of these metrics are
    GAUGE-valued, and `ALIGN_PERCENTILE_*` requires a DISTRIBUTION, so asking
    for one returns HTTP 400 rather than a degraded answer.
    """
    sink: dict[tuple[str, str], float] = {}
    page_token = None
    while True:
        params = {
            **_pod_series_params(
                metric, cluster, location, start=start, now=now,
                primary=primary, secondary=secondary, window_hours=window_hours,
            ),
            "pageSize": MONITORING_PAGE_SIZE,
        }
        if page_token:
            params["pageToken"] = page_token
        try:
            response = session.get(url, params=params, timeout=MONITORING_TIMEOUT_S)
        except Exception as exc:
            return {}, (-1, f"{metric}: {type(exc).__name__}: {exc}")
        if response.status_code != HTTP_OK:
            return {}, (response.status_code, f"{metric}: {response.text}")
        body = _json_body(response)
        if body is None:
            return {}, (-1, f"{metric}: {NON_JSON_BODY}")
        for series in body.get("timeSeries") or []:
            labels = (series.get("resource") or {}).get("labels") or {}
            pod_key = (labels.get("namespace_name", ""), labels.get("pod_name", ""))
            for point in series.get("points") or []:
                value = _point_value(point)
                if value is not None:
                    # `max` across points, not across the window: the secondary
                    # aggregation already collapsed the window to one point per
                    # series, so this only folds together the several series a
                    # pod can produce (its containers, and memory's
                    # `memory_type` breakdown) that `REDUCE_SUM` did not.
                    sink[pod_key] = max(sink.get(pod_key, 0.0), value)
        page_token = body.get("nextPageToken")
        if not page_token:
            return sink, None


def fetch_usage_peaks(
    project: str,
    cluster: str,
    *,
    location: str | None = None,
    session: SessionFn,
    now: datetime,
    window_hours: int = USAGE_WINDOW_HOURS,
) -> tuple[dict[tuple[str, str], tuple[float | None, float | None]], bool, Run]:
    """§2's usage figures, read from Cloud Monitoring rather than sampled.

    Returns `(peaks, available, result)`. `peaks` maps `(namespace, pod)` to
    that pod's `(peak_cpu_cores, peak_mem_mib)` over the trailing
    `window_hours` -- deliberately the same key and the same two units the
    `kubectl top pods` parse produced, so `check_overrequest` reads it
    unchanged. A dimension the pod has no series for is `None`.

    `available=False` is §2's metrics degradation and reaches the manifest as
    a limitation rather than a silent zero. It covers the empty answer as well
    as the failed one: a 200 carrying no time series means this cluster is not
    shipping system metrics, and treating that as "usage was zero" would read
    every workload on it as pure waste.

    Both metrics are per-*container*. `REDUCE_SUM` over
    `(namespace_name, pod_name)` adds a pod's containers back together, so the
    figure is comparable to the pod's summed requests. Memory is read for
    `memory_type="non-evictable"` only: page cache is not what a request has
    to hold.
    """
    started = time.monotonic()
    start = now - timedelta(hours=window_hours)
    url = MONITORING_TIMESERIES_URL.format(project=project)
    reads = ((CPU_METRIC, ALIGN_RATE, CPU_USAGE_KEY), (MEM_METRIC, ALIGN_MAX, MEM_USAGE_KEY))
    label = _monitoring_command(project, [
        _pod_series_params(
            metric, cluster, location, start=start, now=now,
            primary=aligner, secondary=ALIGN_MAX, window_hours=window_hours,
        )
        for metric, aligner, _ in reads
    ])

    def fail(rc: int, message: str) -> tuple[dict, bool, Run]:
        return {}, False, Run([label], rc, "", message[:ERROR_EXCERPT_CHARS], time.monotonic() - started)

    if session is None:
        return fail(-1, NO_SESSION_MESSAGE)

    peaks: dict[str, dict[tuple[str, str], float]] = {CPU_USAGE_KEY: {}, MEM_USAGE_KEY: {}}
    for metric, aligner, key in reads:
        sink, err = _read_pod_series(
            session, url, metric=metric, cluster=cluster, location=location, start=start, now=now,
            primary=aligner, secondary=ALIGN_MAX, window_hours=window_hours,
        )
        if err is not None:
            return fail(*err)
        peaks[key] = sink

    # A pod with a series under one metric and none under the other is
    # unmeasured on that dimension, not idle on it: `None`, which
    # `_per_replica` keeps apart from zero. Read as `0.0`, the missing
    # dimension cleared §3.1's 20% bar by the widest margin available and the
    # finding proposed shrinking a request nothing had measured.
    mem_mib = {pod_key: value / BYTES_PER_MIB for pod_key, value in peaks[MEM_USAGE_KEY].items()}
    merged = {
        pod_key: (peaks[CPU_USAGE_KEY].get(pod_key), mem_mib.get(pod_key))
        for pod_key in set(peaks[CPU_USAGE_KEY]) | set(peaks[MEM_USAGE_KEY])
    }
    if not merged:
        return fail(0, f'no time series for cluster_name="{cluster}" over the trailing {window_hours}h')

    # The manifest digests a command's stdout to prove two runs saw the same
    # thing. There is no stdout here, so stand in the parsed answer, rounded
    # so a digest tracks a real change in usage rather than float noise.
    rendered = json.dumps(
        sorted(
            (ns, pod, None if cpu is None else round(cpu, USAGE_DIGEST_CPU_DIGITS), None if mem is None else round(mem, USAGE_DIGEST_MEM_DIGITS))
            for (ns, pod), (cpu, mem) in merged.items()
        )
    )
    return merged, True, Run([label], 0, rendered, "", time.monotonic() - started)


def fetch_memory_means(
    project: str,
    cluster: str,
    *,
    location: str | None = None,
    session: SessionFn,
    now: datetime,
    window_hours: int = USAGE_WINDOW_HOURS,
) -> tuple[dict[tuple[str, str], float], bool, Run]:
    """Sustained memory per pod, in MiB, keyed like `fetch_usage_peaks`.

    `check_underrequest` needs the mean and not the peak, and the distinction
    is the whole check: a *peak* above the request is what Burstable QoS exists
    for, and a *mean* above it is a request that was sized wrong. Reading the
    peak would flag every workload that ever bursts.

    Memory only, and deliberately -- one extra Cloud Monitoring round trip per
    cluster rather than two. CPU has no equivalent finding: it is compressible,
    so a CPU request below actual usage costs throttling under contention,
    while memory above the request is what kubelet ranks pods by when it needs
    to evict one.

    `available=False` reads the same way as `fetch_usage_peaks`': a failed or
    empty answer means the check could not run, never that usage was zero.
    """
    started = time.monotonic()
    start = now - timedelta(hours=window_hours)
    url = MONITORING_TIMESERIES_URL.format(project=project)
    label = _monitoring_command(project, [
        _pod_series_params(
            MEM_METRIC, cluster, location, start=start, now=now,
            primary=ALIGN_MAX, secondary=ALIGN_MEAN, window_hours=window_hours,
        )
    ])

    def fail(rc: int, message: str) -> tuple[dict, bool, Run]:
        return {}, False, Run([label], rc, "", message[:ERROR_EXCERPT_CHARS], time.monotonic() - started)

    if session is None:
        return fail(-1, NO_SESSION_MESSAGE)

    sink, err = _read_pod_series(
        session, url, metric=MEM_METRIC, cluster=cluster, location=location, start=start, now=now,
        primary=ALIGN_MAX, secondary=ALIGN_MEAN, window_hours=window_hours,
    )
    if err is not None:
        return fail(*err)
    means = {pod_key: value / BYTES_PER_MIB for pod_key, value in sink.items()}
    if not means:
        return fail(0, f'no time series for cluster_name="{cluster}" over the trailing {window_hours}h')

    rendered = json.dumps(sorted((ns, pod, round(mem, USAGE_DIGEST_MEM_DIGITS)) for (ns, pod), mem in means.items()))
    return means, True, Run([label], 0, rendered, "", time.monotonic() - started)


def _read_lb_series(
    session: SessionFn,
    url: str,
    *,
    metric: str,
    start: datetime,
    now: datetime,
) -> tuple[dict[tuple[str, str], float], tuple[int, str] | None]:
    """One load-balancer counter, totalled over the window per forwarding rule,
    keyed by `(region, rule name)`.

    Two collapses happen here and they are different operations. Confusing them
    doubles a number that goes into a published finding, which is the failure
    this whole read was added to stop.

    Within a series the points are **summed**. These are DELTA counters and the
    alignment period is a day, so the answer is seven buckets that add up to
    the window.

    Across the series of one rule the totals are **maxed**, not added. The same
    traffic is reported twice -- once under the legacy `tcp_lb_rule` /
    `udp_lb_rule` monitored resources and again under
    `loadbalancing.googleapis.com/ExternalNetworkLoadBalancerRule` -- and
    `crossSeriesReducer` does not merge across resource *types*, so grouping by
    the rule name still returns two near-equal series per rule. Measured
    against adamparco-kage over 168h on 2026-09-07: rule `ab3a83e6…` came back
    as 374,158 and 371,246 inbound packets, and summing them would have
    published 745,404 for a rule that saw 374,158. `max` also does the right
    thing for a rule only one of the two types reports, which is most of them.
    """
    sink: dict[tuple[str, str], float] = {}
    page_token = None
    while True:
        params = {**_lb_traffic_params(metric, start=start, now=now), "pageSize": MONITORING_PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        try:
            response = session.get(url, params=params, timeout=MONITORING_TIMEOUT_S)
        except Exception as exc:
            return {}, (-1, f"{metric}: {type(exc).__name__}: {exc}")
        if response.status_code != HTTP_OK:
            return {}, (response.status_code, f"{metric}: {response.text}")
        body = _json_body(response)
        if body is None:
            return {}, (-1, f"{metric}: {NON_JSON_BODY}")
        for series in body.get("timeSeries") or []:
            labels = (series.get("resource") or {}).get("labels") or {}
            rule = labels.get("forwarding_rule_name", "")
            region = labels.get("region", "")
            # An unlabelled series is the mis-grouped answer `LB_RULE_LABEL`
            # describes, and it carries the project's whole traffic. Dropping it
            # loses a rule's figures at worst; keeping it invents them.
            # A series with no region cannot be told from a same-named rule in
            # another region, so it is dropped the same way: unmeasured, never
            # credited to the wrong address.
            if not rule or not region:
                continue
            total = 0.0
            for point in series.get("points") or []:
                value = _point_value(point)
                if value is not None:
                    total += value
            sink[(region, rule)] = max(sink.get((region, rule), 0.0), total)
        page_token = body.get("nextPageToken")
        if not page_token:
            return sink, None


def fetch_lb_traffic(
    project: str,
    rules: list | None,
    *,
    session: SessionFn,
    now: datetime,
    window_hours: int = USAGE_WINDOW_HOURS,
) -> tuple[dict[str, dict], Run] | None:
    """What each external forwarding rule in `project` metered, keyed by address.

    Keyed by IP and not by rule name because of what the join needs on the
    other end: a Service carries `status.loadBalancer.ingress[].ip` and no rule
    name, the Monitoring answer carries a rule name and no address, and
    `gcloud compute forwarding-rules list` is the only thing that holds both.
    That is why `collect_fleet` reads the rules before the worker pool instead
    of leaving them where §3.6 uses them.

    Every `EXTERNAL` rule appears in the answer. A rule Monitoring returned no
    series for is present with `None` figures, and that distinction is the
    point: an absent address means this run has nothing to say, a `None` means
    the rule was not measured, and neither is a zero. `check_idle_workload`
    keeps all three apart, because reading any of them as "no traffic" is how
    the check came to assert a caller it had never looked for.

    None when the project has no external rule, because then no request is
    sent: a `Run` for it would reach the manifest as an rc-0 Monitoring read
    that never happened, on every cluster of most projects in a fleet.
    """
    started = time.monotonic()
    start = now - timedelta(hours=window_hours)
    url = MONITORING_TIMESERIES_URL.format(project=project)
    metrics = (
        ("ingress_packets", LB_INGRESS_PACKETS_METRIC),
        ("egress_packets", LB_EGRESS_PACKETS_METRIC),
        ("egress_bytes", LB_EGRESS_BYTES_METRIC),
    )
    label = _monitoring_command(project, [_lb_traffic_params(metric, start=start, now=now) for _, metric in metrics])

    def fail(rc: int, message: str) -> tuple[dict, Run]:
        return {}, Run([label], rc, "", message[:ERROR_EXCERPT_CHARS], time.monotonic() - started)

    addresses = {
        (_location_of(rule), str(rule.get("name") or "")): str(rule.get("IPAddress") or "")
        for rule in (rules or [])
        if isinstance(rule, dict)
        and rule.get("loadBalancingScheme") == LB_EXTERNAL_SCHEME
        and rule.get("IPAddress")
        and rule.get("name")
    }
    if not addresses:
        return None
    if session is None:
        return fail(-1, NO_SESSION_MESSAGE)

    totals: dict[tuple[str, str], dict[str, float]] = {}
    for key, metric in metrics:
        sink, err = _read_lb_series(session, url, metric=metric, start=start, now=now)
        if err is not None:
            return fail(*err)
        for rule, value in sink.items():
            totals.setdefault(rule, {})[key] = value

    # Several rules can share one address -- a TCP and a UDP Service on one
    # static IP is a documented GKE pattern -- so their traffic is summed
    # rather than the last rule read overwriting the others. One unmeasured
    # rule leaves the sum unknown rather than understated.
    by_address: dict[str, dict] = {}
    for (region, rule), address in sorted(addresses.items()):
        measured = {key: totals.get((region, rule), {}).get(key) for key, _ in metrics}
        entry = by_address.get(address)
        if entry is None:
            by_address[address] = {"rule": rule, **measured}
            continue
        entry["rule"] = f"{entry['rule']},{rule}"
        for key, _ in metrics:
            entry[key] = None if entry[key] is None or measured[key] is None else entry[key] + measured[key]
    # Stands in for the stdout the manifest digests, the way `fetch_usage_peaks`'
    # rendering does. Sorted by address explicitly rather than by the whole row:
    # the rows carry `None` where a rule went unmeasured, and a tuple sort that
    # ever reached one of those columns would raise instead of ordering.
    rendered = json.dumps(
        sorted(
            (
                [address, entry["rule"], entry["ingress_packets"], entry["egress_packets"], entry["egress_bytes"]]
                for address, entry in by_address.items()
            ),
            key=lambda row: row[0],
        )
    )
    return by_address, Run([label], 0, rendered, "", time.monotonic() - started)


# --------------------------------------------------------------------------- #
# Object normalization — every kind Step 2 dumps, filtered to what each check
# actually needs. `dump` is the parsed `kubectl get <kinds> -A -o json`.
# --------------------------------------------------------------------------- #


def _by_kind(dump: dict, kind: str) -> list[dict]:
    return [i for i in dump.get("items", []) or [] if i.get("kind") == kind]


def reconciler_of(meta: dict) -> str | None:
    """What continuously reasserts this object's spec, named, or None.

    The fact a `manual` remediation needs: on an object a controller reconciles,
    a change applied by hand is undone. See `collect.reconciler_of`, which holds
    the measurement and the history.
    """
    annotations = meta.get("annotations") or {}
    release = str(annotations.get(_HELM_RELEASE_ANNOTATION) or "").strip()
    if release:
        namespace = str(annotations.get(_HELM_NAMESPACE_ANNOTATION) or "").strip()
        where = f" in {namespace}" if namespace else ""
        return f"the Helm release `{release}`{where}"
    tracking = str(annotations.get(_ARGOCD_TRACKING_ANNOTATION) or "").strip()
    if tracking:
        return f"the Argo CD Application `{tracking.split(':', 1)[0]}`"
    managed_by = str((meta.get("labels") or {}).get(_MANAGED_BY_LABEL) or "").strip()
    if managed_by and managed_by.lower() != _HELM_MANAGED_BY:
        return f"`{managed_by}`"
    return None


def release_of(meta: dict) -> dict | None:
    """The chart release holding this object, as data, or None.

    `reconciler_of` above answers the same question in prose, for a sentence in
    the finding. This answers it in the form `release_declaration_for` can look
    up: `{"namespace", "name", "application"}`, any of which may be `""`. See
    `collect.release_of`, which holds the reasoning about the two markers.
    """
    annotations = meta.get("annotations") or {}
    name = str(annotations.get(_HELM_RELEASE_ANNOTATION) or "").strip()
    namespace = str(annotations.get(_HELM_NAMESPACE_ANNOTATION) or "").strip()
    tracking = str(annotations.get(_ARGOCD_TRACKING_ANNOTATION) or "").strip()
    application = tracking.split(":", 1)[0].strip() if tracking else ""
    if not name and not application:
        return None
    return {"namespace": namespace, "name": name, "application": application}


def _reconcilers_by_object(dump: dict) -> dict[tuple[str, str], str]:
    """`(namespace, "Kind/name") -> reconciler`, for every object in the dump.

    Keyed the way `_emit` builds a candidate, so the annotation is a lookup
    rather than a second pass over the dump per check. Objects with no marker
    are absent, which is the same thing as a miss: both mean "say nothing".
    """
    index: dict[tuple[str, str], str] = {}
    for item in dump.get("items", []) or []:
        meta = item.get("metadata") or {}
        reconciler = reconciler_of(meta)
        if not reconciler:
            continue
        index[(meta.get("namespace", "") or "", f"{item.get('kind', '')}/{meta.get('name', '')}")] = reconciler
    return index


def _releases_by_object(dump: dict, declarations: dict[tuple, dict] | None, cluster: str) -> dict[tuple[str, str], dict]:
    """`(namespace, "Kind/name") -> release declaration`, for the objects a
    chart this repository declares renders.

    Keyed the way `_emit` builds a candidate, for the same reason
    `_reconcilers_by_object` is: one pass over the dump rather than one per
    check. An object with no release marker, or one whose release nothing in
    the clone declares, is absent -- and absent is a miss, which is the
    `manual` verdict this stream published before the index existed.
    """
    if not declarations or not cluster:
        return {}
    index: dict[tuple[str, str], dict] = {}
    for item in dump.get("items", []) or []:
        meta = item.get("metadata") or {}
        found = release_declaration_for(declarations, cluster, release_of(meta))
        if not found:
            continue
        index[(meta.get("namespace", "") or "", f"{item.get('kind', '')}/{meta.get('name', '')}")] = found
    return index


def build_context(dump: dict) -> dict:
    return {
        "reconcilers": _reconcilers_by_object(dump),
        "nodes": _by_kind(dump, "Node"),
        "pods": _by_kind(dump, "Pod"),
        "pvcs": _by_kind(dump, "PersistentVolumeClaim"),
        "pvs": _by_kind(dump, "PersistentVolume"),
        "services": _by_kind(dump, "Service"),
        "jobs": _by_kind(dump, "Job"),
        "cronjobs": _by_kind(dump, "CronJob"),
        "pdbs": _by_kind(dump, "PodDisruptionBudget"),
        "namespaces": _by_kind(dump, "Namespace"),
        "resourcequotas": _by_kind(dump, "ResourceQuota"),
        "statefulsets": _by_kind(dump, "StatefulSet"),
        "deployments": _by_kind(dump, "Deployment"),
        "hpas": _by_kind(dump, "HorizontalPodAutoscaler"),
        "limitranges": _by_kind(dump, "LimitRange"),
        "ingresses": _by_kind(dump, "Ingress"),
    }


def _hpa_targets(context: dict) -> set[tuple[str, str, str]]:
    """`(namespace, kind, name)` of every workload an HPA scales.

    §3.1 leaves these alone: an HPA's target utilisation is a fraction of the
    request, so halving the request doubles the utilisation it reads and the
    autoscaler answers with replicas -- the `minReplicas` floor is the lever.
    """
    targets = set()
    for hpa in context.get("hpas", []):
        ref = (hpa.get("spec") or {}).get("scaleTargetRef") or {}
        if ref.get("kind") and ref.get("name"):
            targets.add(((hpa.get("metadata") or {}).get("namespace", ""), ref["kind"], ref["name"]))
    return targets


def _limitrange_defaults(context: dict) -> dict[str, dict]:
    """Per namespace, the container request its LimitRanges fill in.

    `defaultRequest` where set; otherwise `default`. The API server fills
    `defaultRequest` from `default` when it stores the object, so on a live
    read the merge changes nothing. Only this one fill-in is applied, not the
    server's `max`/`min` ones, because the only caller reads live objects."""
    defaults: dict[str, dict] = {}
    for lr in context.get("limitranges", []):
        ns = (lr.get("metadata") or {}).get("namespace", "")
        for limit in (lr.get("spec") or {}).get("limits") or []:
            if limit.get("type") != "Container":
                continue
            filled = {**(limit.get("default") or {}), **(limit.get("defaultRequest") or {})}
            if filled:
                defaults.setdefault(ns, {}).update(filled)
    return defaults


def _requests_are_the_namespace_default(entry: dict, defaults: dict) -> bool:
    """Whether every container's request is exactly what the LimitRange filled in.

    §3.1 says to fix the LimitRange rather than the workload, so a request
    nobody wrote is not this workload's sizing decision. A request for a
    resource the LimitRange does not default was written by hand, so it keeps
    the workload in scope: GKE's stock CPU-only default must not hide an
    oversized memory request."""
    if not defaults:
        return False
    for pod in entry["pods"]:
        for req in pod["requests"]:
            for resource in ("cpu", "memory"):
                if resource not in defaults:
                    if req.get(resource):
                        return False
                    continue
                parse = parse_cpu_cores if resource == "cpu" else parse_mem_mib
                if parse(str(req.get(resource, ""))) != parse(str(defaults[resource])):
                    return False
    return True


def _namespace_defaulted_dimensions(entry: dict, defaults: dict) -> frozenset[str]:
    """The request dimensions on which every container carries the LimitRange default.

    A controller with one hand-written dimension stays in scope, but the
    dimension the LimitRange filled in is still nobody's sizing decision, so
    §3.1's fix-the-LimitRange rule drops that dimension from the verdict rather
    than proposing a resize of a value the workload never declared."""
    defaulted = set()
    for resource in ("cpu", "memory"):
        if resource not in defaults:
            continue
        parse = parse_cpu_cores if resource == "cpu" else parse_mem_mib
        default = parse(str(defaults[resource]))
        if all(parse(str(req.get(resource, ""))) == default for pod in entry["pods"] for req in pod["requests"]):
            defaulted.add(resource)
    return frozenset(defaulted)


def _age_days(timestamp: str, *, now: datetime) -> float | None:
    if not timestamp:
        return None
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (now - ts).total_seconds() / SECONDS_PER_DAY


def _whole_days(age: float) -> int:
    """Elapsed days, floored -- never the next day up.

    `f"{age:.0f}"` rounds to nearest, which reads as a harmless half-day of
    imprecision until the number lands next to a threshold. It did: the same
    `argocd-webhook-ip` the docstring below is about was 29.69 days old on
    2026-09-01, printed as "(30d ago)", and the model titled the finding
    "unused for 30+ days" -- a claim about a threshold, derived from a rounded
    number, and false. Rounding down cannot produce that: a model given "29d
    ago" has nothing to round up from.

    Floored rather than truncated toward zero only matters for a negative age,
    which means the clock moved backwards between the object's creation and the
    read. Clamp that to 0 rather than print "-1d", which would assert the thing
    is from the future.
    """
    return int(age) if age > 0 else 0


def _ago(age: float | None) -> str:
    """The elapsed-days parenthetical an excerpt puts after a timestamp.

    Every check here that gates on age computes one, decides with it, and then
    quoted the raw ISO timestamp and threw the number away. The model reading
    the manifest still has to say how long the thing has been idle -- that is
    the finding -- so it re-derived the age by doing date arithmetic on the
    string, and got it wrong: an address reserved on 2026-08-02, read on
    2026-09-01, was published as "unused for 28 days". Nothing downstream
    catches that, because `adopt_collector_evidence` replaces the model's
    evidence with the collector's and leaves the title alone, so the wrong
    number is the one a reader sees next to correct evidence.

    Returning the number the gate already used costs nothing and removes the
    arithmetic from the model's job. Empty when the age is unknown rather than
    guessing: a missing timestamp is why `_age_days` returns None, and "(0d
    ago)" would assert something about it.
    """
    return "" if age is None else f" ({_whole_days(age)}d ago)"


# --------------------------------------------------------------------------- #
# 3.2 orphan-pv
# --------------------------------------------------------------------------- #


def _matches_live_statefulset_pvc(claim_name: str, sts_names: set[str]) -> bool:
    """`<volumeClaimTemplate>-<statefulSet>-<ordinal>` for a StatefulSet that
    still exists is a scaled-to-zero StatefulSet's own claim, deliberate and
    never waste -- regardless of what the volumeClaimTemplate is named."""
    return any(re.match(rf"^.+-{re.escape(sts)}-\d+$", claim_name) for sts in sts_names if sts)


def _statefulsets_by_namespace(context: dict) -> dict[str, set[str]]:
    """StatefulSet names per namespace: a StatefulSet only claims in its own."""
    sts_by_ns: dict[str, set[str]] = {}
    for sts in context.get("statefulsets", []):
        sts_by_ns.setdefault(sts.get("metadata", {}).get("namespace", ""), set()).add(sts.get("metadata", {}).get("name", ""))
    return sts_by_ns


def check_orphan_pv(context: dict, *, now: datetime) -> list[dict]:
    pvc_uid = {(p["metadata"].get("namespace", ""), p["metadata"].get("name", "")): p["metadata"].get("uid", "") for p in context["pvcs"]}
    # Per namespace, as 3.3 keys it.
    sts_by_ns = _statefulsets_by_namespace(context)
    # A statically provisioned PV waiting on a claim that has not bound yet is
    # pre-staged, not abandoned. Only this cluster's claims can bind it.
    pending_classes = {
        (p.get("spec") or {}).get("storageClassName") or ""
        for p in context["pvcs"]
        if (p.get("status") or {}).get("phase") == "Pending"
    }
    hits = []
    for pv in context["pvs"]:
        meta, spec, status = pv.get("metadata", {}), pv.get("spec", {}), pv.get("status", {})
        name = meta.get("name", "")
        if spec.get("persistentVolumeReclaimPolicy") != "Retain":
            continue
        annotations = meta.get("annotations") or {}
        if any(k.startswith(prefix) for k in annotations for prefix in BACKUP_ANNOTATION_PREFIXES):
            continue
        if (meta.get("labels") or {}).get(ADDON_MANAGER_KEY) or annotations.get(ADDON_MANAGER_KEY):
            continue
        phase = status.get("phase", "")
        claim_ref = spec.get("claimRef") or {}
        claim_ns, claim_name = claim_ref.get("namespace", ""), claim_ref.get("name", "")
        if claim_name:
            # The name alone is not the claim: a StatefulSet PVC deleted and
            # recreated binds a fresh PV under the same name, and the old
            # Released one is exactly the orphan §3.2 is for.
            live_uid = pvc_uid.get((claim_ns, claim_name))
            if live_uid is not None:
                if not claim_ref.get("uid") or not live_uid or claim_ref.get("uid") == live_uid:
                    continue
            elif _matches_live_statefulset_pvc(claim_name, sts_by_ns.get(claim_ns, set())):
                continue

        if phase in ("Released", "Failed"):
            transition = status.get("lastPhaseTransitionTime", "")
            age = _age_days(transition, now=now) if transition else _age_days(meta.get("creationTimestamp", ""), now=now)
            fallback_note = "" if transition else " (lastPhaseTransitionTime absent; using object AGE)"
            # Creation is older than the release it stands in for, so the
            # fallback takes the Available arm's longer floor.
            if age is None or age < (ORPHAN_PV_RELEASED_DAYS if transition else ORPHAN_PV_UNCLAIMED_DAYS):
                continue
            hits.append(
                {
                    "object": f"PersistentVolume/{name}",
                    "excerpt": f"phase={phase} since {transition or meta.get('creationTimestamp')}{_ago(age)}{fallback_note}, capacity={spec.get('capacity', {}).get('storage')}",
                    "severity": "major" if _is_large_or_ssd(spec) else "minor",
                }
            )
        elif phase == "Available" and not claim_ref:
            age = _age_days(meta.get("creationTimestamp", ""), now=now)
            if age is None or age < ORPHAN_PV_UNCLAIMED_DAYS:
                continue
            if (spec.get("storageClassName") or "") in pending_classes:
                continue
            hits.append(
                {
                    "object": f"PersistentVolume/{name}",
                    "excerpt": f"phase=Available, unclaimed, AGE={_whole_days(age)}d, storageClass={spec.get('storageClassName')}",
                    "severity": "major" if _is_large_or_ssd(spec) else "minor",
                }
            )
    return hits


def _gib(quantity: str) -> float:
    mib = parse_mem_mib(quantity or "0")
    return mib / MIB_PER_GIB if mib is not None else 0.0


def _is_ssd_class(storage_class: str) -> bool:
    sc = storage_class.lower()
    return any(marker in sc for marker in SSD_STORAGE_CLASS_MARKERS)


def _is_large_or_ssd(spec: dict) -> bool:
    gib = _gib((spec.get("capacity") or {}).get("storage", "0"))
    return gib >= LARGE_VOLUME_GIB or _is_ssd_class(spec.get("storageClassName") or "")


# --------------------------------------------------------------------------- #
# 3.3 unconsumed-pvc
# --------------------------------------------------------------------------- #


def _template_pod_spec(controller: dict) -> dict:
    """The pod spec a Job or CronJob will create, or `{}`."""
    spec = controller.get("spec") or {}
    if controller.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    return ((spec.get("template") or {}).get("spec")) or {}


def check_unconsumed_pvc(context: dict, *, now: datetime) -> list[dict]:
    referenced = set()
    # §3.3 spares a claim a suspended CronJob or a not-yet-started Job will
    # mount: no pod references it yet, but one will. Any Job or CronJob
    # template counts, not only the suspended and unstarted ones -- a live
    # CronJob between runs holds its claim the same way.
    templates = [
        (item.get("metadata", {}).get("namespace", ""), _template_pod_spec(item))
        for item in (context.get("jobs") or []) + (context.get("cronjobs") or [])
    ]
    pods = [(pod.get("metadata", {}).get("namespace", ""), pod.get("spec") or {}) for pod in context["pods"]]
    for ns, pod_spec in pods + templates:
        for vol in pod_spec.get("volumes") or []:
            claim = (vol.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                referenced.add((ns, claim))
    # A generic ephemeral volume names no claim: Kubernetes creates one called
    # `<pod>-<volume>` for the pod's life and deletes it with the pod.
    for pod in context["pods"]:
        meta = pod.get("metadata", {})
        for vol in (pod.get("spec") or {}).get("volumes") or []:
            if vol.get("ephemeral") and vol.get("name"):
                referenced.add((meta.get("namespace", ""), f"{meta.get('name', '')}-{vol['name']}"))
    sts_by_ns = _statefulsets_by_namespace(context)

    hits = []
    for pvc in context["pvcs"]:
        meta, spec, status = pvc.get("metadata", {}), pvc.get("spec", {}), pvc.get("status", {})
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        if _is_system_namespace(ns):
            continue
        if status.get("phase") != "Bound":
            continue
        if (ns, name) in referenced:
            continue
        # A claim a Pod owns is garbage-collected with it; there is nothing to
        # reclaim by hand, even if the pod list above missed the pod.
        if any(ref.get("kind") == "Pod" for ref in meta.get("ownerReferences") or []):
            continue
        if _matches_live_statefulset_pvc(name, sts_by_ns.get(ns, set())):
            continue
        annotations = meta.get("annotations") or {}
        if any(k.startswith(CONFIG_SYNC_MARKER_PREFIX) for k in annotations):
            continue
        age = _age_days(meta.get("creationTimestamp", ""), now=now)
        if age is None or age < UNCONSUMED_PVC_MIN_AGE_DAYS:
            continue
        capacity = (status.get("capacity") or {}).get("storage", "0")
        gib = _gib(capacity)
        sc = (spec.get("storageClassName") or "").lower()
        hits.append(
            {
                "namespace": ns,
                "object": f"PersistentVolumeClaim/{name}",
                "excerpt": f"Bound, {capacity}, {sc}, unreferenced by any pod, AGE={_whole_days(age)}d",
                "severity": "major" if gib >= LARGE_VOLUME_GIB or _is_ssd_class(sc) else "minor",
            }
        )
    return hits


# --------------------------------------------------------------------------- #
# 3.7 idle-nodepool / 3.8 scaledown-blocked
# --------------------------------------------------------------------------- #


def _pod_daemonset_owned(pod: dict) -> bool:
    return any(o.get("kind") == "DaemonSet" for o in (pod.get("metadata", {}).get("ownerReferences") or []))


def _pod_is_mirror(pod: dict) -> bool:
    return any(o.get("kind") == "Node" for o in (pod.get("metadata", {}).get("ownerReferences") or []))


def _sizing_containers(spec: dict) -> tuple[list[dict], list[dict]]:
    """A pod's long-running containers, native sidecars last, and the rest.

    Cloud Monitoring's per-pod usage sums every container that runs, and a
    `restartPolicy: Always` init container runs for the pod's whole life. The
    request side has to cover the same containers or a sidecar's usage is set
    against the app containers' requests alone. The second list is the plain
    init containers, which run to completion before the app starts.
    """
    init = spec.get("initContainers") or []
    sidecars = [c for c in init if c.get("restartPolicy") == SIDECAR_RESTART_POLICY]
    plain = [c for c in init if c.get("restartPolicy") != SIDECAR_RESTART_POLICY]
    return (spec.get("containers") or []) + sidecars, plain


def _container_requests(c: dict) -> tuple[float, float]:
    req = (c.get("resources") or {}).get("requests") or {}
    return parse_cpu_cores(str(req.get("cpu", "0"))) or 0, parse_mem_mib(str(req.get("memory", "0"))) or 0


def _pod_effective_requests(pod: dict) -> tuple[float, float]:
    """The requests the scheduler reserves for one pod, per resource.

    App containers plus native sidecars (`restartPolicy: Always` init
    containers), which run alongside them; or, if larger, the heaviest plain
    init container together with the sidecars started before it, since those
    run while it does. Summing app containers alone understated a node's
    requests and read a node carrying a heavy sidecar as idler than the
    scheduler does. `_is_guaranteed` reads init containers for the same reason.
    """
    spec = pod.get("spec") or {}
    cpu, mem = 0.0, 0.0
    for c in spec.get("containers") or []:
        c_cpu, c_mem = _container_requests(c)
        cpu, mem = cpu + c_cpu, mem + c_mem
    sidecar_cpu = sidecar_mem = init_cpu = init_mem = 0.0
    for c in spec.get("initContainers") or []:
        c_cpu, c_mem = _container_requests(c)
        if c.get("restartPolicy") == SIDECAR_RESTART_POLICY:
            sidecar_cpu, sidecar_mem = sidecar_cpu + c_cpu, sidecar_mem + c_mem
        else:
            init_cpu, init_mem = max(init_cpu, sidecar_cpu + c_cpu), max(init_mem, sidecar_mem + c_mem)
    return max(init_cpu, cpu + sidecar_cpu), max(init_mem, mem + sidecar_mem)


def _sum_requests(pods: list[dict]) -> tuple[float, float]:
    cpu_total, mem_total = 0.0, 0.0
    for pod in pods:
        cpu, mem = _pod_effective_requests(pod)
        cpu_total += cpu
        mem_total += mem
    return cpu_total, mem_total


def _allocatable(node: dict) -> tuple[float, float]:
    alloc = (node.get("status") or {}).get("allocatable") or {}
    return parse_cpu_cores(str(alloc.get("cpu", "0"))) or 0, parse_mem_mib(str(alloc.get("memory", "0"))) or 0


def _machine_type_vcpus(machine_type: str) -> int | None:
    for pattern in (MACHINE_TYPE_VCPU_RE, CUSTOM_MACHINE_TYPE_VCPU_RE):
        m = pattern.match(machine_type or "")
        if m:
            return int(m.group(1))
    return None


def _is_big_machine(machine_type: str) -> bool:
    """§3.7's "machine type of >= 8 vCPU, or attached accelerators" severity leg."""
    if ACCELERATOR_MACHINE_RE.match(machine_type or ""):
        return True
    return (_machine_type_vcpus(machine_type) or 0) >= BIG_MACHINE_VCPUS


def node_pool_creation_ages(operations: object, cluster: str, *, now: datetime) -> dict[str, float]:
    """Days since each node pool of `cluster` was last created, from
    `gcloud container operations list` filtered to `CREATE_NODE_POOL`.

    A pool with no entry was created before the operations the API still
    lists, or with the cluster itself (whose first pool arrives in
    `CREATE_CLUSTER`); `check_idle_nodepool` dates it from the cluster. The latest
    creation wins: a pool deleted and recreated under one name is as old as its
    newest incarnation.
    """
    ages: dict[str, float] = {}
    marker = f"/clusters/{cluster}/nodePools/"
    for op in operations if isinstance(operations, list) else []:
        if not isinstance(op, dict) or op.get("operationType") != CREATE_NODE_POOL_OPERATION:
            continue
        link = str(op.get("targetLink") or "")
        if marker not in link:
            continue
        pool = link.rsplit(marker, 1)[1].split("/", 1)[0]
        age = _age_days(OPERATION_FRACTION_RE.sub(r"\1", str(op.get("startTime") or "")), now=now)
        if pool and age is not None:
            ages[pool] = min(age, ages.get(pool, age))
    return ages


def check_idle_nodepool(
    context: dict,
    node_pools: list[dict],
    *,
    now: datetime,
    pool_ages: dict[str, float] | None = None,
    cluster_age: float | None = None,
    limitations: list[str] | None = None,
) -> list[dict]:
    """§3.7. `pool_ages` is `node_pool_creation_ages`' answer, or `None` when
    the operations read failed; only then does the oldest node stand in for the
    pool's age, and a pool that stand-in exempts is named in `limitations`,
    because a node upgrade recreates every node and resets it. A pool with no
    creation operation came with the cluster or before the operations the API
    keeps, so it is `cluster_age` days old, and older than the threshold when
    that is unknown."""
    nodes_by_pool: dict[str, list[dict]] = {}
    for node in context["nodes"]:
        pool = (node.get("metadata", {}).get("labels") or {}).get(NODEPOOL_LABEL, "")
        nodes_by_pool.setdefault(pool, []).append(node)

    running_pods = [p for p in context["pods"] if (p.get("status") or {}).get("phase") == "Running"]
    pods_by_node: dict[str, list[dict]] = {}
    # Every Running pod, DaemonSets included: what an absorbing node has
    # already promised away. The 15% test excludes DaemonSets; the headroom
    # a drain would find does not.
    all_pods_by_node: dict[str, list[dict]] = {}
    for pod in running_pods:
        node_name = (pod.get("spec") or {}).get("nodeName", "")
        if not node_name:
            continue
        all_pods_by_node.setdefault(node_name, []).append(pod)
        if not _pod_daemonset_owned(pod):
            pods_by_node.setdefault(node_name, []).append(pod)

    hits = []
    for pool in node_pools:
        pool_name = pool.get("name", "")
        nodes = nodes_by_pool.get(pool_name, [])
        if not nodes or len(node_pools) <= 1:
            continue
        # §3.7's "pools created < 7 days ago" exclusion, off the pool's own
        # `CREATE_NODE_POOL` operation. Node age is not the pool's: a surge
        # upgrade recreates every node, so a months-old pool read as a week old
        # after each auto-upgrade and went unflagged for that run.
        if pool_ages is not None:
            default_age = IDLE_NODEPOOL_MIN_AGE_DAYS if cluster_age is None else cluster_age
            if pool_ages.get(pool_name, default_age) < IDLE_NODEPOOL_MIN_AGE_DAYS:
                continue
        else:
            # Fallback only. Measured off the *oldest* node rather than
            # whichever one the dump listed first: the dump comes back
            # name-sorted, and a pool that has run for months would exempt
            # itself the moment an autoscaler added a node whose name sorts
            # early.
            ages = [
                age
                for node in nodes
                if (age := _age_days((node.get("metadata", {}) or {}).get("creationTimestamp", ""), now=now))
                is not None
            ]
            if ages and max(ages) < IDLE_NODEPOOL_MIN_AGE_DAYS:
                if limitations is not None:
                    limitations.append(
                        f"idle-nodepool skipped pool {pool_name} as under {IDLE_NODEPOOL_MIN_AGE_DAYS} days old "
                        f"on its oldest node's age, because the node-pool operations read failed; a node "
                        f"upgrade resets that age, so the pool may be older"
                    )
                continue
        if any(node.get("spec", {}).get("unschedulable") for node in nodes):
            continue

        # §3.7 flags when *every node in the pool* is under 15%, not when the
        # pool averages under 15%. The two disagree exactly where it matters:
        # a ten-node pool with one node full and nine empty averages 10% and
        # was reported as an idle pool whose floor should drop to zero, when
        # the node holding the workload is precisely what stops it shrinking.
        # The aggregate figures are still computed, because the excerpt quotes
        # them and a reader wants the pool-level number.
        #
        # The 15% is measured against *workload* requests -- non-DaemonSet and
        # non-`SYSTEM_NS` -- not against every non-DaemonSet pod. §3.7 justifies
        # the bar as sitting "below the point where DaemonSet and system
        # overhead (typically 10-25% of a small node) dominates", and that
        # premise does not survive measurement: GKE's non-DaemonSet add-ons
        # (kube-dns 270m, kube-state-metrics 105m, kube-proxy as a static pod
        # 100m, metrics-server, konnectivity, l7-default-backend and the two
        # autoscalers) come to ~0.57 vCPU on every node, so the bar is only
        # reachable above ~3.8 vCPU of allocatable. On the sixteen-cluster
        # fleet measured 2026-09-05 every Standard pool read 45-70% and the
        # check fired on nothing at all -- including a lone untainted e2-small
        # `default-pool` carrying zero workload pods next to a pool with 1.5
        # vCPU free, which is exactly the finding it exists to make. Those
        # add-ons reschedule with the node, so they are no more a reason to
        # keep a pool than the DaemonSets already excluded above; the "only
        # node pool" exclusion is where the SOP handles their needing a home.
        # The all-non-DaemonSet figure stays in the excerpt -- it is what the
        # cluster autoscaler actually weighs when deciding to drain.
        cpu_alloc_total = mem_alloc_total = cpu_req_total = mem_req_total = 0.0
        cpu_wk_total = mem_wk_total = 0.0
        workload_pods = 0
        every_node_idle = True
        for node in nodes:
            node_name = node.get("metadata", {}).get("name", "")
            cpu_alloc, mem_alloc = _allocatable(node)
            on_node = pods_by_node.get(node_name, [])
            workload = [
                pod
                for pod in on_node
                if not _is_system_namespace((pod.get("metadata") or {}).get("namespace", ""))
            ]
            cpu_req, mem_req = _sum_requests(on_node)
            cpu_wk, mem_wk = _sum_requests(workload)
            cpu_alloc_total += cpu_alloc
            mem_alloc_total += mem_alloc
            cpu_req_total += cpu_req
            mem_req_total += mem_req
            cpu_wk_total += cpu_wk
            mem_wk_total += mem_wk
            workload_pods += len(workload)
            if cpu_alloc <= 0 or mem_alloc <= 0:
                # A node reporting no allocatable capacity is one this check
                # cannot judge -- NotReady, or mid-registration. Treat it as
                # not-idle rather than divide by zero: an unreadable node is
                # not evidence the pool is reclaimable.
                every_node_idle = False
            elif cpu_wk / cpu_alloc > IDLE_NODEPOOL_REQUEST_FRACTION or mem_wk / mem_alloc > IDLE_NODEPOOL_REQUEST_FRACTION:
                every_node_idle = False
        if not every_node_idle:
            continue
        if cpu_alloc_total == 0 or mem_alloc_total == 0:
            continue
        cpu_pct, mem_pct = cpu_req_total / cpu_alloc_total, mem_req_total / mem_alloc_total
        wk_cpu_pct, wk_mem_pct = cpu_wk_total / cpu_alloc_total, mem_wk_total / mem_alloc_total

        autoscaling = pool.get("autoscaling") or {}
        floor_nonzero = not autoscaling.get("enabled") or (autoscaling.get("minNodeCount") or 0) >= 1
        if not floor_nonzero:
            continue

        machine_type = ((pool.get("config") or {}).get("machineType") or "")
        has_accelerator = bool((pool.get("config") or {}).get("accelerators"))
        severity = "major" if len(nodes) >= IDLE_NODEPOOL_MAJOR_NODES or _is_big_machine(machine_type) or has_accelerator else "minor"

        # Everything the pool holds has to land somewhere before a node drains,
        # and that includes the add-ons the gate above deliberately ignores. A
        # reader handed only "0 workload pods" still has to go and work out
        # whether kube-dns has anywhere to go, which is the difference between
        # an actionable finding and a research task -- so state the headroom.
        # It is pool-level arithmetic and takes no account of taints, node
        # selectors or zonal spread, hence the hedge in the wording: it is a
        # necessary condition for the drain, not a sufficient one. A cordoned
        # node accepts no pod, so its free capacity is not counted. Computed
        # after the loop, once every idle pool is known: see `_absorbing_room`.
        pool_node_names = {n.get("metadata", {}).get("name", "") for n in nodes}

        # Whether the *graceful* path works at all. Lowering the floor to zero
        # asks the autoscaler to drain, and it will refuse for as long as one
        # of these sits on the node -- so the finding would read as actionable
        # while the remediation quietly did nothing for weeks. §3.7 already
        # offers pool deletion as the alternative; this is what tells a reader
        # which of the two they need.
        pool_pods = [pod for name in pool_node_names for pod in pods_by_node.get(name, [])]
        blockers = _drain_blockers(pool_pods, _pdb_selectors(context))
        blocker_note = (
            f" Draining will not happen on its own: {len(blockers)} pod(s) on these nodes are ones "
            f"the cluster autoscaler refuses to evict ({', '.join(blockers[:BLOCKERS_NAMED])}"
            + (", …" if len(blockers) > BLOCKERS_NAMED else "")
            + "), so lowering the floor reclaims nothing and deleting the pool is the remediation "
            "that works."
            if blockers
            else ""
        )

        taints = (pool.get("config") or {}).get("taints") or []
        taint_note = (
            " Nodes are tainted "
            + ", ".join(f"{t.get('key')}={t.get('value')}:{t.get('effect')}" for t in taints)
            + " — check whether the pool is dedicated on purpose."
            if taints
            else ""
        )
        occupancy = (
            "no workload pods at all"
            if workload_pods == 0
            else f"{workload_pods} workload pod(s) requesting "
            f"{wk_cpu_pct * 100:.0f}% CPU / {wk_mem_pct * 100:.0f}% memory of allocatable"
        )
        # `min=None` reads as missing data when it actually means the pool has
        # a fixed size, and the two take different remediations: §3.7's
        # `--enable-autoscaling --min-nodes=0` has to *create* the autoscaler
        # on a fixed pool and only lowers the floor on one that already has it.
        floor = (
            f"min={autoscaling.get('minNodeCount')}"
            if autoscaling.get("enabled")
            else "autoscaling disabled, so the node count is a fixed floor"
        )
        hits.append(
            {
                "object": f"NodePool/{pool_name}",
                "_excerpt_head": (
                    f"{len(nodes)} node(s), {machine_type}, {floor}: "
                    f"{occupancy} outside SYSTEM_NS. Counting the system add-ons the autoscaler "
                    f"also weighs, non-DS CPU is {cpu_pct * 100:.0f}% / mem {mem_pct * 100:.0f}% "
                    f"of allocatable ({cpu_req_total:.2f} vCPU / {mem_req_total / MIB_PER_GIB:.1f} GiB), "
                ),
                "_excerpt_tail": f"{blocker_note}{taint_note}",
                "severity": severity,
                "_node_names": pool_node_names,
            }
        )
    return _absorbing_room(hits, context["nodes"], all_pods_by_node)


def _absorbing_room(hits: list[dict], nodes: list[dict], all_pods_by_node: dict[str, list[dict]]) -> list[dict]:
    """Finish each idle-pool excerpt with the room the rest of the cluster has.

    Every pool this run finds idle is left out of that room, not only the one
    the excerpt is about: with two idle pools each counted the other's free
    capacity, so a reader acting on both findings deleted the capacity each was
    told would absorb the other."""
    idle_nodes = set().union(*(hit["_node_names"] for hit in hits))
    free_cpu = free_mem = 0.0
    for node in nodes:
        name = node.get("metadata", {}).get("name", "")
        if name in idle_nodes or (node.get("spec") or {}).get("unschedulable"):
            continue
        cpu, mem = _allocatable(node)
        used_cpu, used_mem = _sum_requests(all_pods_by_node.get(name, []))
        free_cpu += max(0.0, cpu - used_cpu)
        free_mem += max(0.0, mem - used_mem)
    for hit in hits:
        others = sorted(h["object"] for h in hits if h is not hit)
        excluded = f" and not on {', '.join(others)}, which this run also finds idle," if others else ""
        hit["excerpt"] = (
            f"{hit.pop('_excerpt_head')}and the cluster's other pools have {free_cpu:.2f} vCPU / "
            f"{free_mem / MIB_PER_GIB:.1f} GiB unrequested on uncordoned nodes{excluded} to absorb it — before "
            f"taints, selectors and zonal spread, which this figure does not model.{hit.pop('_excerpt_tail')}"
        )
    return hits


def _safe_to_evict(annotations: dict) -> bool | None:
    """The `safe-to-evict` annotation as set-true, set-false, or unset.

    Unset covers every other value, which the autoscaler ignores.
    """
    raw = annotations.get(SAFE_TO_EVICT_ANNOTATION)
    if raw == SAFE_TO_EVICT_TRUE:
        return True
    if raw == SAFE_TO_EVICT_FALSE:
        return False
    return None


def _is_local_volume(volume: dict) -> bool:
    """The autoscaler's `isLocalVolume`: a `hostPath`, or a disk-backed `emptyDir`."""
    if HOST_PATH_VOLUME in volume:
        return True
    if EMPTY_DIR_VOLUME not in volume:
        return False
    return (volume.get(EMPTY_DIR_VOLUME) or {}).get("medium") != EMPTY_DIR_MEMORY_MEDIUM


def _blocking_local_storage(pod: dict) -> bool:
    """Whether the pod's local storage pins its node under
    `--skip-nodes-with-local-storage`: it has a local volume (`_is_local_volume`)
    that `safe-to-evict-local-volumes` does not list. The pod-wide
    `safe-to-evict` annotation is the caller's to weigh."""
    volumes = (pod.get("spec") or {}).get("volumes") or []
    local_names = {v.get("name", "") for v in volumes if _is_local_volume(v)}
    annotations = (pod.get("metadata") or {}).get("annotations") or {}
    raw = annotations.get(SAFE_TO_EVICT_LOCAL_VOLUMES_ANNOTATION) or ""
    listed = set(raw.split(SAFE_TO_EVICT_LOCAL_VOLUMES_SEPARATOR)) if raw else set()
    return bool(local_names - listed)


def _expression_matches(expr: dict, labels: dict) -> bool:
    key, op, values = expr.get("key", ""), expr.get("operator", ""), expr.get("values") or []
    if op == "In":
        return labels.get(key) in values
    if op == "NotIn":
        return key not in labels or labels[key] not in values
    if op == "Exists":
        return key in labels
    if op == "DoesNotExist":
        return key not in labels
    return False  # an operator this does not know matches nothing, as the API server would reject it


def _selector_matches(pdb: tuple[str, dict], ns: str, labels: dict) -> bool:
    """A PDB covers only pods in its own namespace, and both selector halves
    have to hold: `matchLabels` and every `matchExpressions` term. An empty
    selector matches every pod in the namespace, as it does for a PDB."""
    pdb_ns, selector = pdb
    if pdb_ns != ns:
        return False
    return all(labels.get(k) == v for k, v in (selector.get("matchLabels") or {}).items()) and all(
        _expression_matches(expr, labels) for expr in selector.get("matchExpressions") or []
    )


def _pdb_selectors(context: dict) -> list[tuple[str, dict]]:
    """Each PDB's namespace and selector. A PDB with no selector selects no
    pod in `policy/v1`, the opposite of an empty `{}` one, so it is left out
    rather than read as `{}` and made to cover the whole namespace."""
    return [
        ((pdb.get("metadata") or {}).get("namespace", ""), selector)
        for pdb in (context.get("pdbs") or [])
        if (selector := (pdb.get("spec") or {}).get("selector")) is not None
    ]


def _drain_blockers(pods: list[dict], pdb_selectors: list[dict]) -> list[str]:
    """Pods the cluster autoscaler will refuse to evict, so a node holding one
    never scales down.

    Two of the autoscaler's default skip rules, neither of which GKE lets you
    turn off: `--skip-nodes-with-system-pods` pins a node carrying any
    `kube-system` pod that has no PodDisruptionBudget, and
    `--skip-nodes-with-local-storage` pins one carrying an `emptyDir` or
    `hostPath` pod. A pod annotated `safe-to-evict: "true"` is exempt from
    both, as the autoscaler checks the annotation before either rule; one whose
    `safe-to-evict-local-volumes` lists every local volume is exempt from the
    second. DaemonSet and mirror pods are exempt from both -- they go with the
    node. The caller passes only Running pods no DaemonSet owns (the
    `pods_by_node` index in `check_idle_nodepool`), so the one exemption
    tested here is the mirror pod, which that index keeps.

    Two more rules pin a node whatever the namespace: a pod annotated
    `safe-to-evict: "false"`, and a bare pod no controller would recreate.
    §3.8 reports both outside `SYSTEM_NS`, so they are listed here only inside
    it -- including on a `kube-system` pod a PDB covers, which clears the first
    rule and neither of these -- and never counted twice.

    §3.8 reports the workload half of this as findings of its own and skips
    `SYSTEM_NS` deliberately, because a GKE-managed add-on is not something the
    operator can annotate: addon-manager reverts the edit. That makes them
    unactionable as findings but decisive as evidence -- they are the
    difference between "lower the floor to zero" quietly doing nothing and
    "delete the pool" actually reclaiming it.
    """
    blockers = []
    for pod in pods:
        meta = pod.get("metadata") or {}
        if _pod_is_mirror(pod):
            continue  # static, goes with the node
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        labels = meta.get("labels") or {}
        annotations = meta.get("annotations") or {}
        if _safe_to_evict(annotations) is True:
            continue
        has_pdb = any(_selector_matches(sel, ns, labels) for sel in pdb_selectors)
        if ns == "kube-system" and not has_pdb:
            blockers.append(f"{ns}/{name} (kube-system, no PDB)")
        elif _blocking_local_storage(pod):
            blockers.append(f"{ns}/{name} (local storage, not safe-to-evict)")
        elif not _is_system_namespace(ns):
            continue  # §3.8's to report
        elif _safe_to_evict(annotations) is False:
            blockers.append(f'{ns}/{name} (system namespace, safe-to-evict "false")')
        elif not meta.get("ownerReferences"):
            blockers.append(f"{ns}/{name} (system namespace, no controller)")
    return blockers


def check_scaledown_blocked(context: dict, idle_pool_hits: list[dict]) -> list[dict]:
    flagged_nodes: set[str] = set()
    for hit in idle_pool_hits:
        flagged_nodes |= hit.get("_node_names", set())
    if not flagged_nodes:
        return []

    # One finding per node, carrying its worst blocker: a node pinned for good
    # by one pod is `critical` whichever pod the listing happens to put first.
    by_node: dict[str, dict] = {}
    for pod in context["pods"]:
        node_name = (pod.get("spec") or {}).get("nodeName", "")
        if node_name not in flagged_nodes or by_node.get(node_name, {}).get("severity") == "critical":
            continue
        ns = pod.get("metadata", {}).get("namespace", "")
        if _is_system_namespace(ns):
            continue
        # A finished pod holds nothing on the node, and DaemonSet and mirror
        # pods go with it; the autoscaler skips all three when it drains.
        if (pod.get("status") or {}).get("phase") in POD_TERMINAL_PHASES:
            continue
        if _pod_daemonset_owned(pod) or _pod_is_mirror(pod):
            continue
        owners = pod.get("metadata", {}).get("ownerReferences") or []
        annotations = pod.get("metadata", {}).get("annotations") or {}
        evictable = _safe_to_evict(annotations)
        volumes = (pod.get("spec") or {}).get("volumes") or []
        # `has_local_storage` is the raw shape the excerpt reports; the verdict
        # reads `blocking_local_storage`, which applies the autoscaler's rules.
        has_local_storage = any(EMPTY_DIR_VOLUME in v or HOST_PATH_VOLUME in v for v in volumes)
        blocking_local_storage = _blocking_local_storage(pod)

        # A PDB is not a reason this check reports -- obtainability-audit's
        # 3.3/3.4 own it -- but it is not a reason to skip the pod either. A
        # PDB-selected pod that is bare, carries local storage or is annotated
        # `safe-to-evict: "false"` still pins the node after the PDB is fixed,
        # and one empty-selector PDB covers every pod in its namespace. §3.8
        # withholds the finding only where the PDB is the *only* blocker, and
        # `unevictable` below never counts a PDB.
        bare_pod = not owners
        unevictable = evictable is False or ((bare_pod or blocking_local_storage) and evictable is not True)
        if not unevictable:
            continue

        # §3.8: permanent only when nothing will ever reschedule the pod. A
        # controller recreates its pod elsewhere once someone deletes it, so
        # `safe-to-evict: "false"` on a controlled pod is `major`.
        permanent = bare_pod and (blocking_local_storage or evictable is False)
        if node_name in by_node and not permanent:
            continue  # the node already carries a blocker at least this bad
        pod_name = pod.get("metadata", {}).get("name", "")
        # The raw annotation, not the parse: the excerpt is evidence, and a
        # reader checking it against `kubectl get pod -o yaml` needs to see what
        # is really on the object.
        by_node[node_name] = {
            "object": f"Node/{node_name}",
            "excerpt": f"pod {ns}/{pod_name} blocks drain (ownerReferences={'none' if bare_pod else 'set'}, safe-to-evict={annotations.get(SAFE_TO_EVICT_ANNOTATION)}, local-storage={has_local_storage}, safe-to-evict-local-volumes={annotations.get(SAFE_TO_EVICT_LOCAL_VOLUMES_ANNOTATION)})",
            "severity": "critical" if permanent else "major",
        }
    return list(by_node.values())


# --------------------------------------------------------------------------- #
# 3.9 terminal-pods
# --------------------------------------------------------------------------- #


def _job_finished_at(status: dict) -> str:
    """When a Job without a `completionTime` last transitioned to a terminal
    condition.

    A failed Job never gets a `completionTime`, so the condition list is the
    only timestamp there -- and it is a *list*, appended to in the order the
    controller set each condition. `conditions[0]` is therefore whichever one
    was set first, which on a suspended-then-failed Job is `Suspended` and on a
    Job the controller has since un-suspended is a condition whose
    `lastTransitionTime` is when it stopped applying. Both dated the Job's
    death to the wrong moment, and 3.9's whole gate is "terminal for >= 7 days".
    """
    candidates = [
        c.get("lastTransitionTime", "")
        for c in status.get("conditions") or []
        if c.get("type") in JOB_TERMINAL_CONDITIONS and c.get("status") == "True"
    ]
    return max((c for c in candidates if c), default="")


def check_terminal_pods(context: dict, *, now: datetime) -> list[dict]:
    terminal = [p for p in context["pods"] if (p.get("status") or {}).get("phase") in POD_TERMINAL_PHASES]
    by_ns: dict[str, list[dict]] = {}
    for pod in terminal:
        ns = pod.get("metadata", {}).get("namespace", "")
        if _is_system_namespace(ns):
            continue
        labels = pod.get("metadata", {}).get("labels") or {}
        if any(k.startswith(p) for k in labels for p in GC_OWNED_LABEL_PREFIXES):
            continue
        age = _age_days(pod.get("metadata", {}).get("creationTimestamp", ""), now=now)
        if age is not None and age < 1:
            continue
        by_ns.setdefault(ns, []).append(pod)

    hits = []
    total = sum(len(v) for v in by_ns.values())
    for ns, pods in by_ns.items():
        oldest = min((p.get("metadata", {}).get("creationTimestamp", "") for p in pods), default="")
        oldest_age = _age_days(oldest, now=now)
        old_enough = any((_age_days(p.get("metadata", {}).get("creationTimestamp", ""), now=now) or 0) >= TERMINAL_MIN_AGE_DAYS for p in pods)
        if len(pods) >= TERMINAL_PODS_PILE or old_enough:
            severity = "major" if len(pods) > TERMINAL_PODS_MAJOR_NS or total > TERMINAL_PODS_MAJOR_TOTAL else "minor"
            hits.append({"namespace": ns, "object": f"Namespace/{ns}", "excerpt": f"{len(pods)} terminal pods, oldest from {oldest}{_ago(oldest_age)}", "severity": severity})

    for job in context["jobs"]:
        meta, spec, status = job.get("metadata", {}), job.get("spec", {}), job.get("status", {})
        ns = meta.get("namespace", "")
        if _is_system_namespace(ns):
            continue
        if any(o.get("kind") == "CronJob" for o in (meta.get("ownerReferences") or [])):
            continue
        if any(k.startswith(p) for k in (meta.get("labels") or {}) for p in GC_OWNED_LABEL_PREFIXES):
            continue
        if spec.get("ttlSecondsAfterFinished") is not None:
            continue
        done = status.get("completionTime") or _job_finished_at(status)
        if not (status.get("succeeded") or status.get("failed")):
            continue
        age = _age_days(done, now=now)
        if age is None or age < TERMINAL_MIN_AGE_DAYS:
            continue
        hits.append({"namespace": ns, "object": f"Job/{meta.get('name', '')}", "excerpt": f"finished {done}{_ago(age)}, no ttlSecondsAfterFinished", "severity": "minor"})

    for cj in context["cronjobs"]:
        meta, spec = cj.get("metadata", {}), cj.get("spec", {})
        ns = meta.get("namespace", "")
        if _is_system_namespace(ns):
            continue
        if (spec.get("successfulJobsHistoryLimit") or 0) > CRONJOB_HISTORY_LIMIT_MAX:
            hits.append({"namespace": ns, "object": f"CronJob/{meta.get('name', '')}", "excerpt": f"successfulJobsHistoryLimit={spec.get('successfulJobsHistoryLimit')}", "severity": "minor"})
    return hits


# --------------------------------------------------------------------------- #
# 3.10 idle-namespace
# --------------------------------------------------------------------------- #


def _is_retention_key(key: str) -> bool:
    """Whether an annotation key names an owner or a retention period."""
    words = re.split(r"[-_.]", key.rsplit("/", 1)[-1].lower())
    return any(word in RETENTION_ANNOTATION_WORDS for word in words)


def check_idle_namespace(context: dict, *, now: datetime) -> list[dict]:
    """Namespaces with no Running/Pending pod that still hold a billable object.

    The pod dump is a snapshot, so what it measures is "no pod now". The age is
    the namespace's own, from `creationTimestamp`; nothing here records when its
    last pod stopped, and the excerpt says only what was read.
    """
    active_ns = {
        p.get("metadata", {}).get("namespace", "")
        for p in context["pods"]
        if (p.get("status") or {}).get("phase") in ("Running", "Pending")
    }
    pvc_gib_by_ns: dict[str, float] = {}
    for pvc in context["pvcs"]:
        ns = pvc.get("metadata", {}).get("namespace", "")
        cap = ((pvc.get("status") or {}).get("capacity") or {}).get("storage", "0")
        pvc_gib_by_ns[ns] = pvc_gib_by_ns.get(ns, 0) + _gib(cap)
    lb_ns = {s.get("metadata", {}).get("namespace", "") for s in context["services"] if (s.get("spec") or {}).get("type") == "LoadBalancer"}
    cronjob_ns = {cj.get("metadata", {}).get("namespace", "") for cj in context.get("cronjobs") or []}
    # A ResourceQuota used to be a third way in here, and it is not billable.
    # Kubernetes reserves nothing for one: it is an admission gate on the sum of
    # the requests of the pods in its namespace, it holds no capacity, no
    # scheduler consults it on behalf of anyone else, and deleting it frees
    # nothing and saves nothing. Nothing in the fleet-waste dump is cheaper to
    # keep. The arm was untested -- every case passed `resourcequotas: []` --
    # and on the reference fleet it produced exactly one finding, an empty
    # `gitops-managed` whose only object was a quota with `used` all zeroes, for
    # which the model wrote that the quota reserved "10 vCPU / 20 GiB of request
    # headroom ... that no other namespace on this Autopilot cluster can use".
    # Every clause of that is false, and it is the kind of false a cost audit
    # can least afford: a reader who checks one savings number and finds it
    # imaginary stops believing the ones that are real.

    hits = []
    for ns_obj in context["namespaces"]:
        name = ns_obj.get("metadata", {}).get("name", "")
        if _is_system_namespace(name) or name in active_ns:
            continue
        if (ns_obj.get("status") or {}).get("phase") == "Terminating":
            continue
        # Config Sync marks what it manages with annotations; Flux's
        # kustomize-controller stamps labels. Either one owns the lifecycle.
        markers = {**(ns_obj.get("metadata", {}).get("annotations") or {}), **(ns_obj.get("metadata", {}).get("labels") or {})}
        if any(k.startswith(GITOPS_SYNC_MARKER_PREFIXES) for k in markers):
            continue
        if any(_is_retention_key(k) for k in ns_obj.get("metadata", {}).get("annotations") or {}):
            continue
        # A CronJob's namespace is empty between fires by design, and a
        # suspended one is waiting to be resumed; either way the pods it has
        # not started yet are not evidence of abandonment.
        if name in cronjob_ns:
            continue
        age = _age_days(ns_obj.get("metadata", {}).get("creationTimestamp", ""), now=now)
        if age is None or age < IDLE_NAMESPACE_MIN_AGE_DAYS:
            continue
        billable = name in lb_ns or pvc_gib_by_ns.get(name, 0) > 0
        if not billable:
            continue
        # Floor the capacity for the same reason `_whole_days` floors an age:
        # this number sits next to a threshold. A namespace holding one
        # 102000Mi PVC is 99.6 GiB, which `:.0f` printed as "100 GiB" while the
        # severity gate -- reading the raw value -- graded it `minor`, so the
        # excerpt asserted the threshold in the same sentence the severity
        # denied it. Gate on the floored number too: `floor(x) >= 100` and
        # `x >= 100` are the same test, and reading one value twice is what
        # makes the two impossible to disagree again.
        gib = pvc_gib_by_ns.get(name, 0)
        whole_gib = int(gib)
        # "<1" rather than "0" when there really is a claim: flooring a 500Mi
        # PVC to "0 GiB of PVCs" would deny the storage that made the namespace
        # billable three lines up. A namespace billable only through a
        # LoadBalancer holds no PVCs and still prints "0", which the excerpt
        # names the LoadBalancer alongside so the reason is never absent.
        gib_text = str(whole_gib) if whole_gib or not gib else "<1"
        severity = "major" if name in lb_ns or whole_gib >= LARGE_VOLUME_GIB else "minor"
        hits.append(
            {
                "object": f"Namespace/{name}",
                "excerpt": f"no Running/Pending pods now; namespace created {_whole_days(age)}d ago; holds {'a LoadBalancer Service, ' if name in lb_ns else ''}{gib_text} GiB of PVCs",
                "severity": severity,
            }
        )
    return hits


# --------------------------------------------------------------------------- #
# 3.1 overrequest (usage-sampling)
# --------------------------------------------------------------------------- #


def _owner_key(owners: list[dict]) -> tuple[str, str] | None:
    """The controller that owns a pod, as `(kind, name)`.

    `ownerReferences[0]` is not it. The API guarantees at most one entry with
    `controller: true` and says nothing about the order of the rest, so a pod
    carrying a second, non-controlling reference -- which is how several
    operators mark ownership alongside the ReplicaSet -- could aggregate under
    either one, and under a different one next week. That moves the finding's
    `object`, and §5 is explicit that a `check`/`cluster`/`namespace`/`object`
    that moves is announced as fixed one week and re-announced as new the next.
    Sorting the non-controller fallback keeps the answer stable even where no
    reference claims to be the controller.
    """
    controllers = [o for o in owners if o.get("controller")]
    pick = controllers[0] if controllers else min(owners, key=lambda o: (o.get("kind", ""), o.get("name", "")), default=None)
    if not pick:
        return None
    return (pick.get("kind", ""), pick.get("name", ""))


def _sizing_owner(meta: dict) -> tuple[str, str]:
    """The object whose manifest declares this pod's request.

    `_owner_key` answers a different question -- who controls this pod -- and
    for anything a Deployment runs the answer is a ReplicaSet. A ReplicaSet is
    the wrong object for a sizing finding twice over. Its name carries the
    pod-template hash, so the next edit to the pod template rolls a new one and
    the finding's `object` moves with it: §5 reads a moved `object` as the old
    finding resolved and a new one appearing, so a controller that is still
    over-requested is announced fixed on the run that changes anything about
    it. And §3.1's remediation is "the controller's complete desired manifest,
    taken from its declaration in the GitOps repo" -- a repo declares
    `Deployment/web`, never `ReplicaSet/web-74d7c4f678`, so the manifest the
    remediation asks for does not exist under the name the finding gives.

    The hash is the pod's own `pod-template-hash` label, which the Deployment
    controller sets on the ReplicaSet and its pods and nothing else sets, so
    stripping it off the end of the ReplicaSet's name gives the Deployment
    exactly rather than by inference. A ReplicaSet created directly -- no
    Deployment above it -- carries no such label and is left alone, which is
    correct: there it really is the declared object.
    """
    owners = meta.get("ownerReferences") or []
    key = _owner_key(owners)
    if key is None:
        return ("Pod", meta.get("name", ""))
    kind, name = key
    if kind != "ReplicaSet":
        return key
    suffix = str((meta.get("labels") or {}).get("pod-template-hash") or "")
    if suffix and name.endswith("-" + suffix):
        return ("Deployment", name[: -(len(suffix) + 1)])
    return key


def _declares_cpu_or_memory(requests: list[dict]) -> bool:
    """Whether any container's requests name a nonzero CPU or memory.

    The partition between the sized checks and 3.12 turns on this, and it used
    to turn on whether a `requests` dict was non-empty: a container requesting
    only `nvidia.com/gpu`, only `ephemeral-storage`, or `cpu: "0"` read as
    sized, every sizing check then dropped it for having no CPU or memory to
    compare, and 3.12 never saw it. A declared zero is unsized here but not
    in `collect.py`'s `no-requests`, which asks only whether the key is
    present, so 3.12 can fire on a pod that audit passes; its excerpt says "no
    nonzero CPU or memory request" rather than that none was declared.
    """
    for request in requests:
        cpu = parse_cpu_cores(str(request.get("cpu") or ""))
        mem = parse_mem_mib(str(request.get("memory") or ""))
        if (cpu or 0) > 0 or (mem or 0) > 0:
            return True
    return False


def _eligible_pods_by_owner(context: dict, *, now: datetime) -> dict[tuple, dict]:
    """Pods whose declared request is a sizing decision somebody here can change.

    Shared by both sizing checks, so that a pod excluded from one is excluded
    from the other. The two ask opposite questions of the same number, and an
    exclusion that applied to only one of them would let this stream tell a
    controller to shrink a request it had already declined to judge.

    The exclusions, and what each is for: `SYSTEM_NS` and DaemonSets are not
    request values an operator owns (Google's addon manager reverts the first,
    and the second is per-node overhead rather than a sizing choice); a pod
    under an hour old has a cold cache and a JIT still warming; `Job`-owned
    pods are periodic by design and whatever window catches them is the wrong
    one; a pod with no request at all is `obtainability-audit`'s `no-requests`,
    which owns the request's absence and defers its value to here.
    """
    by_owner: dict[tuple, dict] = {}
    for pod in context["pods"]:
        meta, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        if _is_system_namespace(ns):
            continue
        # `Terminating` is not a phase: deletion shows as `deletionTimestamp`.
        # A Failed pod (an eviction, typically) is no replica either.
        if status.get("phase") in NOT_A_REPLICA_PHASES or meta.get("deletionTimestamp"):
            continue
        age = _age_days(status.get("startTime", ""), now=now)
        if age is not None and age < POD_SETTLE_DAYS:
            continue
        owners = meta.get("ownerReferences") or []
        if any(o.get("kind") in ("Job",) for o in owners):
            continue
        if any(o.get("kind") == "DaemonSet" for o in owners):
            continue
        # Native sidecars included: the usage these requests are set against
        # sums them (`_sizing_containers`).
        containers, init_containers = _sizing_containers(spec)
        sidecars = [str(c.get("name") or "") for c in containers[len(spec.get("containers") or []):]]
        requests = [(c.get("resources") or {}).get("requests") or {} for c in containers]
        limits = [(c.get("resources") or {}).get("limits") or {} for c in containers]
        if not _declares_cpu_or_memory(requests):
            continue  # obtainability-audit's `no-requests` owns this
        # Namespaced, because `(kind, name)` is not unique within a cluster: a
        # Deployment or StatefulSet owns pods under its bare name, so the same
        # chart installed into two namespaces produces one merged entry, and
        # the finding names whichever namespace sorted first while summing both
        # namespaces' requests and usage into its numbers. No collision exists
        # on the sixteen-cluster fleet today; this keeps one from being
        # silently wrong later.
        kind, owner_name = _sizing_owner(meta)
        entry = by_owner.setdefault((ns, kind, owner_name), {"ns": ns, "pods": [], "oldest_h": None, "labels": {}})
        # Plain init containers take no part in the sizing sums above, but
        # kubelet counts them for QoS: an init container without limits makes
        # the pod Burstable. `_is_guaranteed` reads them.
        init_requests = [(c.get("resources") or {}).get("requests") or {} for c in init_containers]
        init_limits = [(c.get("resources") or {}).get("limits") or {} for c in init_containers]
        entry["pods"].append({"ns": ns, "name": name, "containers": [str(c.get("name") or "") for c in containers], "requests": requests, "limits": limits, "init_requests": init_requests, "init_limits": init_limits, "sidecars": sidecars})
        # For `check_idle_workload`'s Service join. Replicas of one controller
        # share the selector labels by construction, so the first pod's set
        # answers for the controller; a later pod merges in rather than
        # replacing, so a rollout mid-flight cannot narrow it.
        entry["labels"].update(meta.get("labels") or {})
        # A measurement is only as long as the pod that reported it. Monitoring
        # keeps a week of history, but a Deployment rolled an hour ago has an
        # hour of it, and a finding that says "over the trailing 168h" about
        # that controller is claiming to have watched something that did not
        # exist. Carry the longest-lived pod's age so the excerpt can state the
        # window it really measured.
        if age is not None:
            entry["oldest_h"] = max(entry["oldest_h"] or 0.0, age * HOURS_PER_DAY)
    return by_owner


def _measured_over(
    oldest_h: float | None, *, replaced: int = 0, controller_h: float | None = None, kind: str = ""
) -> tuple[int, str]:
    """The window a controller was really measured over, and how to say it.

    `USAGE_WINDOW_HOURS` is what the Monitoring read asks for. What bounds the
    answer is how far back a pod of this controller reported, and there are two
    of those bounds because there are two pod populations.

    Without `replaced`, the series in hand are the live pods' names, and for
    most kinds that is the live pods' history: the bound is the longest-lived
    one's age and a finding may not claim more. A kind in
    `SAME_NAME_RECREATION_KINDS` is the exception. A StatefulSet recreates
    `db-0` as `db-0`, and the read groups by pod name, so the live name's
    series already holds every earlier incarnation and the bound is the
    controller's age, as it is with `replaced`. The read collapses each series
    to one point, so when that history starts is not in the answer; the
    controller's creation is the bound it cannot exceed.

    With `replaced`, `_observed_pod_keys` found series from pods this controller
    has since rolled away, so the read reaches back past every live pod -- as
    far as the controller itself, which is where this clamps instead. The
    distinction is not cosmetic: on 2026-09-06 nine of the twenty-two cost
    findings claimed a 6h window because Argo CD's pods had rolled that morning,
    while the controllers were weeks old and the earlier pods' series were
    sitting unread in the same response.

    Both numbers reach the reader, from different directions, and until this
    existed neither said anything about the other. `evidence.command` records
    the read verbatim and so carries `window=168h`; the excerpt carried "over
    the trailing 12h". The 2026-09-06 cost report published three findings that
    way -- a reader checking the evidence had the query in one hand, a different
    number in the other, and no way to reconcile them but to guess which was
    wrong.

    Returns `(window_h, phrase)`. The phrase is the shared clause of all three
    sizing excerpts, so a check appends its own punctuation.
    """
    if replaced and controller_h is not None:
        window_h = max(1, min(USAGE_WINDOW_HOURS, round(controller_h)))
        pods = f"{replaced} pod{'s' if replaced != 1 else ''} it has replaced since"
        # Say which bound stopped it. A controller younger than the read is a
        # different claim from one the read simply could not see further back
        # than, and only the first is a caveat on the finding.
        short = (
            ""
            if window_h >= USAGE_WINDOW_HOURS
            else f", which is this controller's whole life -- the read covers {USAGE_WINDOW_HOURS}h"
        )
        return window_h, (
            f"over the trailing {window_h}h (Cloud Monitoring, across this "
            f"controller's live pods and the {pods}{short})"
        )
    if replaced:
        # A kind whose own age is not in the dump -- a bare ReplicaSet. The
        # peak came partly from pods that are gone, so the live pods' age is
        # not the bound either; say what the read asked and what is unknown.
        pods = f"{replaced} pod{'s' if replaced != 1 else ''} it has replaced"
        return USAGE_WINDOW_HOURS, (
            f"over up to the trailing {USAGE_WINDOW_HOURS}h (Cloud Monitoring, "
            f"across this controller's live pods and the {pods}; the "
            f"controller's own age was not read, so its history may be shorter)"
        )
    if kind in SAME_NAME_RECREATION_KINDS:
        if controller_h is None:
            return USAGE_WINDOW_HOURS, (
                f"over up to the trailing {USAGE_WINDOW_HOURS}h (Cloud Monitoring; a "
                f"{kind} recreates its pods under the same names, so their series span "
                f"earlier incarnations, and the controller's own age was not read)"
            )
        window_h = max(1, min(USAGE_WINDOW_HOURS, round(controller_h)))
        short = (
            ""
            if window_h >= USAGE_WINDOW_HOURS
            else f", which is this controller's whole life -- the read covers {USAGE_WINDOW_HOURS}h"
        )
        return window_h, (
            f"over the trailing {window_h}h (Cloud Monitoring; a {kind} recreates "
            f"its pods under the same names, so their series span every incarnation{short})"
        )
    window_h = USAGE_WINDOW_HOURS if oldest_h is None else min(USAGE_WINDOW_HOURS, round(oldest_h))
    if window_h >= USAGE_WINDOW_HOURS:
        return window_h, f"over the trailing {window_h}h (Cloud Monitoring)"
    cycle = ", under a full daily cycle" if window_h < SHORT_OBSERVATION_HOURS else ""
    return window_h, (
        f"over the trailing {window_h}h (Cloud Monitoring: the read covers "
        f"{USAGE_WINDOW_HOURS}h, and this controller's oldest pod started "
        f"{window_h}h ago{cycle})"
    )


def _controller_hours(context: dict, kind: str, ns: str, name: str, now: datetime) -> float | None:
    """`_controller_age_days` in the unit `_measured_over` bounds a window in."""
    age_days = _controller_age_days(context, kind, ns, name, now=now)
    return None if age_days is None else age_days * HOURS_PER_DAY


def _live_pod_owners(context: dict) -> dict[tuple[str, str], tuple[str, str]]:
    """Every pod in the dump and the controller that owns it, unfiltered.

    Deliberately not `_eligible_pods_by_owner`: this is the exclusion list
    `_observed_pod_keys` checks a name-pattern match against, so it has to cover
    the pods the sizing checks skip -- a DaemonSet's, a Job's, a system
    namespace's. A pattern that reaches one of those must lose to the live
    dump's answer rather than to an absence in a filtered copy of it.
    """
    owners: dict[tuple[str, str], tuple[str, str]] = {}
    for pod in context.get("pods") or []:
        meta = pod.get("metadata") or {}
        owners[(meta.get("namespace", ""), meta.get("name", ""))] = _sizing_owner(meta)
    return owners


def _observed_pod_keys(
    entry: dict, kind: str, name: str, metric_keys: Any, live_owners: dict
) -> tuple[list[tuple[str, str]], int]:
    """Every pod of this controller the metric read saw, live or since replaced.

    Returns `(keys, replaced_count)`, `keys` always starting with the live pods
    so a caller that only wants those can still find them.

    The join this widens was the quiet half of the measurement bug. The read
    asks Cloud Monitoring for a week and gets a week -- keyed by pod name, so a
    controller rolled this morning has its earlier pods' series right there in
    the response, under names no longer in the cluster. Matching only the live
    names discarded them and then reported the remainder as the week's peak.
    Measured on `kube-agents-host` on 2026-09-06: `litellm`'s live pods topped
    out at 1101 and 1164 MiB, while a pod they had replaced reached 1656 MiB,
    and `argocd-dex-server` read 0.3m of CPU against a predecessor's 3.6m.

    Two guards keep a pattern from claiming a pod that is not replaced. It
    must not be a live pod at all -- `live_owners` is the cluster's own answer
    and outranks any inference from a name, and a live pod of this controller
    that the sizing checks skipped is live, not replaced. And a controller kind absent
    from `REPLACED_POD_PATTERNS` is not widened at all, because a bare `Pod` and
    the kinds this does not model name their pods by rules this does not know.
    """
    live = [(pod["ns"], pod["name"]) for pod in entry["pods"]]
    template = REPLACED_POD_PATTERNS.get(kind)
    if template is None:
        return live, 0
    ns = entry["ns"]
    seen = set(live)
    pattern = re.compile(template.format(name=re.escape(name)))
    extra = [
        key
        for key in metric_keys
        if key not in seen
        and key[0] == ns
        and pattern.match(key[1])
        # A pod still in the dump is not replaced, whoever owns it: another
        # controller's, or this one's that the sizing checks skipped (under an
        # hour old, terminating, Pending).
        and key not in live_owners
    ]
    return live + sorted(extra), len(extra)


def _per_replica(keys: list[tuple[str, str]], series: dict, index: int | None = None) -> float | None:
    """The worst single replica's figure, across every pod that occupied a slot.

    `max`, not the mean, and not the sum. Sum is what the join produced before
    it was widened and it cannot survive the widening: seven pod names for a
    one-replica Deployment would total seven replicas of usage. Mean understates
    the hot replica, which is the one a request has to cover -- the checks that
    divide by `replicas` to size a manifest are asking exactly this question.

    Widening therefore forces the aggregation change; they are not separable.
    Both directions the totals move are the safe ones: a higher peak makes
    §3.1 and §3.13 propose fewer shrinks and §3.12 propose larger requests.

    `index` picks a field out of a `(cpu, mem)` tuple; `None` reads the value
    whole, which is `fetch_memory_means`' shape. CPU and memory are maximised
    independently, and may well come from different pods -- the same choice
    `_read_pod_series` already makes when folding a pod's containers together.

    `None` when no key carried a figure -- for a tuple, on that dimension --
    which is "unmeasured", never zero. Zero is the most idle a workload can
    read, so an absent series read as zero clears every idle test there is.
    """
    best = None
    for key in keys:
        value = series.get(key)
        if value is not None and index is not None:
            value = value[index]
        if value is None:
            continue
        best = value if best is None else max(best, value)
    return best


def _missing_usage_dimension(usage_peaks: dict) -> tuple[str, str] | None:
    """`(missing, present)` when one metric of a non-empty usage answer
    carried no series on any pod, else `None`.

    `fetch_usage_peaks` refuses only an answer empty on both metrics. One
    empty beside the other leaves every pod unmeasured on that dimension, and
    `_measured_peaks` then skips every controller -- so the three checks that
    read it would be recorded as run clean over a cluster nothing measured.
    """
    if not usage_peaks:
        return None
    for (name, index), (other, _) in zip(USAGE_DIMENSIONS, reversed(USAGE_DIMENSIONS)):
        if all(value[index] is None for value in usage_peaks.values()):
            return name, other
    return None


def _measured_peaks(
    keys: list[tuple[str, str]], usage_peaks: dict, replicas: int
) -> tuple[float, float] | None:
    """The controller's `(peak_cpu, peak_mem)` totals, or `None` when either
    dimension went unmeasured across every one of its pods."""
    cpu = _per_replica(keys, usage_peaks, 0)
    mem = _per_replica(keys, usage_peaks, 1)
    if cpu is None or mem is None:
        return None
    return cpu * replicas, mem * replicas


def _resize_target(
    peak_total: float,
    replicas: int,
    *,
    floor: float,
    unit: float,
    multiplier: float = OVERREQUEST_PEAK_MULTIPLIER,
) -> float:
    """The per-replica request §3.1's resize would write: `ceil(peak x 2)`,
    clamped up to `50m` / `64Mi`.

    Split out of `_resize_shrinks_request`, which computed this number to
    answer a yes/no question and then discarded it. The excerpt needs the
    number itself: left to infer it, the model reports one it worked out from
    the rounded peak the excerpt quotes, and the 2026-09-06 cost report asked
    for `about 10m` on a dimension whose floor is `50m`.

    §3.11 sizes at 1.3x rather than 2x and calls this with
    `multiplier=UNDERREQUEST_PEAK_MULTIPLIER`. The ceil and the per-replica
    division are the same in both directions, and stating them twice is how
    the two halves of one edit drift apart.
    """
    # Rounded before the ceil: `0.27 / 3 * 2 / 0.001` is 180.00000000000003,
    # and ceiling float noise asks for a millicore the peak never needed.
    return max(floor, math.ceil(round(peak_total / replicas * multiplier / unit, RESIZE_CEIL_DIGITS)) * unit)


def _resize_shrinks_request(
    request_total: float, peak_total: float, replicas: int, *, floor: float, unit: float
) -> bool:
    """Would §3.1's resize actually lower this dimension's request?

    The remediation is `ceil(peak x 2)` per replica, clamped up to `50m` /
    `64Mi`. Where that arithmetic lands on the request the controller already
    declares, the dimension is idle but not reclaimable: no manifest edit
    closes it, and the model, following the rule correctly, answers "already at
    the sizing floor". The 2026-09-05 cost report carried three findings that
    were nothing but this -- `hello-world` on `adam-new-cluster`,
    `adamparco-gitops` and `ap-ap-deploy-test`, each 50m/64Mi per replica and
    each graded `major` by the Autopilot bump.

    This is not the materiality floor relaxing. That floor asks whether the
    *request* is large enough to be worth reporting and deliberately ignores
    the reclaimable delta, because a 199m delta on a 200m request is worth
    taking. This asks whether a reclaimable delta exists at all once the resize
    floor is applied -- a strictly narrower question.

    In practice only that floor can make the answer "no". A dimension reaches
    here having passed `peak <= 0.2 * request`, so `2 x peak` is at most 40% of
    the request and always shrinks it. The `ceil` is applied rather than
    assumed away so this stays a faithful evaluation of the rule if the idle
    ratio or either floor moves.

    `unit` is the smallest amount a manifest can express -- a millicore for
    CPU, a MiB for memory -- which is what the `ceil` rounds up to. Both totals
    are summed across the controller's pods; the edit is per replica.
    """
    if replicas <= 0:
        return False
    request_per_replica = request_total / replicas
    target = _resize_target(peak_total, replicas, floor=floor, unit=unit)
    return _clearly_exceeds(request_per_replica, target)


def _clearly_exceeds(larger: float, smaller: float) -> bool:
    """`larger > smaller` by more than a relative hair. A relative tolerance,
    because a request is compared as a per-replica quotient of a sum: three
    pods of `50m` total 0.15000000000000002, and a third of that is a hair over
    0.05. An exact `>` would call that hair a reclaimable delta and report
    every three-replica controller sitting on the floor -- the exact shape
    `_resize_shrinks_request` exists to drop."""
    return larger - smaller > max(larger, smaller) * PER_REPLICA_RELATIVE_TOLERANCE


def _below_resize_floor(request_total: float, replicas: int, *, floor: float) -> bool:
    """Whether the per-replica request sits under §3.1's resize floor, with
    the tolerance `_resize_shrinks_request` uses, so a three-way split of
    `50m` is on the floor rather than a hair beside it."""
    if replicas <= 0:
        return False
    request_per_replica = request_total / replicas
    return _clearly_exceeds(floor, request_per_replica)


def _is_guaranteed(entry: dict) -> bool:
    """Whether this controller's pods are `Guaranteed` QoS: requests == limits.

    Per container, as kubelet decides it: every container needs a CPU and a
    memory limit, and a request that is set must equal its limit. A sidecar
    with no resources, or a missing memory limit, makes the pod Burstable
    however the totals add up.

    Read by `check_overrequest`, which refuses to propose a resize here, and by
    `check_idle_workload`, which needs the same verdict because that refusal is
    what hands it the finding. Spelt once so the two cannot drift into either
    reporting one controller twice or dropping it between them.

    Init containers count as well, as they do for kubelet: an unlimited
    init container or native sidecar leaves the pod Burstable.
    """
    for pod in entry["pods"]:
        requests = pod["requests"] + pod.get("init_requests", [])
        limits = pod["limits"] + pod.get("init_limits", [])
        for req, lim in zip(requests, limits):
            for resource, parse in (("cpu", parse_cpu_cores), ("memory", parse_mem_mib)):
                limit = parse(str(lim.get(resource, "")))
                if not limit:
                    return False
                # An unset request defaults to the limit, which is still Guaranteed.
                if resource in req and parse(str(req[resource])) != limit:
                    return False
    return bool(entry["pods"])


def _idle_on_every_dimension(
    cpu_req: float, peak_cpu: float, mem_req: float, peak_mem: float
) -> bool:
    """§3.13's idleness bar: under the ratio on every dimension declared.

    Every, not either. A workload using none of its CPU and all of its memory
    is doing something, and §3.1 is the check that owns one oversized
    dimension. A dimension the controller does not declare is not a vote.
    """
    return all(
        peak / req <= IDLE_WORKLOAD_UTILISATION
        for req, peak in ((cpu_req, peak_cpu), (mem_req, peak_mem))
        if req
    )


def _stands_down_instead_of_resizing(
    context: dict,
    kind: str,
    ns: str,
    name: str,
    *,
    guaranteed: bool,
    cpu_req: float,
    peak_cpu: float,
    mem_req: float,
    peak_mem: float,
    now: datetime,
) -> bool:
    """Whether §3.13 takes this controller off §3.1's hands.

    §3.1 declines to resize a `Guaranteed` controller at all -- on such a pod
    the request *is* the limit, so a number derived from a week of observed
    idleness becomes an enforcement ceiling, and the SOP has it emit `manual`
    with the reason. That refusal was leaving the fully idle ones with nowhere
    to go. §3.13 would not take them either, because its partition asks whether
    a resize is *arithmetically* available rather than whether §3.1 will make
    one, and above the 50m/64Mi floor the arithmetic says yes. The result was a
    controller idle for a fortnight getting prose from one check and silence
    from the other, when the edit §3.13 proposes -- `spec.replicas: 0` -- is
    both available and free of the hazard §3.1 is avoiding: standing a workload
    down sets no ceiling, because there is no pod left for a ceiling to bind.

    Observed on 2026-09-06: `ai-inference-hardened` and `ai-inference-unsafe`
    on `adamparco-gitops`, each `Guaranteed` at 0.50 vCPU / 2.0 GiB and each
    peaking at 0.00 vCPU over the trailing week, both declared in the GitOps
    repo and both published `kind: manual`.

    So this is the one case where §3.1 yields. It is deliberately the narrow
    one: idle on *every* dimension, past §3.13's age bar, and `Guaranteed`. A
    `Guaranteed` controller idle on one dimension only stays §3.1's, where the
    `manual` note is the right answer -- nothing is unused, one number is
    wrong, and no stand-down is warranted.
    """
    if not guaranteed:
        return False
    age_days = _controller_age_days(context, kind, ns, name, now=now)
    if age_days is None or age_days < IDLE_WORKLOAD_MIN_AGE_DAYS:
        return False
    return _idle_on_every_dimension(cpu_req, peak_cpu, mem_req, peak_mem)


def check_overrequest(context: dict, usage_peaks: dict, *, now: datetime, autopilot: bool) -> list[dict]:
    """`usage_peaks` is `fetch_usage_peaks`'s `(ns, pod) -> (cores, MiB)`.

    It used to be a list of three `kubectl top` samples, and the flag rule
    used to be "every sample agrees usage is under 20% of requests, and the
    reclaimable delta is measured against the highest of them". One peak over
    a week is the same rule with the sampling error taken out: the max across
    samples *is* the peak, and a run of samples all agreeing is exactly the
    condition that the peak clears the bar.
    """
    if not usage_peaks:
        return []
    by_owner = _eligible_pods_by_owner(context, now=now)
    live_owners = _live_pod_owners(context)
    hpa_targets = _hpa_targets(context)
    lr_defaults = _limitrange_defaults(context)

    hits = []
    for (_ns, kind, name), entry in by_owner.items():
        if (entry["ns"], kind, name) in hpa_targets:
            continue
        if _requests_are_the_namespace_default(entry, lr_defaults.get(entry["ns"], {})):
            continue
        defaulted = _namespace_defaulted_dimensions(entry, lr_defaults.get(entry["ns"], {}))
        cpu_req_total = mem_req_total = 0.0
        for pod in entry["pods"]:
            for req in pod["requests"]:
                cpu_req_total += parse_cpu_cores(str(req.get("cpu", "0"))) or 0
                mem_req_total += parse_mem_mib(str(req.get("memory", "0"))) or 0
        if cpu_req_total == 0 and mem_req_total == 0:
            continue
        replicas = len(entry["pods"])
        guaranteed = _is_guaranteed(entry)

        keys, replaced = _observed_pod_keys(entry, kind, name, usage_peaks, live_owners)
        # Same guard `check_underrequest` carries, and this is the direction in
        # which getting it wrong is worse. A controller none of whose pods
        # reported would read as zero on both dimensions, which is not "idle"
        # but "unmeasured" -- and zero is the most idle a workload can read, so
        # it clears both ratio tests at once and the finding proposes shrinking
        # the request of a workload nobody observed. The `if not usage_peaks`
        # return above only catches a cluster that answered nothing at all;
        # one namespace missing from an otherwise-populated answer, or a
        # metrics agent down on a single node, lands here instead. A controller
        # measured on one dimension only is skipped whole: this check's verdict
        # and its excerpt state both dimensions, and one of them is unknown.
        peaks = _measured_peaks(keys, usage_peaks, replicas)
        if peaks is None:
            continue
        peak_cpu, peak_mem = peaks
        # The one case this check yields to §3.13. A `Guaranteed` controller
        # idle on every dimension gets a stand-down there rather than the
        # `manual` note here, because the note is all this check can offer it
        # and a pull request is available. `_stands_down_instead_of_resizing`
        # holds the argument and both halves of the partition.
        if _stands_down_instead_of_resizing(
            context,
            kind,
            entry["ns"],
            name,
            guaranteed=guaranteed,
            cpu_req=cpu_req_total,
            peak_cpu=peak_cpu,
            mem_req=mem_req_total,
            peak_mem=peak_mem,
            now=now,
        ):
            continue
        # Per dimension, not across both. Requiring CPU *and* memory to be idle
        # together made the common shape invisible: a controller sized for its
        # memory footprint and given a copy-pasted CPU request uses ~all of the
        # memory and ~none of the CPU, so the memory ratio vetoed the finding
        # and the 40x CPU over-request was never reported. Each dimension is a
        # separate sizing decision and gets a separate verdict; only the idle
        # one contributes to the reclaimable delta, so a finding never proposes
        # shrinking a request the workload is actually consuming.
        # A dimension the LimitRange filled in gets no verdict at all.
        cpu_unused = "cpu" not in defaulted and bool(cpu_req_total) and peak_cpu / cpu_req_total <= IDLE_WORKLOAD_UTILISATION
        mem_unused = "memory" not in defaulted and bool(mem_req_total) and peak_mem / mem_req_total <= IDLE_WORKLOAD_UTILISATION
        if not (cpu_unused or mem_unused):
            continue
        # Idle is not the same as reclaimable. §3.1 resizes a request to
        # `ceil(peak x 2)` per replica but never below `50m` / `64Mi`, so a
        # dimension already on that floor has a recommendation identical to
        # what it declares. Narrowing here rather than at the end means the
        # materiality floor, the reclaimable delta, the severity and the
        # excerpt all read the same verdict: a controller with nothing to give
        # back on any dimension is dropped, and one with a floor-bound
        # dimension alongside a shrinkable one reports only the shrinkable one.
        cpu_idle = cpu_unused and _resize_shrinks_request(
            cpu_req_total, peak_cpu, replicas, floor=OVERREQUEST_RESIZE_FLOOR_VCPU, unit=1 / MILLICORES_PER_CORE
        )
        mem_idle = mem_unused and _resize_shrinks_request(
            mem_req_total, peak_mem, replicas, floor=OVERREQUEST_RESIZE_FLOOR_MIB, unit=1.0
        )
        if not (cpu_idle or mem_idle):
            continue

        # The floor is a property of the request, not of the delta, and only an
        # idle dimension can satisfy it: a workload consuming all 8 GiB it asked
        # for and none of its 10m of CPU must not clear a materiality test on
        # the strength of the memory it is using. It is also applied per
        # dimension, as the resize floor above is: a dimension idle but under
        # the materiality floor is left out of the delta, the severity, the
        # both-dimensions sentence and the Resize-to prescription alike, so a
        # finding carried by its other dimension does not also ask to shrink it.
        cpu_reclaimable, mem_reclaimable = cpu_idle, mem_idle
        cpu_idle = cpu_idle and cpu_req_total >= OVERREQUEST_FLOOR_VCPU
        mem_idle = mem_idle and mem_req_total / MIB_PER_GIB >= OVERREQUEST_FLOOR_GIB
        if not (cpu_idle or mem_idle):
            continue
        delta_cpu = (cpu_req_total - peak_cpu) if cpu_idle else 0.0
        delta_mem_gib = ((mem_req_total - peak_mem) / MIB_PER_GIB) if mem_idle else 0.0

        _, measured_over = _measured_over(
            entry["oldest_h"], replaced=replaced, controller_h=_controller_hours(context, kind, entry["ns"], name, now), kind=kind
        )
        severity = "major" if delta_cpu >= NODE_WORTH_VCPU or delta_mem_gib >= NODE_WORTH_GIB else "minor"
        # Recorded, not just applied: a grade the bump supplied is held out of
        # the automatic sweep (`AUTOPILOT_BUMP_TRIAGE`), and one the delta
        # earned on its own is not.
        bumped = autopilot and severity == "minor"
        if bumped:
            severity = "major"

        # Name the over-requested dimensions in the excerpt. The remediation
        # resizes only these -- SOP 3.1 sizes a request at 2x the observed
        # peak, and applying that to a dimension running at 96% of its request
        # would halve a request the workload is using. The excerpt is the
        # channel because `adopt_collector_evidence` overwrites whatever the
        # model wrote with it, so this sentence is the one part of the finding
        # guaranteed to reach the reviewer intact.
        dimensions = [d for d, on in (("cpu", cpu_idle), ("memory", mem_idle)) if on]
        # The per-replica request each idle dimension has to end up at. Stated
        # rather than left to the reader for two reasons. The model cannot
        # recompute it from this excerpt -- the peaks above are rounded to two
        # decimals of a vCPU and one of a GiB, which is `0.00 vCPU / 0.0 GiB`
        # for anything small -- and the 2026-09-06 report duly asked for
        # `about 10m` on a dimension whose floor is `50m`. And `ceil(peak x 2)`
        # is not the whole rule: the clamp is what decides the number on every
        # near-idle workload, and a rule quoted without its clamp reads as
        # though the answer were `1m`.
        targets = {
            "cpu": f"{_resize_target(peak_cpu, replicas, floor=OVERREQUEST_RESIZE_FLOOR_VCPU, unit=1 / MILLICORES_PER_CORE) * MILLICORES_PER_CORE:.0f}m",
            "memory": f"{_resize_target(peak_mem, replicas, floor=OVERREQUEST_RESIZE_FLOOR_MIB, unit=1.0):.0f}Mi",
        }
        measured = f"peak observed {peak_cpu:.2f} vCPU / {peak_mem / MIB_PER_GIB:.1f} GiB {measured_over}"
        # Every number above is summed across the controller's pods, because
        # that is what the fleet is actually paying for -- but the remediation
        # edits one container's request in one manifest, and §3.1 sizes it at
        # 2x the observed peak. A reader handed only the total sets a
        # three-replica Deployment's request to 3x what it needs. The excerpt
        # is the only channel -- `_emit` whitelists the keys it forwards, and
        # `adopt_collector_evidence` overwrites the model's own wording with
        # this string -- so the arithmetic has to be spelled out here.
        excerpt = f"requests {cpu_req_total:.2f} vCPU / {mem_req_total / MIB_PER_GIB:.1f} GiB; {measured}"
        if len(dimensions) == 1:
            over = dimensions[0]
            at = (peak_cpu / cpu_req_total) if over == "cpu" else (peak_mem / mem_req_total)
            # Two reasons a dimension can be left out, and they call for
            # opposite handling. "In use" means resizing it would take away
            # something the workload consumes. "On the floor" means it is idle
            # too, but `ceil(peak x 2)` clamps to what it already declares, so
            # there is nothing to take. Saying "in use" of a floor-bound
            # dimension would be a false statement about the workload, and the
            # excerpt is the one part of the finding `adopt_collector_evidence`
            # guarantees reaches the reviewer.
            # A third reason: the other dimension is the namespace LimitRange's
            # default, which gets no verdict here at all -- idle or not, it is
            # fixed in the LimitRange, and "in use" would be false of it.
            # A fourth: no request declared at all, where "in use" would be
            # a verdict about a dimension nothing was measured against.
            other, other_unused = ("memory", mem_unused) if over == "cpu" else ("cpu", cpu_unused)
            other_reclaimable = mem_reclaimable if over == "cpu" else cpu_reclaimable
            other_req_total = mem_req_total if over == "cpu" else cpu_req_total
            if other in defaulted:
                why = "the namespace LimitRange default, which is fixed in the LimitRange rather than resized here"
            elif not other_req_total:
                why = "not requested, so there is nothing to resize"
            elif other_unused and _below_resize_floor(
                other_req_total,
                replicas,
                floor=OVERREQUEST_RESIZE_FLOOR_VCPU if other == "cpu" else OVERREQUEST_RESIZE_FLOOR_MIB,
            ):
                # Idle and not reclaimable covers a request under the floor as
                # well as one on it, and "at the floor" is false of the first.
                why = "already below the 50m/64Mi sizing floor, where a resize would raise it rather than reduce it"
            elif other_reclaimable:
                # Idle and shrinkable, but its request is under the
                # materiality floor, so the finding does not ask for it.
                why = "idle, but its request is under the 100m/128Mi materiality floor and is not worth a resize"
            elif other_unused:
                why = "already at the 50m/64Mi sizing floor and cannot be reduced further"
            else:
                why = "in use and must not be resized"
            excerpt += f". Over-requested on {over} only ({at * 100:.0f}% of request); {other} is {why}"
        # Both dimensions idle used to say nothing at all: the branch above
        # fires only at `len(dimensions) == 1`, so the finding that most needs
        # the instruction was the one that went without it. A resize of one
        # dimension leaves the other satisfying the check on its own and the
        # finding republishes next week, against a repository whose manifest
        # now shows the merged PR -- which reads as the audit ignoring a fix.
        # `github-token-minter` on 2026-09-06 was exactly this: idle on both,
        # remediated on CPU alone, and still over-requested on the 0.12 GiB the
        # recommendation called "close enough to in-use to leave alone" at 9%
        # of request.
        elif len(dimensions) > 1:
            excerpt += (
                ". Over-requested on both dimensions"
                f" (cpu at {peak_cpu / cpu_req_total * 100:.0f}% of request,"
                f" memory at {peak_mem / mem_req_total * 100:.0f}%);"
                " resizing one alone does not clear this finding"
            )
        # "per replica" only where there is more than one, which is the same
        # rule the sentence below follows: on a single-replica controller the
        # qualifier distinguishes nothing and reads as though it did.
        excerpt += ". Resize to " + " and ".join(f"{d} {targets[d]}" for d in dimensions)
        if replicas > 1:
            excerpt += " per replica"
            excerpt += (
                f". Totals span {replicas} replicas — per replica that is "
                f"{cpu_req_total / replicas:.3f} vCPU / {mem_req_total / replicas / MIB_PER_GIB:.2f} GiB requested "
                f"against a {peak_cpu / replicas:.3f} vCPU / {peak_mem / replicas / MIB_PER_GIB:.2f} GiB peak, "
                f"and the manifest change is per replica"
            )
        # The usage behind the target sums every running container, native
        # sidecars included, so the figure is the pod's. Written on the app
        # container alone it would carry the sidecar's share as well.
        sidecars = sorted({s for pod in entry["pods"] for s in pod.get("sidecars") or () if s})
        if sidecars:
            excerpt += (
                ". That is the pod's total, native sidecar "
                + ", ".join(f"`{s}`" for s in sidecars)
                + " included: split it across the containers rather than writing it on one"
            )
        excerpt += "."

        hits.append(
            {
                "namespace": entry["ns"],
                "object": f"{kind}/{name}",
                "excerpt": excerpt,
                "severity": severity,
                "_guaranteed": guaranteed,
                "_autopilot_bumped": bumped,
            }
        )
    return hits


def _controller_age_days(context: dict, kind: str, ns: str, name: str, *, now: datetime) -> float | None:
    """How long the controller itself has existed, or `None` if it is not in
    the dump.

    Distinct from the oldest pod's age, which is all `_eligible_pods_by_owner`
    can see. A pod's age measures the last node upgrade as often as it measures
    the workload: the four Deployments `check_idle_workload` reports have run
    untouched for a month behind pods under eight days old. The sizing checks
    are right to clamp their *measurement window* to the pod, since that is how
    much history exists -- but "has anyone used this since it was created" is a
    question about the controller.
    """
    pool = {"Deployment": "deployments", "StatefulSet": "statefulsets"}.get(kind)
    if not pool:
        return None
    for obj in context.get(pool) or []:
        meta = obj.get("metadata") or {}
        if meta.get("namespace", "") == ns and meta.get("name", "") == name:
            return _age_days(meta.get("creationTimestamp", ""), now=now)
    return None


def _is_internal_load_balancer(meta: dict) -> bool:
    """Whether either GKE internal-load-balancer annotation is set on this Service.

    A copy of collect.py's, as `release_declarations` is."""
    annotations = meta.get("annotations") or {}
    return any(annotations.get(key) == INTERNAL_LB_ANNOTATION_VALUE for key in INTERNAL_LB_ANNOTATIONS)


def _internal_load_balancers(context: dict, ns: str) -> set[str]:
    """`Service/<name>` for every internal LoadBalancer Service in `ns`."""
    return {
        f"Service/{(svc.get('metadata') or {}).get('name', '')}"
        for svc in context["services"]
        if (svc.get("spec") or {}).get("type") == "LoadBalancer"
        and (svc.get("metadata") or {}).get("namespace", "") == ns
        and _is_internal_load_balancer(svc.get("metadata") or {})
    }


def _fronting_load_balancers(context: dict, ns: str, labels: dict) -> list[str]:
    """LoadBalancer Services in `ns` whose selector this controller's pods match.

    Kubernetes' own rule: a selector matches when every one of its pairs is
    present on the pod, and an empty selector selects nothing here (a Service
    with no selector is fed by hand-written Endpoints and is not this
    controller's).
    """
    matched = []
    for svc in context["services"]:
        spec = svc.get("spec") or {}
        if spec.get("type") != "LoadBalancer":
            continue
        meta = svc.get("metadata") or {}
        if meta.get("namespace", "") != ns:
            continue
        selector = spec.get("selector") or {}
        if selector and all(labels.get(k) == v for k, v in selector.items()):
            matched.append(f"Service/{meta.get('name', '')}")
    return sorted(matched)


def _selecting_services(context: dict, ns: str, labels: dict) -> list[str]:
    """Every Service in `ns` whose selector this controller's pods match.

    A deliberate copy of `_fronting_load_balancers` with the `LoadBalancer`
    test removed, rather than a widening of it. That function's answer feeds
    §3.13's severity, where `LoadBalancer` is the whole point -- the forwarding
    rule and external IP are the charge that lifts the finding to `major`.
    Widening it in place would grade every `ClusterIP`-backed idle workload
    `major` and promote it, which is the reverse of what this is for.

    What this answers is a different question: does anything in the cluster
    have a stable name to call this workload by. A `ClusterIP` Service is not
    a cost -- it is free -- but it is the thing that makes a stand-down
    somebody else's outage rather than a reclaimed reservation.
    """
    matched = []
    for svc in context["services"]:
        spec = svc.get("spec") or {}
        meta = svc.get("metadata") or {}
        if meta.get("namespace", "") != ns:
            continue
        selector = spec.get("selector") or {}
        if selector and all(labels.get(k) == v for k, v in selector.items()):
            matched.append(f"Service/{meta.get('name', '')}")
    return sorted(matched)


def _fronting_addresses(context: dict, ns: str, fronting: list[str]) -> list[str]:
    """The external addresses the Services in `fronting` were actually given.

    `status`, not `spec`: `spec.loadBalancerIP` is a request, and a Service
    still waiting on one has none. An address here is one GCP assigned, which
    is the only kind a forwarding rule can be joined to.
    """
    wanted = set(fronting)
    found = set()
    for svc in context["services"]:
        meta = svc.get("metadata") or {}
        if meta.get("namespace", "") != ns:
            continue
        if f"Service/{meta.get('name', '')}" not in wanted:
            continue
        for ingress in ((svc.get("status") or {}).get("loadBalancer") or {}).get("ingress") or []:
            if isinstance(ingress, dict) and ingress.get("ip"):
                found.add(str(ingress["ip"]))
    return sorted(found)


def _sum_or_none(values) -> float | None:
    """The sum, or None when any value is None: one unmeasured term leaves the
    total unknown rather than understated."""
    total = 0.0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _idle_traffic_clause(
    context: dict, ns: str, fronting: list[str], lb_traffic: dict | None
) -> str:
    """What the forwarding rules in front of an idle controller metered.

    The sentence this check owed a reader and did not have. §3.13 used to
    publish "a workload nobody is calling" off a CPU and memory read, and on
    2026-09-07 three findings carrying that phrase merged a scale-to-zero
    unattended -- two of them on rules that had metered several hundred
    thousand packets in the same window the finding quoted. `d1b8fd23`
    retracted the claim. This supplies the measurement it was standing in for.

    Five answers, and the distinction between unmeasured and quiet is the one
    that matters. A rule with figures gets them, with the mean payload per
    outbound packet, because packet counts alone cannot tell a session from a
    port scan and the payload can. A rule Monitoring holds no series for is
    reported as unmeasured, never as quiet, and so is a rule above the floor
    whose outbound series is missing (LB_EGRESS_UNMEASURED), because without
    it the payload cannot be computed. And an address this run knows nothing about --
    no session, no rules read, a Service still waiting for an IP -- gets no
    clause at all, because silence is honest and a zero is not.
    """
    if lb_traffic is None or not fronting:
        return ""
    entries = [
        lb_traffic[address]
        for address in _fronting_addresses(context, ns, fronting)
        if address in lb_traffic
    ]
    if not entries:
        return ""
    measured = [entry for entry in entries if entry.get("ingress_packets") is not None]
    if not measured:
        subject = (
            "Its forwarding rule"
            if len(entries) == 1
            else f"The {len(entries)} forwarding rules in front of it"
        )
        return (
            f". {subject} carries no Cloud Monitoring traffic series over the "
            f"trailing {USAGE_WINDOW_HOURS}h, so what reached "
            f"{'it' if len(entries) == 1 else 'them'} is unmeasured rather than zero"
        )
    # Some rules measured and some not: the figures below speak for the
    # measured ones only, and the rest are named as unmeasured. Folding them
    # into "the N rules metered ..." read an unmeasured rule as a quiet one,
    # the zero `fetch_lb_traffic` refuses to write within one address.
    unmeasured = len(entries) - len(measured)
    if not unmeasured:
        subject = "Its forwarding rule" if len(entries) == 1 else f"The {len(entries)} forwarding rules in front of it"
    else:
        subject = f"{len(measured)} of the {len(entries)} forwarding rules in front of it"
    unmeasured_clause = (
        f"; the other {unmeasured} carr{'ies' if unmeasured == 1 else 'y'} no Cloud "
        f"Monitoring traffic series, so what reached "
        f"{'it' if unmeasured == 1 else 'them'} is unmeasured rather than zero"
        if unmeasured
        else ""
    )
    ingress = sum(entry["ingress_packets"] for entry in measured)
    # The two egress counters are read independently of ingress, and a rule
    # with no series under one of them is unmeasured on that figure, not zero:
    # "answered none" or "0 bytes each" would be a reply this run never saw.
    egress_packets = _sum_or_none(entry.get("egress_packets") for entry in measured)
    egress_bytes = _sum_or_none(entry.get("egress_bytes") for entry in measured)
    metered = (
        f". {subject} metered {ingress:,.0f} inbound packet"
        f"{'' if ingress == 1 else 's'} over {USAGE_WINDOW_HOURS}h"
    )
    if ingress < LB_TRAFFIC_MIN_PACKETS:
        floor = (
            f"{metered}, under the {LB_TRAFFIC_MIN_PACKETS:,}-packet floor this "
            f"check treats as the background an exposed address collects on its "
            f"own"
        )
        if unmeasured:
            return floor + unmeasured_clause
        return f"{floor} -- nothing measurable reached it"
    if egress_packets is None:
        return f"{metered}; {LB_EGRESS_UNMEASURED.format(what='what it answered')}{unmeasured_clause}"
    if not egress_packets:
        return f"{metered} and answered none of them{unmeasured_clause}"
    if egress_bytes is None:
        return (
            f"{metered} and answered with {egress_packets:,.0f} outbound packets; "
            f"{LB_EGRESS_UNMEASURED.format(what='their payload')}{unmeasured_clause}"
        )
    per_packet = egress_bytes / egress_packets
    answered = (
        f"{metered} and answered with {egress_bytes:,.0f} bytes across "
        f"{egress_packets:,.0f} outbound packets, {per_packet:,.0f} bytes each"
    )
    if per_packet < LB_TRAFFIC_PAYLOAD_BYTES_PER_PACKET:
        return (
            f"{answered} -- under the {LB_TRAFFIC_PAYLOAD_BYTES_PER_PACKET}-byte "
            f"mark a served response clears, so the rule was busy without ever "
            f"sending a payload, which is the shape of unsolicited connection "
            f"attempts to a public address rather than of sessions{unmeasured_clause}"
        )
    return (
        f"{answered}, which is enough payload that something is being served{unmeasured_clause}. "
        f"Find out what before standing the controller down: this check "
        f"measured a forwarding rule, not a caller"
    )


def check_idle_workload(
    context: dict, usage_peaks: dict, *, now: datetime, lb_traffic: dict | None = None
) -> list[dict]:
    """A controller nothing is using, whose request no resize can lower.

    The complement of `check_overrequest`, and the population `bc437731`
    deliberately silenced. That commit stopped publishing a sizing finding
    whose own remediation is a no-op, which was right: a Deployment already at
    the 50m/64Mi admission floor has no resize to make, and three such findings
    a week teach a reader to skip the section. What it left behind is the
    reason they kept appearing -- four `hello-world`-class Deployments on the
    2026-09-06 fleet, 28 to 35 days old, each holding 50m/64Mi per replica and
    peaking at 0.2m to 2.1m of CPU and 3 to 6 MiB of memory. Between 0.5% and
    4% of a reservation, for a month, on workloads whose names say what they
    are. The audit measured all four correctly and then said nothing about any
    of them, because the only question it knew how to ask was "what should this
    request be" and the honest answer was "exactly what it already is".

    So this check asks the other question. Its bar is not "could the request be
    smaller" but "is anything using this at all", and where the answer is no
    the remediation is deleting the manifest -- which, unlike the resize, is a
    pull request somebody can merge.

    The partition with §3.1 is exact and load-bearing, and it turns on whether
    §3.1 *will* propose a resize rather than on whether one is arithmetically
    available. Usually those are the same thing and this fires only where
    `_resize_shrinks_request` is false on every declared dimension, or true
    only on one whose request is under §3.1's materiality floor. The
    exception is a `Guaranteed` controller, where §3.1 declines the resize it
    could compute, because on such a pod the request is also the limit;
    `_stands_down_instead_of_resizing` carries that argument and §3.1 skips
    exactly what this admits. Either way no controller produces both findings,
    so the report never asks a reader to both shrink and stand down one object.
    """
    if not usage_peaks:
        return []
    live_owners = _live_pod_owners(context)
    lr_defaults = _limitrange_defaults(context)
    hpa_targets = _hpa_targets(context)
    hits = []
    for (_ns, kind, name), entry in _eligible_pods_by_owner(context, now=now).items():
        # §3.1's HPA exclusion holds here too, for a sharper reason: the HPA
        # cannot hold its target below `minReplicas`, so a merged
        # `replicas: 0` is scaled straight back up on its next sync.
        if (entry["ns"], kind, name) in hpa_targets:
            continue
        # A controller the dump does not carry -- anything but a Deployment or
        # a StatefulSet -- has no age to test, and this check does not guess one
        # from its pods. Skipping is the safe direction: the cost of staying
        # quiet is another month of a 50m reservation, and the cost of being
        # wrong is a recommendation to delete something.
        age_days = _controller_age_days(context, kind, entry["ns"], name, now=now)
        if age_days is None or age_days < IDLE_WORKLOAD_MIN_AGE_DAYS:
            continue
        cpu_req = mem_req = 0.0
        for pod in entry["pods"]:
            for req in pod["requests"]:
                cpu_req += parse_cpu_cores(str(req.get("cpu", "0"))) or 0
                mem_req += parse_mem_mib(str(req.get("memory", "0"))) or 0
        if cpu_req == 0 and mem_req == 0:
            continue
        replicas = len(entry["pods"])

        keys, replaced = _observed_pod_keys(entry, kind, name, usage_peaks, live_owners)
        # Same guard as §3.1, and it matters more here. Zero is the most idle a
        # workload can read, and an unmeasured controller would read as zero on
        # both dimensions -- so without this, a metrics agent down on one node
        # produces a recommendation to delete whatever was running there. Idle
        # on every dimension needs every dimension measured.
        peaks = _measured_peaks(keys, usage_peaks, replicas)
        if peaks is None:
            continue
        peak_cpu, peak_mem = peaks

        if not _idle_on_every_dimension(cpu_req, peak_cpu, mem_req, peak_mem):
            continue
        # The partition with §3.1. Anything shrinkable belongs there -- unless
        # §3.1 refuses to shrink it, which is what `Guaranteed` means here, or
        # the shrinkable dimension is the namespace LimitRange's default, which
        # §3.1 gives no verdict.
        # A shrinkable dimension under §3.1's materiality floor is dropped
        # there, so it stays here: deferring on the resize floor alone lost a
        # 50m/100Mi controller idle for a month to both checks.
        guaranteed = _is_guaranteed(entry)
        defaulted = _namespace_defaulted_dimensions(entry, lr_defaults.get(entry["ns"], {}))
        cpu_shrinks = "cpu" not in defaulted and _resize_shrinks_request(
            cpu_req, peak_cpu, replicas, floor=OVERREQUEST_RESIZE_FLOOR_VCPU, unit=1 / MILLICORES_PER_CORE
        )
        mem_shrinks = "memory" not in defaulted and _resize_shrinks_request(
            mem_req, peak_mem, replicas, floor=OVERREQUEST_RESIZE_FLOOR_MIB, unit=1.0
        )
        if not guaranteed and (
            (cpu_shrinks and cpu_req >= OVERREQUEST_FLOOR_VCPU)
            or (mem_shrinks and mem_req / MIB_PER_GIB >= OVERREQUEST_FLOOR_GIB)
        ):
            continue

        _, measured_over = _measured_over(
            entry["oldest_h"], replaced=replaced, controller_h=age_days * HOURS_PER_DAY, kind=kind
        )
        # Why no resize is on the table, which is the reader's first question
        # and has three answers. Naming the wrong one would send them to
        # check a floor the manifest is nowhere near. A sub-material dimension
        # beside a LimitRange-defaulted one is both answers at once, so the
        # sentence names each dimension with its own.
        shrinking = [dim for dim, shrinks in (("CPU", cpu_shrinks), ("memory", mem_shrinks)) if shrinks]
        defaulted_names = [label for dim, label in (("cpu", "CPU"), ("memory", "memory")) if dim in defaulted]
        also_defaulted = (
            f", and the {' and '.join(defaulted_names)} request is the namespace LimitRange "
            f"default, which is fixed in the LimitRange rather than here"
            if defaulted_names
            else ""
        )
        no_resize = (
            "Requests and limits are equal, so any resize would lower the "
            "enforcement ceiling with them -- a sizing observation is not a "
            "safe limit, which is why no resize is offered"
            if guaranteed
            else f"The {' and '.join(shrinking)} request is under the 100m / 128Mi a resize is worth "
            f"proposing for{also_defaulted}, so no resize is offered"
            if shrinking
            else "Every dimension is already at or below the 50m/64Mi floor or is the "
            "namespace LimitRange default, which is fixed in the LimitRange "
            "rather than here, so no resize of this workload can reclaim any of it"
            if defaulted
            else "Every dimension is already at or below the 50m/64Mi floor, so no "
            "resize can reclaim any of it"
        )
        excerpt = (
            f"requests {cpu_req:.3f} vCPU / {mem_req:.0f} MiB across {replicas} "
            f"replica{'s' if replicas != 1 else ''}; peak observed "
            f"{peak_cpu * MILLICORES_PER_CORE:.1f}m vCPU / {peak_mem:.1f} MiB {measured_over}. "
            f"Declared {_whole_days(age_days)} days ago. {no_resize}"
        )
        # The reservation is the smaller half of the bill. A LoadBalancer
        # Service in front of an unused Deployment holds a forwarding rule and
        # an external IP, which on this fleet cost several times the pod. The
        # 2026-09-06 report priced three of these at roughly $17/month of
        # Autopilot pod charges while the three L4 rules fronting them ran
        # about $66 -- and no check joined the two, so the larger number
        # appeared in neither the finding nor its severity.
        fronting = _fronting_load_balancers(context, entry["ns"], entry["labels"])
        if fronting:
            # An internal load balancer has a forwarding rule and no external
            # IP, so the excerpt names only what each Service really holds.
            internal = _internal_load_balancers(context, entry["ns"]) & set(fronting)
            if not internal:
                held = "a forwarding rule and its external IP bill"
            elif len(internal) == len(fronting):
                held = "an internal forwarding rule bills"
            else:
                held = "forwarding rules, and external IPs for the external ones, bill"
            excerpt += (
                f". {' and '.join(fronting)} still front{'s' if len(fronting) == 1 else ''} it, "
                f"so {held} for it too — usually several times the pod's own cost"
            )
        # Immediately after the cost clause and before the endpoints one,
        # because it is the same rule's story: what it costs, then what it
        # carried, then what depends on it. Empty unless a rule was both found
        # and read -- see `_idle_traffic_clause` for why an unmeasured rule
        # still gets a sentence and an unknown one does not.
        excerpt += _idle_traffic_clause(context, entry["ns"], fronting, lb_traffic)
        # Cost is not the only thing a Service says about an idle workload, and
        # the other thing it says is the one that takes a serving system down.
        # A `ClusterIP` Service costs nothing, so it never reaches the clause
        # above -- but it is a stable name something else in the cluster may
        # still be calling, and a stand-down empties it of endpoints. This
        # check has never measured a call, so it names what selects the pods
        # and stops there; `needs_triage` below is what keeps the sweep from
        # deciding for a reader.
        selecting = _selecting_services(context, entry["ns"], entry["labels"])
        if selecting:
            unnamed = [svc for svc in selecting if svc not in set(fronting)]
            if unnamed:
                clause = (
                    f"{' and '.join(unnamed)} also select"
                    f"{'s' if len(unnamed) == 1 else ''} its pods"
                )
            elif len(selecting) == 1:
                clause = "That Service selects its pods"
            else:
                clause = "Those Services select its pods"
            excerpt += (
                f". {clause}, so standing the controller down leaves "
                f"{'it' if len(selecting) == 1 else 'them'} with no endpoints; this "
                f"check measured CPU and memory, not whether anything still resolves "
                f"{'that name' if len(selecting) == 1 else 'those names'}"
            )
        excerpt += "."

        hits.append(
            {
                "namespace": entry["ns"],
                "object": f"{kind}/{name}",
                "excerpt": excerpt,
                # Dropped by `_emit`'s fixed key set, read by the emit loop in
                # `collect_cluster` -- the same handoff `_guaranteed` uses.
                "_selected_by": selecting,
                # A floor-bound request is small by construction, so on that
                # arm the pod alone never justifies more than `minor`. The load
                # balancer does: it is the larger charge, and unlike the pod it
                # is reachable from the internet while nothing uses it. The
                # `Guaranteed` arm has no such construction -- those requests
                # are whatever the author wrote, and the two on the live fleet
                # hold 0.50 vCPU / 2.0 GiB each -- so it is graded on the size
                # of the reservation, against the same floors §3.1 uses to
                # decide a request is worth reporting at all.
                "severity": "major"
                if fronting
                or (
                    guaranteed
                    and (
                        cpu_req >= OVERREQUEST_FLOOR_VCPU
                        or mem_req / MIB_PER_GIB >= OVERREQUEST_FLOOR_GIB
                    )
                )
                else "minor",
            }
        )
    return hits


def _underrequest_target(pods: list[dict]) -> tuple[str, float, float | None]:
    """The container §3.11's raise lands on, what the rest of the pod requests,
    and that container's own memory limit.

    The mean is read per pod, so the prescription is a pod figure, but
    admission compares each container's request with its own limit: a pod
    of a 2Gi main and a 512Mi sidecar has a 2560Mi summed limit, and a 2470Mi
    prescription that fits it is still rejected once it is written on the
    main container. So the raise is named on one container -- the one with
    the largest memory request, which is the one a reader would pick -- sized
    as the pod figure less the other containers' requests, and compared with
    that container's limit alone.

    Across the controller's pods the smallest of the others' requests and the
    smallest of the container's limits, so a rollout mid-way leaves the
    request no smaller and the limit clause no quieter than either revision
    needs. The name is "" for a single-container pod, which needs no naming,
    and the limit None when no pod declares one on that container.
    """
    first = pods[0]
    names = first.get("containers") or [""] * len(first["requests"])
    sizes = [parse_mem_mib(str(req.get("memory") or "")) or 0 for req in first["requests"]]
    # Never a native sidecar: the overage is the app's to carry, and the
    # sidecar's own request is among the others' subtracted below, so the raise
    # does not hand its usage to the app container either.
    sidecars = set(first.get("sidecars") or ())
    app = [(size, cname) for cname, size in zip(names, sizes) if cname not in sidecars]
    target = max(app, key=lambda pair: pair[0])[1] if app else ""
    others: list[float] = []
    limits: list[float] = []
    for pod in pods:
        pod_names = pod.get("containers") or [""] * len(pod["requests"])
        other = 0.0
        for cname, req, lim in zip(pod_names, pod["requests"], pod["limits"]):
            mem_req = parse_mem_mib(str(req.get("memory") or "")) or 0
            if cname == target:
                mem_lim = parse_mem_mib(str(lim.get("memory") or "")) or 0
                if mem_lim > 0:
                    limits.append(mem_lim)
            else:
                other += mem_req
        others.append(other)
    return (target if len(names) > 1 else ""), min(others), (min(limits) if limits else None)


def check_underrequest(context: dict, usage_peaks: dict, memory_means: dict, *, now: datetime) -> list[dict]:
    """The other half of `check_overrequest`: a memory request sized too small.

    `memory_means` is `fetch_memory_means`' `(ns, pod) -> mean_mem_mib`. The
    mean is load-bearing. A pod whose *peak* memory exceeds its request is
    doing what Burstable QoS is for; a pod whose *mean* exceeds it has a
    request that does not describe the workload, and it stays permanently at
    the top of kubelet's eviction ranking, which orders Burstable pods by how
    far usage sits above the request.

    Memory only. A CPU request below actual usage is throttling under
    contention, which is a performance question and recoverable; memory is not
    reclaimable, so the same mistake ends in an eviction or an OOMKill.
    """
    if not memory_means:
        return []
    by_owner = _eligible_pods_by_owner(context, now=now)
    live_owners = _live_pod_owners(context)

    hits = []
    for (_ns, kind, name), entry in by_owner.items():
        mem_req_total = mem_lim_total = 0.0
        for pod in entry["pods"]:
            for req in pod["requests"]:
                mem_req_total += parse_mem_mib(str(req.get("memory", "0"))) or 0
            for lim in pod["limits"]:
                mem_lim_total += parse_mem_mib(str(lim.get("memory", "0"))) or 0
        if mem_req_total <= 0:
            continue  # obtainability-audit's `no-requests` owns a missing request
        # The mean is summed over the pod's containers, and a container with no
        # memory request adds usage with no request to set it against -- so the
        # overage would land on whichever container did declare one.
        # A declared zero counts as none, as `_declares_cpu_or_memory` reads it:
        # a sidecar's `memory: "0"` sets nothing against its own usage either.
        if any(not (parse_mem_mib(str(req.get("memory") or "")) or 0) > 0 for pod in entry["pods"] for req in pod["requests"]):
            continue

        replicas = len(entry["pods"])
        keys, replaced = _observed_pod_keys(entry, kind, name, memory_means, live_owners)
        # `max` over the pods, per `_per_replica`, and the mean wants it as much
        # as the peak does: the question is whether *a* replica sustains more
        # than the request it was given, and the worst one answers it. A pod
        # that lived a tenth of the window carries its mean while alive, not a
        # tenth of it -- `ALIGN_MEAN` averages the points that exist rather than
        # padding the gaps with zeroes.
        mean_per_replica = _per_replica(keys, memory_means)
        # A controller none of whose pods reported is not a controller using no
        # memory. Reading absent pods as zero would read it as comfortably
        # under its request, which is the same vacuum-as-evidence mistake
        # `fetch_usage_peaks` refuses at the cluster level.
        if mean_per_replica is None:
            continue
        mean_mem = mean_per_replica * replicas
        # The peak only sizes the new request and is printed beside the mean.
        # Unread, the mean stands in for it -- a peak is never below the mean
        # it contains -- rather than a zero that would size the raise at the
        # 64Mi floor under a request already being exceeded.
        peak_per_replica = _per_replica(keys, usage_peaks, 1)
        peak_mem = mean_mem if peak_per_replica is None else peak_per_replica * replicas
        peak_text = "peak not read" if peak_per_replica is None else f"peak {peak_mem / MIB_PER_GIB:.2f} GiB"

        overage = mean_mem - mem_req_total
        if overage <= 0 or overage < UNDERREQUEST_FLOOR_MIB:
            continue

        _, measured_over = _measured_over(
            entry["oldest_h"], replaced=replaced, controller_h=_controller_hours(context, kind, entry["ns"], name, now), kind=kind
        )
        # `critical` is reserved for the case that is already failing rather
        # than merely mis-scheduled: sustained usage within 10% of the ceiling
        # is an OOMKill waiting for one more request. Everything else is
        # `major`, and there is deliberately no Autopilot bump -- this stream
        # already grades most of what it finds at the ceiling, and a severity
        # every finding shares stops ordering any of them.
        #
        # Every container must declare a memory limit for the sum to be a
        # ceiling anything enforces. Admission and the OOM killer are per
        # container, so a pod pairing a limited sidecar with an unlimited main
        # container -- every default Istio injection -- has a `mem_lim_total`
        # binding neither. Grading against it published `critical` off the
        # sidecar's 1Gi while the unlimited container held the memory, and a
        # `manifest` fix graded at or above `AUTO_PROMOTION_FLOOR` is what
        # `finish` promotes unattended.
        limited = all(
            parse_mem_mib(str(lim.get("memory", "0")))
            for pod in entry["pods"]
            for lim in pod["limits"]
        )
        near_limit = limited and mem_lim_total > 0 and mean_mem >= UNDERREQUEST_NEAR_LIMIT_FRACTION * mem_lim_total
        severity = "critical" if near_limit else "major"

        if limited and mem_lim_total > 0:
            ceiling = f"{mem_lim_total / MIB_PER_GIB:.1f} GiB limit"
        elif mem_lim_total > 0:
            ceiling = "a memory limit on only some containers"
        else:
            ceiling = "no memory limit"
        excerpt = (
            f"requests {mem_req_total / MIB_PER_GIB:.2f} GiB of memory ({ceiling}); mean observed "
            f"{mean_mem / MIB_PER_GIB:.2f} GiB {measured_over} — "
            f"{mean_mem / mem_req_total * 100:.0f}% of request, {overage / MIB_PER_GIB:.2f} GiB above it; "
            f"{peak_text}. Sustained, not a burst."
        )
        # State the prescribed request rather than leaving it to be recomputed,
        # for the reason `_resize_target` records and §3.1 already acts on: the
        # per-replica peak below is printed to two decimals of a GiB, so
        # `litellm` on 2026-09-07 reads as 1.62 GiB and 1.62 x 1.3 gives
        # 2.11 GiB where the unrounded 1655.96 MiB gives 2153Mi. The floor
        # passed here cannot bind -- the new request is 1.3x a peak that is at
        # least the mean, and the mean is already above the current request.
        new_request = _resize_target(
            peak_mem,
            replicas,
            floor=OVERREQUEST_RESIZE_FLOOR_MIB,
            unit=1.0,
            multiplier=UNDERREQUEST_PEAK_MULTIPLIER,
        )
        per_replica = " per replica" if replicas > 1 else ""
        target, others_mib, target_limit = _underrequest_target(entry["pods"])
        if target:
            new_request -= others_mib
            excerpt += (
                f" Raise the memory request of container `{target}` to {new_request:.0f}Mi{per_replica}, "
                f"leaving the others' {others_mib:.0f}Mi as it is."
            )
        else:
            excerpt += f" Raise the memory request to {new_request:.0f}Mi{per_replica}."
        # A request above its limit is rejected at admission, so a remediation
        # that raises the request and leaves the limit alone does not degrade
        # the workload -- it stops it scheduling at all. On 2026-09-07 that is
        # exactly what shipped: `litellm` on `kube-agents-host` was published
        # with a 2153Mi per-replica prescription against a 2048Mi per-replica
        # limit, and the model had no way to see it. The only limit the excerpt
        # printed was the 4.0 GiB controller total, which is twice the
        # prescription, so the comparison a reader makes from the excerpt alone
        # says it fits. Compare one replica's container here rather than asking
        # for that division to be remembered.
        # Against the named container's own limit (`_underrequest_target`),
        # never the pod's sum: admission is per container, so a sum fits
        # prescriptions one container rejects, and an unlimited sidecar beside
        # a limited main container still leaves the main one's limit binding.
        if target_limit is not None and new_request > target_limit:
            excerpt += (
                f" That exceeds the {target_limit:.0f}Mi memory limit declared"
                f"{' on that container' if target else ''}{per_replica}, and a request above its limit is rejected at admission, so "
                f"raise the limit to {new_request * UNDERREQUEST_LIMIT_MULTIPLIER:.0f}Mi in "
                f"the same edit."
            )
        # §3.11 sizes the new request at ceil(peak x 1.3), and the manifest it
        # edits declares one replica's request. Handing over only the summed
        # peak inflates a two-replica controller's request by 2x -- which is
        # the wrong direction to be wrong in for this check, since the whole
        # finding is about the scheduler's booking being inaccurate. Spell out
        # the per-replica arithmetic rather than trusting it to be redone.
        if replicas > 1:
            # No peak clause where the peak was not read: `peak_mem` is then
            # the mean, and printing it as a peak is a measurement never made.
            peak_clause = (
                "" if peak_per_replica is None else f" and a {peak_mem / replicas / MIB_PER_GIB:.2f} GiB peak"
            )
            excerpt += (
                f" Totals span {replicas} replicas — per replica that is "
                f"{mem_req_total / replicas / MIB_PER_GIB:.2f} GiB requested against a "
                f"{mean_mem / replicas / MIB_PER_GIB:.2f} GiB mean{peak_clause}, and the manifest change is per replica."
            )
        hits.append(
            {
                "namespace": entry["ns"],
                "object": f"{kind}/{name}",
                "excerpt": excerpt,
                "severity": severity,
            }
        )
    return hits


def _unsized_pods_by_owner(context: dict, *, now: datetime) -> dict[tuple, dict]:
    """The population `_eligible_pods_by_owner` drops for declaring no request.

    Every other exclusion is the same and applied in the same order, so a pod
    lands in exactly one of the two sets: a controller is either sized (and its
    value is 3.1/3.11's question) or unsized (and its value is 3.12's).
    """
    by_owner: dict[tuple, dict] = {}
    for pod in context["pods"]:
        meta, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
        ns, name = meta.get("namespace", ""), meta.get("name", "")
        if _is_system_namespace(ns):
            continue
        # `Terminating` is not a phase: deletion shows as `deletionTimestamp`.
        # A Failed pod (an eviction, typically) is no replica either.
        if status.get("phase") in NOT_A_REPLICA_PHASES or meta.get("deletionTimestamp"):
            continue
        age = _age_days(status.get("startTime", ""), now=now)
        if age is not None and age < POD_SETTLE_DAYS:
            continue
        owners = meta.get("ownerReferences") or []
        if any(o.get("kind") in ("Job",) for o in owners):
            continue
        if any(o.get("kind") == "DaemonSet" for o in owners):
            continue
        # The containers `_eligible_pods_by_owner` sizes, so a pod whose only
        # request is on a native sidecar lands in exactly one of the two.
        containers, _ = _sizing_containers(spec)
        if not containers:
            continue
        requests = [(c.get("resources") or {}).get("requests") or {} for c in containers]
        if _declares_cpu_or_memory(requests):
            continue  # sized: 3.1 and 3.11 own it
        kind, owner_name = _sizing_owner(meta)
        entry = by_owner.setdefault(
            (ns, kind, owner_name),
            {"ns": ns, "pods": [], "containers": [], "oldest_h": None},
        )
        entry["pods"].append({"ns": ns, "name": name})
        for container in containers:
            if container.get("name") and container["name"] not in entry["containers"]:
                entry["containers"].append(container["name"])
        if age is not None:
            entry["oldest_h"] = max(entry["oldest_h"] or 0.0, age * HOURS_PER_DAY)
    return by_owner


def check_unsized(context: dict, usage_peaks: dict, *, now: datetime, autopilot: bool) -> list[dict]:
    """A controller that declares no request at all, priced from its usage.

    §3.1 says it owns the request *value* and that the Workload Reliability
    audit's `no-requests` owns the request's *absence*, "deferring the sizing
    to here, where the usage history is". Nothing here accepted the handoff:
    both sizing checks skip a container with no request, so the number was
    never produced and the deferral ended nowhere. On the sixteen-cluster
    adamparco-kage fleet on 2026-09-05 that was seven of the twenty-one
    eligible controllers -- every Argo CD component on the hub, including an
    application controller peaking at 0.18 vCPU and 1.6 GiB that the scheduler
    books as zero. The reliability audit named all seven and, as designed,
    proposed no number for any of them.

    So this check states the number and nothing else. It never reports that a
    request is missing -- that finding already exists in the other stream, and
    §3.1's boundary rule is about the two audits not restating one another's
    half. There is no oscillation risk in the pair: once the request lands at
    2x the peak the workload sits at ~50% of it, which is neither idle enough
    for 3.1 nor above the mean for 3.11, and `no-requests` stops firing too.
    """
    if not usage_peaks:
        return []
    by_owner = _unsized_pods_by_owner(context, now=now)
    live_owners = _live_pod_owners(context)

    hits = []
    for (_ns, kind, name), entry in by_owner.items():
        replicas = len(entry["pods"])
        # This check has the most to lose from the narrow join and the least
        # margin for it: it writes a *request* into a manifest, so a peak read
        # off pods that rolled in this morning becomes a reservation the
        # workload outgrows by tonight. On 2026-09-06 every Argo CD component
        # on the hub was sized off a 6h window for exactly that reason.
        keys, replaced = _observed_pod_keys(entry, kind, name, usage_peaks, live_owners)
        # The whole finding is the measurement, so an unmeasured controller has
        # nothing to say. Reporting it anyway would recommend requesting zero,
        # which is the state being complained about -- and so would a
        # dimension no series was read for.
        peaks = _measured_peaks(keys, usage_peaks, replicas)
        if peaks is None:
            continue
        peak_cpu, peak_mem = peaks

        _, measured_over = _measured_over(
            entry["oldest_h"], replaced=replaced, controller_h=_controller_hours(context, kind, entry["ns"], name, now), kind=kind
        )
        # §3.1's 2x, per replica, because the manifest declares one replica's
        # request, ceiled to a whole millicore / MiB by the helper §3.1 uses so
        # the figure is never below twice the peak. Floored at the smallest
        # values worth writing into a manifest so a near-silent sidecar is not
        # handed a `1m`/`1Mi` request that no scheduler decision can turn on.
        raw_cpu = peak_cpu / replicas * OVERREQUEST_PEAK_MULTIPLIER
        raw_mem_mib = peak_mem / replicas * OVERREQUEST_PEAK_MULTIPLIER
        want_cpu = _resize_target(peak_cpu, replicas, floor=UNSIZED_FLOOR_VCPU, unit=1 / MILLICORES_PER_CORE)
        want_mem_mib = _resize_target(peak_mem, replicas, floor=UNSIZED_FLOOR_MIB, unit=1.0)
        # Autopilot bills on requests and injects its own defaults where a
        # manifest declares none, so an unsized workload there is not merely
        # unbooked -- it is being charged for a number nobody chose. Same
        # one-level bump §3.1 takes, and it stops at `major` because §3's
        # severity ceiling reserves `critical` for a drain blocker or a
        # last-copy deletion, neither of which a missing request is.
        #
        # The bumped `major` is the grade the automatic sweep opens on the
        # checks it clears for `major` (`MAJOR_SWEEP_CHECKS` in audit_report.py),
        # so the hit says the bump supplied it and the candidate carries
        # `AUTOPILOT_BUMP_TRIAGE` whichever floor applies: it
        # still waits for `/remediate`, and a *platform* attribute moves this
        # finding up the ledger without opening a pull request by itself.
        severity = "major" if autopilot else "minor"
        # §3.1's units are vCPU and GiB, and this check does not use them. The
        # workloads it finds are small by construction -- nobody forgets a
        # request on the thing sized for the cluster -- and at that end `%.3f`
        # vCPU and `%.2f` GiB round to zero. The live fleet on 2026-09-05
        # produced "peak observed 0.000 vCPU / 0.03 GiB ... sized at 0.010
        # vCPU / 68Mi", where the CPU figure reads as nothing at all and the
        # memory figure cannot be compared to its own recommendation without
        # arithmetic. So: millicores and mebibytes, the units the manifest
        # edit is written in, and the peak is directly twice-able by eye.
        peak_cpu_m, want_cpu_m = peak_cpu * MILLICORES_PER_CORE, want_cpu * MILLICORES_PER_CORE
        excerpt = (
            f"declares no nonzero CPU or memory request on "
            f"{'container' if len(entry['containers']) == 1 else 'containers'} "
            f"{', '.join(entry['containers'])}; peak observed {peak_cpu_m:.1f}m vCPU / "
            f"{peak_mem:.0f}Mi {measured_over}. "
            f"Sized at 2x the peak that is {want_cpu_m:.0f}m / {want_mem_mib:.0f}Mi per replica"
        )
        if replicas > 1:
            excerpt += (
                f" — the peak above spans {replicas} replicas, and the manifest change is per replica"
            )
        # Without this the arithmetic in the sentence above is visibly wrong:
        # `argocd-dex-server` peaks at 0.4m and is recommended 10m, and a
        # reviewer who checks the doubling finds it off by twenty-five times.
        # Read off the pre-floor values, not by comparing the floored result
        # back against a re-derivation of them: `0.09 * 3 / 3 * 2` does not
        # round-trip, and a three-replica controller nowhere near the floor
        # claimed to be sitting on it.
        floored = [
            dim
            for dim, hit in (
                ("cpu", raw_cpu < UNSIZED_FLOOR_VCPU),
                ("memory", raw_mem_mib < UNSIZED_FLOOR_MIB),
            )
            if hit
        ]
        if floored:
            names = {"cpu": f"{UNSIZED_FLOOR_VCPU * MILLICORES_PER_CORE:.0f}m", "memory": f"{UNSIZED_FLOOR_MIB:.0f}Mi"}
            excerpt += (
                f" — the {' and '.join(floored)} figure is the "
                f"{'/'.join(names[d] for d in floored)} floor rather than 2x the peak, "
                f"which is below anything worth writing into a manifest"
            )
        excerpt += "."
        hits.append(
            {
                "namespace": entry["ns"],
                "object": f"{kind}/{name}",
                "excerpt": excerpt,
                "severity": severity,
                "_autopilot_bumped": autopilot,
            }
        )
    return hits


def workload_declarations(root: Path) -> dict[tuple[str, str, str, str], set[str]]:
    """Index the Kubernetes objects a GitOps clone declares, by cluster tree.

    Maps `(cluster, kind, namespace, metadata.name)` to the set of
    clone-relative paths declaring it — a set, because two files claiming one
    object is the ambiguity `declaration_for` refuses to resolve rather than
    guessing between.

    Copied from `collect.py`, which carries the full argument for why this
    exists at all: the SOPs otherwise tell the *model* to run
    `grep -rl "name: <object>"` once per finding, and a grep that is kind-blind
    and unanchored does not decide consistently. What earns the copy here is
    that this collector already disagrees with itself. In the live fleet's
    2026-09-06 run it published three `kind: manifest` sizing findings whose
    paths this index resolves, and two `overrequest` findings marked
    `kind: manual` — "no pull request is possible" — on
    `Deployment/ai-inference-hardened` and `Deployment/ai-inference-unsafe`,
    both of which it also resolves, in the same repository, on the same run.
    A shrunk `resources.requests` is the most mechanical manifest edit this
    audit produces; leaving it `manual` for want of a file path costs a PR
    that needs no fact from outside the file.

    Returns `{}` when PyYAML is absent or the clone is unreadable, which leaves
    every candidate unannotated and the SOP's grep as the only answer -- the
    behaviour that shipped before this existed.
    """
    try:
        import yaml  # noqa: PLC0415 -- optional; absence disables the annotation
    except ImportError:
        return {}

    index: dict[tuple[str, str, str, str], set[str]] = {}
    try:
        paths = sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml"))
    except OSError:
        return {}
    for path in paths:
        # `.git` holds packed objects, not manifests, and rglob walks into it.
        if GIT_DIR_NAME in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            # A file this collector cannot parse is one it cannot make a claim
            # about. Skipping leaves those findings unannotated.
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        # Only a file under `clusters/<name>/` is applied to a known cluster.
        # Anything else -- `gcp/`, `bootstrap/`, the repo root -- is either a
        # Config Connector resource this index skips or hub infrastructure no
        # per-cluster finding should resolve to, so it is indexed under no
        # cluster and can never match: `declaration_for` requires the cluster.
        if len(parts) <= GITOPS_CLUSTER_TREE_DEPTH or parts[0] != GITOPS_CLUSTER_TREE_ROOT:
            continue
        cluster = parts[1]
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            api = str(doc.get("apiVersion") or "")
            kind = str(doc.get("kind") or "")
            if not kind or KCC_API_GROUP_SUFFIX in api:
                continue
            meta = doc.get("metadata")
            if not isinstance(meta, dict):
                continue
            name = str(meta.get("name") or "")
            namespace = str(meta.get("namespace") or "")
            if not name or not namespace:
                continue
            index.setdefault((cluster, kind, namespace, name), set()).add(str(relative))
    return index


def declaration_for(
    index: dict[tuple[str, str, str, str], set[str]],
    cluster: str,
    namespace: str,
    obj: str,
) -> dict | None:
    """Where the GitOps repo declares one candidate's object, or `None`.

    `None` means unannotated — no claim either way — and is what an absent
    index, an object outside `Kind/name` form, or a genuine miss all return.
    A hit carries `path` (the declaration itself, for a remediation that
    *changes* the object, which every sizing finding here is) and `directory`
    (its parent, for one that *creates* a sibling beside it).

    The match is exact on all four of cluster, kind, namespace and name; see
    `collect.py`'s copy for why every looser arm was measured and dropped.
    """
    kind, _, name = obj.partition("/")
    if not kind or not name or not cluster or not namespace:
        return None
    paths = index.get((cluster, kind, namespace, name))
    if not paths or len(paths) > 1:
        # Two files declaring one object is a duplicate resource id that Argo
        # and Config Sync both reject; picking one would name the wrong file
        # half the time. Say nothing and let the SOP's `manual` branch hold.
        return None
    path = next(iter(paths))
    parent = str(Path(path).parent)
    return {"path": path, "directory": parent}


def _argocd_chart_source(spec: dict) -> dict | None:
    """The one chart source on an Argo CD Application, with its field path.

    `spec.source.chart` absent is the discriminator that keeps this off the
    plain-manifest Applications a GitOps repo is mostly made of. Two chart
    entries under `spec.sources` is an ambiguity with no right answer and
    returns None, the way `declaration_for` treats two declaring files. See
    `collect._argocd_chart_source`.
    """
    single = spec.get("source")
    if isinstance(single, dict) and single.get("chart"):
        return {"source": single, "field": "spec.source"}
    sources = spec.get("sources")
    if not isinstance(sources, list):
        return None
    charts = [
        {"source": entry, "field": f"spec.sources[{position}]"}
        for position, entry in enumerate(sources)
        if isinstance(entry, dict) and entry.get("chart")
    ]
    return charts[0] if len(charts) == 1 else None


def _argocd_kustomize_source(spec: dict, root: Path) -> dict | None:
    """The one Kustomize source on an Argo CD Application, with its field path.

    Reached only where `_argocd_chart_source` found no chart, and answers the
    case its docstring sets aside. A plain directory of manifests is resolved
    by `workload_declarations`, which finds the object's own YAML -- but an
    overlay over a *remote* base renders objects no file in the repo declares,
    so that lookup finds nothing and the finding falls to `manual`. It is the
    same hole a chart put a workload in, and as common: `resources:` pointing
    at a tagged base in another repository is an ordinary way to run a fleet.

    A `kustomization.yaml` at `spec.source.path` is the discriminator, checked
    against the clone rather than against `spec.source.kustomize`, which Argo
    CD infers from that same file and most Applications therefore omit. Where
    the base *is* local, the object has its own manifest, and the
    `"declaration" not in candidate` guard in `_emit` keeps that direct edit
    ahead of a patch here.

    Multi-source and two-source ambiguity follow `_argocd_chart_source`.
    """

    def rooted(entry: object) -> bool:
        if not isinstance(entry, dict) or entry.get("chart"):
            return False
        path = str(entry.get("path") or "").strip()
        if not path or path.startswith("/") or ".." in Path(path).parts:
            return False
        try:
            directory = root / path
            return any((directory / name).is_file() for name in KUSTOMIZATION_FILE_NAMES)
        except OSError:
            return False

    single = spec.get("source")
    if rooted(single):
        return {"source": single, "field": "spec.source"}
    sources = spec.get("sources")
    if not isinstance(sources, list):
        return None
    overlays = [
        {"source": entry, "field": f"spec.sources[{position}]"}
        for position, entry in enumerate(sources)
        if rooted(entry)
    ]
    return overlays[0] if len(overlays) == 1 else None


def _argocd_values_field(source: dict, field: str) -> str:
    """Where a values override belongs on one Argo CD chart source.

    Names the block already in the file when there is one, so an override joins
    it rather than sitting beside a second block Argo CD would ignore --
    `values` and `valuesObject` are mutually exclusive and Argo rejects an
    Application carrying both.
    """
    helm = source.get("helm")
    if isinstance(helm, dict) and isinstance(helm.get(ARGOCD_VALUES_STRING_FIELD), str):
        return f"{field}.helm.{ARGOCD_VALUES_STRING_FIELD}"
    return f"{field}.helm.{ARGOCD_VALUES_OBJECT_FIELD}"


def release_declarations(root: Path) -> dict[tuple, dict]:
    """Index the Helm releases a GitOps clone installs, by destination cluster.

    `workload_declarations` above resolves a workload to the file declaring
    *that object*. A workload a chart renders has no such file: the repo
    declares the release, and the object exists only after Helm expands the
    chart. That is what sends a right-sizing finding to `manual` -- and
    right-sizing is the fix a pull request carries best, because `resources` is
    the one key nearly every chart publishes. See `collect.release_declarations`,
    which holds the full reasoning, the three key shapes, and why `ApplicationSet`
    is deliberately not indexed.

    Returns `{}` when PyYAML is absent or the clone is unreadable.
    """
    try:
        import yaml  # noqa: PLC0415 -- optional; absence disables the annotation
    except ImportError:
        return {}

    try:
        paths = sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml"))
    except OSError:
        return {}

    documents: list[tuple[str, tuple[str, ...], dict]] = []
    for path in paths:
        if GIT_DIR_NAME in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            docs = list(yaml.safe_load_all(text))
        except yaml.YAMLError:
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        for doc in docs:
            if isinstance(doc, dict) and doc.get("kind"):
                documents.append((str(relative), relative.parts, doc))

    # Pass 1: the two lookups an Application or a HelmRelease resolves through.
    servers: dict[str, str] = {}
    repositories: dict[tuple[str, str], str] = {}
    for _, _, doc in documents:
        kind = str(doc.get("kind") or "")
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        if kind == "Secret":
            labels = meta.get("labels") if isinstance(meta.get("labels"), dict) else {}
            if labels.get(ARGOCD_CLUSTER_SECRET_LABEL) != ARGOCD_CLUSTER_SECRET_VALUE:
                continue
            # `stringData` is what a committed registration uses; `data` is
            # base64 and a committed one would be a leaked credential, so only
            # the plaintext form is read.
            entry = doc.get("stringData")
            if not isinstance(entry, dict):
                continue
            server = str(entry.get("server") or "").strip()
            cluster = str(entry.get("name") or "").strip()
            if server and cluster:
                servers[server] = cluster
        elif kind == FLUX_HELM_REPOSITORY_KIND:
            repo_spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
            url = str(repo_spec.get("url") or "").strip()
            name = str(meta.get("name") or "")
            namespace = str(meta.get("namespace") or "")
            if url and name:
                repositories[(namespace, name)] = url

    index: dict[tuple, dict] = {}
    ambiguous: set[tuple] = set()

    def record(key: tuple, entry: dict) -> None:
        existing = index.get(key)
        if existing is not None and existing != entry:
            # Two declarations for one release. Same reasoning as
            # `declaration_for`: naming one would name the wrong file half the
            # time, so name neither.
            ambiguous.add(key)
            return
        index[key] = entry

    # Pass 2: the declarations themselves.
    for relative, parts, doc in documents:
        kind = str(doc.get("kind") or "")
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        spec = doc.get("spec") if isinstance(doc.get("spec"), dict) else {}
        name = str(meta.get("name") or "")
        if not name:
            continue
        if kind == ARGOCD_APPLICATION_KIND:
            chart = _argocd_chart_source(spec)
            overlay = _argocd_kustomize_source(spec, root) if chart is None else None
            if chart is None and overlay is None:
                continue
            found = chart or overlay
            source, field = found["source"], found["field"]
            destination = spec.get("destination") if isinstance(spec.get("destination"), dict) else {}
            cluster = str(destination.get("name") or "").strip()
            if not cluster:
                server = str(destination.get("server") or "").strip()
                # The in-cluster destination is whichever cluster Argo CD runs
                # on, which this index has no way to name. Skip rather than
                # resolve a finding into the wrong tree.
                if server and server != ARGOCD_IN_CLUSTER_SERVER:
                    cluster = servers.get(server, "")
            if not cluster:
                continue
            release_namespace = str(destination.get("namespace") or "").strip()
            helm = source.get("helm") if isinstance(source.get("helm"), dict) else {}
            entry = {
                "path": relative,
                "kind": ARGOCD_APPLICATION_KIND,
                "renderer": RENDERER_HELM if chart else RENDERER_KUSTOMIZE,
                "chart": str(source.get("chart") or source.get("path") or ""),
                "repo": str(source.get("repoURL") or ""),
                "version": str(source.get("targetRevision") or ""),
                "values_field": (
                    _argocd_values_field(source, field)
                    if chart
                    else f"{field}.{RENDERER_KUSTOMIZE}.{ARGOCD_KUSTOMIZE_PATCHES_FIELD}"
                ),
            }
            record((cluster, RELEASE_KEY_APPLICATION, name), entry)
            release_name = str(helm.get("releaseName") or "").strip() or name
            # Only a chart install leaves the `meta.helm.sh` pair behind, so
            # only a chart is worth the second key. Kustomize output carries
            # the tracking id alone, which the Application key above already
            # answers.
            if chart and release_namespace:
                record((cluster, RELEASE_KEY_RELEASE, release_namespace, release_name), entry)
            # A local Kustomize root is also an answer to "where does a new
            # object for this namespace go", which no other key gives. A chart
            # is not: its directory is in another repository, and the values
            # override that reaches an *existing* object cannot create one.
            if overlay and release_namespace and entry["chart"]:
                record((cluster, RELEASE_KEY_NAMESPACE, release_namespace), entry)
        elif kind == FLUX_HELM_RELEASE_KIND:
            # Flux is installed per-cluster, so the path convention is the only
            # cluster signal a HelmRelease carries.
            if len(parts) <= GITOPS_CLUSTER_TREE_DEPTH or parts[0] != GITOPS_CLUSTER_TREE_ROOT:
                continue
            cluster = parts[1]
            namespace = str(meta.get("namespace") or "")
            # A scalar or a list where the chart template goes is a malformed
            # document, and one malformed file must not crash the whole run
            # before the manifest prints. Skip it; `sourceRef` is guarded alike.
            # An absent `chart` is the `chartRef` form, which still indexes, on
            # an empty chart, for the values field it names.
            chart = spec.get("chart") if spec.get("chart") is not None else {}
            chart_spec = chart.get("spec") if isinstance(chart, dict) and chart.get("spec") is not None else {}
            if not isinstance(chart, dict) or not isinstance(chart_spec, dict):
                continue
            source_ref = chart_spec.get("sourceRef") if isinstance(chart_spec.get("sourceRef"), dict) else {}
            repo_namespace = str(source_ref.get("namespace") or namespace)
            repo_name = str(source_ref.get("name") or "")
            release_namespace = str(spec.get("targetNamespace") or namespace)
            release_name = str(spec.get("releaseName") or "").strip() or name
            if not release_namespace:
                continue
            entry = {
                "path": relative,
                "kind": FLUX_HELM_RELEASE_KIND,
                "renderer": RENDERER_HELM,
                "chart": str(chart_spec.get("chart") or ""),
                "repo": repositories.get((repo_namespace, repo_name), ""),
                "version": str(chart_spec.get("version") or ""),
                "values_field": f"spec.{FLUX_VALUES_FIELD}",
            }
            record((cluster, RELEASE_KEY_RELEASE, release_namespace, release_name), entry)

    for key in ambiguous:
        index.pop(key, None)
    return index


def release_declaration_for(index: dict[tuple, dict], cluster: str, release: dict | None) -> dict | None:
    """Where the GitOps repo declares one candidate's chart release, or `None`.

    `None` is no claim either way, exactly as in `declaration_for`. The Argo CD
    Application name is tried first, being the more specific of the two keys.
    """
    if not index or not cluster or not release:
        return None
    application = str(release.get("application") or "")
    if application:
        entry = index.get((cluster, RELEASE_KEY_APPLICATION, application))
        if entry:
            return entry
    name = str(release.get("name") or "")
    namespace = str(release.get("namespace") or "")
    if name and namespace:
        entry = index.get((cluster, RELEASE_KEY_RELEASE, namespace, name))
        if entry:
            return entry
    return None


def _emit(
    slug: str,
    hit: dict,
    *,
    cluster: str = "",
    declarations: dict | None = None,
    reconcilers: dict[tuple[str, str], str] | None = None,
    releases: dict[tuple[str, str], dict] | None = None,
) -> dict:
    """One candidate. `declarations` annotates it with the file that declares
    its object, when the run was given a `--workspace` and the index resolves
    it; `cluster` is the tree that index is keyed by, so both are needed or
    neither. `reconcilers` says what holds the object's spec, which is what
    decides whether a hand-applied fix survives; it is built from one cluster's
    object dump, so it pairs with `cluster` under the same rule. `releases`
    covers the case `declarations` cannot: a workload no file declares because
    a chart renders it, annotated with the file declaring that chart release,
    so the fix is a values override rather than a refusal. The project-scoped
    compute checks pass none of the four: a disk, a reserved address and a
    forwarding rule are GCP resources with no cluster tree, no namespace and no
    Kubernetes controller holding their spec, so there is nothing for any of
    the indexes to key on."""
    candidate = {
        "check": slug,
        "namespace": hit.get("namespace", ""),
        "object": hit["object"],
        "severity": hit["severity"],
        "excerpt": hit["excerpt"],
        "impact": IMPACT[slug],
        "needs_triage": None,
    }
    if declarations:
        found = declaration_for(declarations, cluster, candidate["namespace"], candidate["object"])
        if found:
            candidate["declaration"] = found
    if reconcilers and cluster:
        reconciler = reconcilers.get((candidate["namespace"], candidate["object"]))
        if reconciler:
            candidate["reconciler"] = reconciler
    # Only where nothing declares the object itself. A workload with its own
    # manifest in the repo is fixed by editing that manifest, and naming a
    # values override beside it would offer two files for one fix.
    if releases and cluster and "declaration" not in candidate:
        release = releases.get((candidate["namespace"], candidate["object"]))
        if release:
            candidate["release_declaration"] = release
    return candidate


def _pv_disk_key(handle: str) -> str:
    """`<location>/<name>` for a PV handle that names its disk's zone or region,
    else the bare name.

    A disk name is unique only per zone, so a PV holding `us-central1-a/data-1`
    must not claim an orphaned `us-central1-b/data-1`. A handle with no
    location (`pdName`, or a handle this does not parse) claims every disk of
    that name, which errs towards not reporting. A disk name cannot contain
    `/`, so the two spellings never collide."""
    match = PV_DISK_HANDLE_RE.search(handle)
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return handle.rsplit("/", 1)[-1]


def _fleet_facts(context: dict) -> dict:
    """What the project-scoped compute checks (3.4, 3.6) need to know about
    *this* cluster's live objects, so `collect_fleet` can union them across
    every cluster before running those checks -- a PV's backing disk or a
    Service a forwarding rule targets can live on any cluster in the
    project, not necessarily the one whose dump happened to mention it."""
    pv_handles = set()
    for pv in context["pvs"]:
        spec = pv.get("spec", {})
        handle = (spec.get("csi") or {}).get("volumeHandle") or (spec.get("gcePersistentDisk") or {}).get("pdName")
        if handle:
            pv_handles.add(_pv_disk_key(handle))
    service_names = {
        f"{s.get('metadata', {}).get('namespace', '')}/{s.get('metadata', {}).get('name', '')}" for s in context["services"]
    }
    referenced_addresses = set()
    # An Ingress names its global address with
    # `kubernetes.io/ingress.global-static-ip-name`, and holds it RESERVED
    # until its load balancer provisions.
    for svc in context["services"] + context.get("ingresses", []):
        annotations = svc.get("metadata", {}).get("annotations") or {}
        for key in LB_ANNOTATION_KEYS:
            value = annotations.get(key)
            if value:
                referenced_addresses.update(v.strip() for v in value.split(","))
    return {"pv_handles": pv_handles, "service_names": service_names, "referenced_addresses": referenced_addresses}


def empty_fleet_facts() -> dict:
    """The `fleet_facts` half of a `collect_cluster` return for a cluster
    nothing could be read from."""
    return {"pv_handles": set(), "service_names": set(), "referenced_addresses": set()}


def crashed_entry(cluster: dict, exc: BaseException) -> dict:
    """A `clusters[]` entry for a worker that raised something unmodelled.

    `future.result()` re-raises, so one unhandled exception on one cluster
    aborts `collect_fleet` — and the SOP invokes this collector as
    `fleet_waste.py … > manifest_fleet-wide-cost-analysis.json`, so by then
    the shell has already truncated the file. The run loses the whole fleet to
    one bad object instead of one cluster. `gate-failed` is the shape the
    document already carries for "enumerated, could not be read", and it also
    closes §3.6's `all_reachable` gate for that project, which is the
    conservative answer when a cluster's objects went unseen.
    """
    print(
        f"[fleet_waste] {cluster.get('project', '?')}/{cluster.get('name', '?')}: "
        f"collector raised {type(exc).__name__}: {exc}",
        file=sys.stderr,
    )
    return {
        "name": target_name(cluster.get("project", "?"), cluster.get("location", "?"), cluster.get("name", "?")),
        "project": cluster.get("project", "?"),
        "location": cluster.get("location", "?"),
        "autopilot": bool(cluster.get("autopilot")),
        "outcome": "gate-failed",
        "error": f"collector raised {type(exc).__name__}: {exc}"[:ERROR_EXCERPT_CHARS],
    }


def crashed_project_error(project: str, exc: BaseException) -> str:
    """`crashed_entry`'s account for a project read: the error its
    `gate-failed` `project/<p>` target carries. A project read parses the same
    live API answers a cluster read does, and without this one bad answer
    there aborts the fleet the way `crashed_entry` exists to prevent."""
    print(f"[fleet_waste] project {project}: collector raised {type(exc).__name__}: {exc}", file=sys.stderr)
    return f"collector raised {type(exc).__name__}: {exc}"[:ERROR_EXCERPT_CHARS]


def _metrics_gap_phrase(result: Run, read: str) -> str:
    """How a §2 metrics limitation should describe the read behind it.

    `fetch_usage_peaks` and `fetch_memory_means` both fold two outcomes into
    `available=False`, and only one of them is a failure: rc 0 is a 200 that
    carried no time series, which says the cluster is not shipping system
    metrics, and every other rc is a read that did not complete. Calling the
    first one "failed (rc=0)" gave the reader a contradiction to resolve
    before they could act on it -- an rc of zero is the one value that
    normally means nothing went wrong.
    """
    if result.rc == 0:
        return (
            f"the Cloud Monitoring {read} read returned no container time"
            " series, so this cluster is not shipping system metrics"
        )
    return (
        f"the Cloud Monitoring {read} read failed (rc={result.rc}) — "
        f"{result.stderr.strip()[:DETAIL_EXCERPT_CHARS] or 'no detail'}"
    )


def collect_cluster(cluster: dict, *, run: RunFn, session: SessionFn, now: datetime, declarations: dict | None = None, releases: dict[tuple, dict] | None = None, lb_traffic: tuple[dict, Run] | None = None) -> tuple[dict, dict]:
    """Returns `(manifest_entry, fleet_facts)` — the second only populated
    when the object dump succeeded; `collect_fleet` unions it across every
    cluster before running the project-scoped checks that need it."""
    name, project, location = cluster["name"], cluster["project"], cluster["location"]
    target = target_name(project, location, name)
    # A cluster property `enumerate_clusters` already resolved, so it rides on
    # every shape below: the mode does not stop being true because this run
    # failed to read inside the cluster.
    mode = {"autopilot": bool(cluster.get("autopilot"))}

    empty_facts = empty_fleet_facts()
    kubeconfig, cred_run = fetch_credentials(project, name, location, run=run)
    if cred_run.rc != 0:
        return {"name": target, "project": project, "location": location, **mode, "outcome": UNREACHABLE_OUTCOME, "error": f"get-credentials rc={cred_run.rc}: {cred_run.stderr.strip()[:ERROR_EXCERPT_CHARS]}"}, empty_facts

    env = {**os.environ, "KUBECONFIG": str(kubeconfig)}
    dump_argv = ["kubectl", "get", CLUSTER_DUMP_KINDS, "-A", "-o", "json"]
    parsed, result = run_and_gate(dump_argv, run=run, env=env)
    if parsed is None:
        return {"name": target, "project": project, "location": location, **mode, "outcome": "gate-failed", "error": f"object dump gate failed (rc={result.rc}): {result.stderr.strip()[:ERROR_EXCERPT_CHARS]}"}, empty_facts
    if not isinstance(parsed, dict) or not isinstance(parsed.get("items"), list):
        # Read as empty, it would be a cluster with no Services or volumes --
        # which 3.4 and 3.6 then take as proof a project's disks and rules are
        # nobody's.
        return {"name": target, "project": project, "location": location, **mode, "outcome": "gate-failed", "error": "object dump gate failed: the answer has no `items` list"}, empty_facts
    dump_record = _record(f"KUBECONFIG={kubeconfig} {shlex.join(dump_argv)}", result)
    context = build_context(parsed)
    fleet_facts = _fleet_facts(context)
    release_index = _releases_by_object(parsed, releases, name)

    def emit(slug: str, hit: dict) -> dict:
        """`_emit` with this cluster's three indexes bound. Every candidate
        below goes through it, so a check added later is annotated by default
        rather than by remembering to ask. Defined after the dump because two
        of those indexes are built from it."""
        return _emit(
            slug, hit, cluster=name, declarations=declarations,
            reconcilers=context["reconcilers"], releases=release_index,
        )

    limitations: list[str] = []
    not_applicable: list[dict] = []
    # A check whose read failed: neither run nor inapplicable. `finish` rejects
    # a document that files one of these under either list, which a
    # `limitations` sentence alone cannot make it do.
    unevaluated: dict[str, str] = {}

    usage_peaks, metrics_ok, usage_result = fetch_usage_peaks(project, name, location=location, session=session, now=now)
    usage_record = _record(usage_result.argv[0], usage_result)
    # Only worth the extra round trip where the peak read already succeeded:
    # the two fail for the same reasons, and `underrequest` has nothing to say
    # about a cluster `overrequest` could not measure either.
    if metrics_ok:
        memory_means, means_ok, means_result = fetch_memory_means(project, name, location=location, session=session, now=now)
    else:
        memory_means, means_ok, means_result = {}, False, usage_result
    means_record = _record(means_result.argv[0], means_result)

    candidates = []
    commands = {
        "orphan-pv": dump_record, "unconsumed-pvc": dump_record, "terminal-pods": dump_record, "idle-namespace": dump_record,
    }
    candidates += [emit("orphan-pv", h) for h in check_orphan_pv(context, now=now)]
    candidates += [emit("unconsumed-pvc", h) for h in check_unconsumed_pvc(context, now=now)]
    candidates += [emit("terminal-pods", h) for h in check_terminal_pods(context, now=now)]
    candidates += [emit("idle-namespace", h) for h in check_idle_namespace(context, now=now)]

    # Autopilot owns its node pools, so 3.7/3.8 are inapplicable there. Only a
    # Standard cluster owes them, so only a Standard cluster can be short of
    # them -- claiming the limitation on Autopilot too would raise a gap for a
    # check that target does not owe, which is the double-counted disposition
    # 7301c594 removed.
    #
    # The collector declares that itself rather than leaving it to the model,
    # because the model has to remember *both* slugs and on 2026-08-29 it
    # remembered one: three Autopilot clusters came back with `idle-nodepool`
    # not-applicable and `scaledown-blocked` simply absent, which §6 reads --
    # correctly, on what it was given -- as a check nobody ran. The weekly
    # `fleet-wide-cost-analysis` published `partial: true` with three coverage
    # gaps naming a check that cannot exist on those clusters. `autopilot` is a
    # fact the collector already holds, so the disposition belongs here where it
    # is the same on every run.
    if not cluster.get("autopilot"):
        # Read here rather than for every cluster: nothing on the Autopilot
        # branch consumes it, and there the call is a round trip whose answer
        # is discarded.
        node_pools_argv = ["gcloud", "container", "node-pools", "list", "--cluster", name, "--location", location, "--project", project, "--format", "json"]
        # Gated, unlike the bare `run` this used to be. An unreadable node-pool
        # list -- denied, throttled, a bad `--location` -- parsed to `[]`, and a
        # cluster with no node pools has no idle ones, so 3.7 and 3.8 recorded
        # their command and reported nothing found. The evidence line carried the
        # non-zero rc, but nothing downstream reads it: the ledger said the pools
        # were checked and were fine. An answer that parses to anything but a
        # list of objects (an error object, a stray string) is as unread: one
        # key read as a sole pool, and more crashed the cluster on a string.
        parsed_pools, pools_result = run_and_gate(node_pools_argv, run=run)
        pools_readable = isinstance(parsed_pools, list) and all(isinstance(p, dict) for p in parsed_pools)
        node_pools = parsed_pools if pools_readable else []
        pools_record = _record(shlex.join(node_pools_argv), pools_result)
        if not pools_readable:
            pools_failure = (
                "returned output that is not a JSON list of node pools (rc=0)"
                if pools_result.rc == 0
                else f"failed (rc={pools_result.rc})"
            )
            for slug in ("idle-nodepool", "scaledown-blocked"):
                unevaluated[slug] = f"`gcloud container node-pools list` {pools_failure}"
            limitations.append(
                f"idle-nodepool and scaledown-blocked could not be measured on "
                f"this cluster: `gcloud container node-pools list` {pools_failure} — "
                f"{pools_result.stderr.strip()[:DETAIL_EXCERPT_CHARS] or 'no stderr'}"
            )
        elif len(node_pools) == 1:
            # The same shape as the Autopilot branch below, reached from the
            # other direction. §3.7 will not flag a cluster's only node pool --
            # the system pods need somewhere to land -- so `check_idle_nodepool`
            # drops out on `len(node_pools) <= 1` before it measures anything,
            # and `scaledown-blocked` reads the idle pools it found, so it has
            # nothing to examine either. Recording the commands anyway published
            # a `gcloud container node-pools list` that returned rc=0 against
            # both slugs, which tells a reader the checks ran and came back
            # clean. On 2026-09-05 that put `idle-nodepool` in issue #113's
            # evidence table for all eleven Standard clusters when ten of them
            # have a single pool: a denominator of one presented as eleven.
            #
            # This is not the unreadable-pools case above, which is a
            # degradation to repair, and not the zero-pool case, which is a real
            # measurement over an empty set. A sole pool is permanent and
            # structural, so it is a disposition -- and like Autopilot's it
            # names both slugs, because a check that is neither run nor
            # dispositioned reads downstream as one nobody performed.
            not_applicable += [
                {
                    "check": slug,
                    "reason": (
                        "This cluster has a single node pool, which §3.7 will "
                        "not flag because the system pods need somewhere to "
                        "land, so idle-nodepool has no pool it is permitted to "
                        "size and scaledown-blocked has no idle pool to examine."
                    ),
                }
                for slug in ("idle-nodepool", "scaledown-blocked")
            ]
        else:
            commands["idle-nodepool"] = pools_record
            commands["scaledown-blocked"] = dump_record
            ops_argv = [
                "gcloud", "container", "operations", "list", "--location", location, "--project", project,
                "--filter", f"operationType={CREATE_NODE_POOL_OPERATION} AND targetLink~/clusters/{name}/nodePools/",
                "--format", "json",
            ]
            operations, _ops_result = run_and_gate(ops_argv, run=run)
            # A read that parsed to anything but a list of objects said nothing about
            # pool creations; handed on, it read as "no pool created lately"
            # and dated every pool from the cluster. It takes the failed-read
            # path instead: node age, with the pool named in `limitations`.
            pool_ages = node_pool_creation_ages(operations, name, now=now) if object_list(operations) is not None else None
            idle_pool_hits = check_idle_nodepool(
                context, node_pools, now=now, pool_ages=pool_ages,
                cluster_age=_age_days(cluster.get("create_time") or "", now=now), limitations=limitations,
            )
            candidates += [emit("idle-nodepool", h) for h in idle_pool_hits]
            candidates += [emit("scaledown-blocked", h) for h in check_scaledown_blocked(context, idle_pool_hits)]
    else:
        not_applicable += [
            {
                "check": slug,
                "reason": (
                    "Autopilot manages this cluster's nodes and exposes no node "
                    "pools to size or to find scaledown-blocked, so the check has "
                    "no object to run against."
                ),
            }
            for slug in ("idle-nodepool", "scaledown-blocked")
        ]

    # All three peak checks go through `_measured_peaks`, which needs both
    # dimensions, so one metric missing cluster-wide loses all three. Not
    # `underrequest`: it reads the mean-memory query, and prints "peak not
    # read" where the memory peak is absent.
    missing_dimension = _missing_usage_dimension(usage_peaks) if metrics_ok else None
    if metrics_ok and missing_dimension is None:
        commands["overrequest"] = usage_record
        commands["unsized-workload"] = usage_record
        for hit in check_overrequest(context, usage_peaks, now=now, autopilot=bool(cluster.get("autopilot"))):
            emitted = emit("overrequest", hit)
            # `guaranteed-qos` wins over the bump marker: §3.1 has that
            # finding published as `manual`, and the model needs the marker
            # to know to write it that way. Both keep it out of the sweep.
            if hit.get("_guaranteed"):
                emitted["needs_triage"] = GUARANTEED_QOS_TRIAGE
            elif hit.get("_autopilot_bumped"):
                emitted["needs_triage"] = AUTOPILOT_BUMP_TRIAGE
            candidates.append(emitted)
        # Same peak read, the complementary population: 3.1 sizes the
        # controllers that declared a request, 3.12 the ones that did not.
        for hit in check_unsized(context, usage_peaks, now=now, autopilot=bool(cluster.get("autopilot"))):
            emitted = emit("unsized-workload", hit)
            if hit.get("_autopilot_bumped"):
                emitted["needs_triage"] = AUTOPILOT_BUMP_TRIAGE
            candidates.append(emitted)
        # Same peak read again, and the population 3.1 drops: a controller
        # whose resize would change nothing because nothing is using it, or
        # one whose resize 3.1 refuses to make because it is `Guaranteed`.
        commands["idle-workload"] = usage_record
        # A second read behind the same check, recorded under its own slug.
        # Not a check: nothing in §3's roster is called this, so it must never
        # reach the document's `checks_run`, which validates against that
        # roster and would reject the run. What it is is the evidence behind
        # the traffic sentence in an `idle-workload` excerpt, and a figure a
        # reader cannot re-read is a figure they have to take on trust -- which
        # is the position §3.13 was in when it asserted a caller.
        #
        # Its own slug and not a second write to `idle-workload`: that key
        # holds the usage read, `adopt_collector_evidence` publishes it as the
        # command behind every finding here, and overwriting it would leave
        # each sizing claim citing a load-balancer query.
        if lb_traffic is not None:
            commands["idle-workload-traffic"] = _record(lb_traffic[1].argv[0], lb_traffic[1])
        for hit in check_idle_workload(
            context, usage_peaks, now=now, lb_traffic=lb_traffic[0] if lb_traffic else None
        ):
            emitted = emit("idle-workload", hit)
            # Without this the retracted impact never reaches a reader.
            # `adopt_arm_impact` adopts the collector's sentence only for
            # candidates that mark it authoritative, and the pass `finish` plans
            # for reusing last run's prose on byte-identical evidence would reuse
            # it -- which for a workload idle for a month is every run. The
            # findings that stood down three Deployments would go on
            # publishing "a workload nobody is calling" forever with the constant
            # above corrected.
            emitted["impact_authoritative"] = True
            # Every stand-down is held out of the automatic sweep, under the
            # more specific of two reasons. A Service-backed one removes the
            # endpoints behind a name something may still be calling, and this
            # check has no way to know whether anything is; any other one still
            # takes the workload to zero on an idle reading
            # (`IDLE_STANDDOWN_TRIAGE`). `/remediate` still opens either, which
            # is the point -- the judgement the collector cannot supply is a
            # reader's to supply by name.
            if hit.get("_selected_by"):
                emitted["needs_triage"] = IDLE_SERVICE_TRIAGE
            else:
                emitted["needs_triage"] = IDLE_STANDDOWN_TRIAGE
            candidates.append(emitted)
    elif metrics_ok:
        missing, present = missing_dimension
        usage_gap = (
            f"the Cloud Monitoring usage read returned no {missing} container"
            f" time series although it returned {present} series for the same cluster"
        )
        for slug in ("overrequest", "unsized-workload", "idle-workload"):
            unevaluated[slug] = usage_gap
        limitations.append(
            f"overrequest, unsized-workload and idle-workload could not be measured on this cluster: {usage_gap}"
        )
    elif not context["nodes"] and usage_result.rc == 0:
        # A cluster with no nodes cannot be over-requesting: there is no
        # capacity for a reservation to waste, and nothing has run for a
        # request to be measured against. The usage read comes back empty
        # there for the same reason the cluster is empty -- no containers ran,
        # so none reported -- so the branch below would read a vacuum as a
        # degradation. On 2026-08-29 it did: `fleet-wide-cost-analysis`
        # published `partial: true` over two freshly created Autopilot peers
        # whose whole object set was fifteen Pending pods and no nodes. That is
        # 7301c594's failure again from the other side. A `partial` that fires
        # on every empty cluster is one operators learn to scroll past, and the
        # gap it hides next time will be a real one.
        #
        # `rc == 0` is what earns the word "returns" in the reason below.
        # `metrics_ok` is False for a read that came back empty *and* for one
        # that never came back at all -- `fetch_usage_peaks` returns rc 0 only
        # for the empty answer, -1 for a missing session or a transport
        # error, and the HTTP status for anything else. Without the guard, a
        # credential failure at startup fails every cluster's read and this
        # arm tells the operator that each empty one has no metrics because
        # nothing ran on it -- a structural verdict drawn from a measurement
        # that was never taken, and one the same report contradicts a few
        # lines down, where every cluster that does have nodes carries the
        # honest "read failed" limitation for the identical failure.
        not_applicable.append(
            {
                "check": "overrequest",
                "reason": (
                    "This cluster has no nodes, so no workload is scheduled and "
                    "no reservation is holding capacity: there is nothing for a "
                    "request to be over against. Cloud Monitoring returns no "
                    "container time series for it for the same reason -- nothing "
                    "ran to report any."
                ),
            }
        )
        not_applicable.append(
            {
                "check": "idle-workload",
                "reason": (
                    "This cluster has no nodes, so no controller holds a "
                    "reservation and Cloud Monitoring has no usage history to "
                    "call one unused: nothing ran to report any."
                ),
            }
        )
        not_applicable.append(
            {
                "check": "unsized-workload",
                "reason": (
                    "This cluster has no nodes, so nothing has run and Cloud "
                    "Monitoring holds no usage history for it. A request cannot "
                    "be sized from a measurement that does not exist, and this "
                    "check reports nothing else -- the absence of a request is "
                    "the Workload Reliability audit's no-requests finding."
                ),
            }
        )
    else:
        # §2's metrics degradation. `overrequest` already dropped out of
        # `commands` on its own, so §6 was raising it as a gap with no reason
        # attached -- a reader saw the check named and had nothing to tell them
        # whether it was denied, throttled, or never attempted.
        for slug in ("overrequest", "unsized-workload", "idle-workload"):
            unevaluated[slug] = _metrics_gap_phrase(usage_result, "usage")
        limitations.append(
            f"overrequest, unsized-workload and idle-workload could not be "
            f"measured on this cluster: {_metrics_gap_phrase(usage_result, 'usage')}"
        )

    if means_ok:
        commands["underrequest"] = means_record
        candidates += [emit("underrequest", h) for h in check_underrequest(context, usage_peaks, memory_means, now=now)]
    elif not context["nodes"] and means_result.rc == 0:
        # Same vacuum as `overrequest`'s arm above, read the other way round: a
        # cluster with no nodes has nothing scheduled, so no pod can be sitting
        # above its request, and the empty Monitoring answer says only that
        # nothing ran. Same `rc == 0` guard, for the same reason, and it
        # carries here even when the usage read is what failed: `collect_cluster`
        # hands `means_result` the very same `Run`, so its rc is the usage
        # read's rc and a failure there does not become a verdict here.
        not_applicable.append(
            {
                "check": "underrequest",
                "reason": (
                    "This cluster has no nodes, so nothing is scheduled and no "
                    "pod can be consuming more than it requested. Cloud "
                    "Monitoring returns no container time series for it for the "
                    "same reason -- nothing ran to report any."
                ),
            }
        )
    else:
        # Where the peak read failed the mean read was never issued and
        # `means_result` is the usage read's `Run`, so name that read.
        if metrics_ok and means_result.rc == 0:
            # The usage read answered, so the cluster is shipping system
            # metrics and "not shipping" would contradict the other checks'
            # records; what came back empty is this one query.
            means_gap = (
                "the Cloud Monitoring mean-memory read returned no container"
                " time series although the usage read for the same cluster did"
            )
        else:
            means_gap = _metrics_gap_phrase(means_result, "mean-memory" if metrics_ok else "usage")
        unevaluated["underrequest"] = means_gap
        limitations.append(f"underrequest could not be measured on this cluster: {means_gap}")

    entry = {
        "name": target, "project": project, "location": location, **mode, "outcome": "collected",
        "commands": [{"check": slug, **record} for slug, record in commands.items()],
        "candidates": candidates,
    }
    if limitations:
        entry["limitations"] = "; ".join(limitations)
    if not_applicable:
        entry["checks_not_applicable"] = not_applicable
    if unevaluated:
        entry["checks_unevaluated"] = [{"check": slug, "reason": reason} for slug, reason in sorted(unevaluated.items())]
    return entry, fleet_facts


# --------------------------------------------------------------------------- #
# Project-scoped GCP compute checks (3.4, 3.5, 3.6)
# --------------------------------------------------------------------------- #


def _idle_since(disk: dict) -> tuple[str, str]:
    """When the disk stopped being used, and how the excerpt should say it.

    `creationTimestamp` is the wrong clock here for any disk that was ever
    attached. The SOP justifies the 30-day threshold as outliving "node
    upgrades, pod rescheduling, and maintenance windows" — churn, in other
    words — and none of those touch creation time. A boot disk created a year
    ago and detached this morning clears a creation-age filter with eleven
    months to spare, and it is exactly the churn the threshold exists to
    exclude. Reading it as waste would also make the excerpt's "unattached
    since" a false statement by eleven months.

    GCE stamps `lastDetachTimestamp` on every detach and omits it for a disk
    that has never been attached — the one case where creation really is the
    moment it went idle, and the excerpt says so rather than implying a detach
    that never happened.
    """
    detached = disk.get("lastDetachTimestamp")
    if detached:
        return str(detached), f"unattached since {detached}"
    created = disk.get("creationTimestamp", "")
    return str(created), f"never attached, created {created}"


def _pvc_origin(disk: dict) -> str:
    """`namespace/name` of the claim a GKE disk was provisioned for, or "".

    A PD-CSI volume is named for its PersistentVolume's UID, so the finding
    identifies it as `pvc-d45cfdfd-f194-4bda-9d53-f83a67d8ac34` and nothing in
    that string tells the operator what is on it. The decision the finding asks
    for is whether to delete the disk, and "this is the platform agent's data
    volume" is most of that decision. PD-CSI already records it: the driver
    writes the claim's namespace and name into `description` as JSON when it
    provisions, and GKE leaves the field alone afterwards, so it survives the
    cluster the claim lived in.
    """
    raw = str(disk.get("description") or "")
    if CSI_DESCRIPTION_MARKER not in raw:
        return ""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    name = str(parsed.get("kubernetes.io/created-for/pvc/name") or "")
    if not name:
        return ""
    namespace = str(parsed.get("kubernetes.io/created-for/pvc/namespace") or "")
    return f"{namespace}/{name}" if namespace else name


def _named_cluster_of(disk: dict, known_clusters: set[str] | None, unread_clusters: frozenset[str]) -> str:
    """The cluster a `gke-<cluster>-` disk name points at, or "".

    §3.4's second attribution rung, for a disk with no cluster label. The
    longest match over every cluster the project lists wins, so
    `gke-prod-usc1-...` is `prod-usc1`'s and not `prod`'s; it names a cluster
    only when the audit read that one, because a prefix is a guess and the
    fleet this run did not read is where the guess cannot be checked.
    """
    if known_clusters is None:
        return ""
    name = str(disk.get("name") or "")
    matches = [c for c in known_clusters if c and name.startswith(f"{GKE_DISK_NAME_PREFIX}{c}{GKE_DISK_NAME_SEPARATOR}")]
    if not matches:
        return ""
    longest = max(matches, key=len)
    return "" if longest in unread_clusters else longest


def _dead_cluster_of(disk: dict, known_clusters: set[str] | None) -> str:
    """The cluster this disk was provisioned for, if that cluster is gone.

    Empty string for every other case, including the one that matters most:
    `known_clusters=None` means this project's `container clusters list` did
    not come back, so *no* cluster can be shown absent and the short floor is
    withheld from the whole project. Reading an unknown fleet as an empty one
    would flag every GKE disk in it a week after any detach.
    """
    if known_clusters is None:
        return ""
    owner = ((disk.get("labels") or {}).get(GKE_CLUSTER_LABEL) or "").strip()
    if not owner or owner in known_clusters:
        return ""
    return owner


def _bare_cluster_name(entry: dict) -> str:
    """The cluster's own name, which is what a disk's `goog-k8s-cluster-name` carries.

    `not_running_entry` qualifies its `name` as a manifest target, while the
    running half of `enumerate_clusters` keeps `clusters list`'s bare one, so
    both halves are reduced to the last segment before they are compared."""
    return entry.get("name", "").rsplit(QUALIFIED_TARGET_SEPARATOR, 1)[-1]


def _known_clusters(clusters: list[dict]) -> tuple[set[str], set[tuple[str, str | None]]]:
    """A project's cluster names, and its (name, location) pairs, whatever their status."""
    return (
        {_bare_cluster_name(c) for c in clusters},
        {(_bare_cluster_name(c), c.get("location")) for c in clusters},
    )


def _unread_names(known: set[tuple[str, str | None]], collected: set[tuple[str, str | None]]) -> frozenset[str]:
    """The names of this project's clusters whose PersistentVolumes were not read.

    Keyed on (name, location) and reduced to names, because a disk's
    `goog-k8s-cluster-name` label is all `check_unattached_disk` matches on:
    when two clusters share a name and only one was read, the name counts as
    unread, which skips the read one's disks rather than flag the other's."""
    return frozenset(name for name, _ in known - collected)


def _unread_labels(known: set[tuple[str, str | None]], collected: set[tuple[str, str | None]]) -> list[str]:
    """The unread clusters as a reader should see them: a name another of the
    project's clusters shares carries its location, so a limitation never
    names a cluster that was read as one that was not."""
    shared = {name for name, _ in known if sum(1 for other, _ in known if other == name) > 1}
    return sorted(f"{name} in {location}" if name in shared else name for name, location in known - collected)


def check_unattached_disk(
    disks: list[dict],
    live_pv_handles: set[str],
    *,
    now: datetime,
    known_clusters: set[str] | None = None,
    unread_clusters: frozenset[str] = frozenset(),
) -> list[dict]:
    """`unread_clusters` are this project's clusters whose PersistentVolumes
    this run does not hold. A detached disk one of them still binds is not in
    `live_pv_handles`, so it would read as abandoned: a disk labelled for one
    is skipped, and so is an unlabelled one that a PVC created, which could
    belong to any of them. A disk no PVC created is judged as usual.

    A managed service's disk (Composer, Dataproc) is that service's to
    reclaim, and a node boot disk is the node pool's while its cluster lives.

    The excerpt names the owner by label, else by a `gke-<cluster>-` name
    prefix (`_named_cluster_of`); the 7-day floor stays keyed on the label."""
    hits = []
    for disk in disks:
        if disk.get("users"):
            continue
        labels = disk.get("labels") or {}
        if any(key.startswith(MANAGED_DISK_LABEL_PREFIXES) for key in labels):
            continue
        owner = (labels.get(GKE_CLUSTER_LABEL) or "").strip()
        if GKE_NODE_DISK_LABEL in labels and not _dead_cluster_of(disk, known_clusters):
            continue
        if unread_clusters and (owner in unread_clusters or (not owner and _pvc_origin(disk))):
            continue
        idle_since, idle_phrase = _idle_since(disk)
        age = _age_days(idle_since, now=now)
        if age is None:
            continue
        dead_cluster = _dead_cluster_of(disk, known_clusters)
        if age < (DEAD_CLUSTER_AGE_DAYS if dead_cluster else UNATTACHED_AGE_DAYS):
            continue
        name = disk.get("name", "")
        if name in live_pv_handles or f"{_location_of(disk)}/{name}" in live_pv_handles:
            continue
        # `sizeGb` is an int64 string in the API's JSON; a value that does not
        # parse skips this one disk rather than failing the project's read,
        # as `check_registry_no_cleanup` does with `sizeBytes`.
        try:
            size_gb = float(disk.get("sizeGb") or 0)
        except (TypeError, ValueError):
            continue
        # gcloud returns `type` as a full diskTypes selfLink, so the excerpt
        # used to carry 100 characters of URL where `pd-balanced` belongs --
        # next to a `--zone` flag `_scope_flag` shortens for the same reason.
        # The severity test below reads the whole string either way; `pd-ssd`
        # is a substring of its own URL.
        disk_type = str(disk.get("type") or "").rsplit("/", 1)[-1]
        # The excerpt has to carry the dead cluster, not just the age: it is the
        # whole reason a 9-day-old disk is a finding when the one beside it at
        # 20 days is not, and `adopt_collector_evidence` makes this string the
        # only evidence a reader sees.
        # A live owner is named too: the finding stays on `project/<p>` so its
        # identity does not move with attribution, which leaves the excerpt as
        # the one place the owning cluster is recorded.
        if dead_cluster:
            orphan_note = f", provisioned for cluster {dead_cluster} which this project no longer runs"
        elif owner:
            orphan_note = f", labelled for cluster {owner}"
        elif named := _named_cluster_of(disk, known_clusters, unread_clusters):
            orphan_note = f", named for cluster {named}"
        else:
            orphan_note = ""
        pvc = _pvc_origin(disk)
        pvc_note = f", held the {pvc} PersistentVolumeClaim" if pvc else ""
        hits.append(
            {
                "object": _located("Disk", disk),
                "excerpt": f"{idle_phrase}{_ago(age)}, {size_gb:.0f} GB, {disk_type} ({_scope_flag(disk)}){pvc_note}{orphan_note}",
                "severity": "major" if size_gb >= UNATTACHED_DISK_MAJOR_GB or "ssd" in disk_type.lower() or "extreme" in disk_type.lower() else "minor",
            }
        )
    return hits


def _location_of(obj: dict) -> str:
    """`us-east4-a`, `us-east4`, or `global` for any compute resource.

    gcloud returns `zone` and `region` as full selfLink URLs and omits both keys
    for a global resource, so a finding that passes either field through carries
    a URL where a location belongs -- and nothing at all in the global case.
    Handles a bare name too, which is what some projections return.
    """
    for key in ("zone", "region"):
        value = obj.get(key) or ""
        if value:
            return value.rsplit("/", 1)[-1]
    return "global"


def _located(kind: str, obj: dict) -> str:
    """`<kind>/<location>:<name>`, the `object` of a project-scoped compute finding.

    A disk name is unique per zone and an address, rule, pool or backend name
    per region (or once globally), not per project; the collector reads the
    whole project in one list and files every candidate under `project/<p>`
    with no namespace, so `object` is the only field left to tell two
    same-named resources apart. With the bare name they derived one finding
    id and `finish` refused the document. The spelling is stockout's
    `Quota/<region>:<metric>`.
    """
    return f"{kind}/{_location_of(obj)}:{obj.get('name', '')}"


def _scope_flag(obj: dict) -> str:
    """The gcloud scope flag a remediation command for `obj` must carry.

    Every `gcloud compute` verb has to be told where to look, and getting it
    wrong does not fail loudly: with no flag gcloud resolves against whatever
    region it happens to be configured for, so a global address -- or a disk in
    another zone -- answers `was not found`. A reader takes that for a finding
    somebody has already remediated rather than a command written wrong, so the
    resource stays on the bill and a true finding is discredited. Emitting the
    flag next to the object is what keeps the agent from having to infer it.
    """
    location = _location_of(obj)
    if location == "global":
        return "--global"
    return f"--zone={location}" if obj.get("zone") else f"--region={location}"


def check_idle_address(addresses: list[dict], referenced_addresses: set[str], *, project: str, now: datetime) -> list[dict]:
    hits = []
    idle = []
    for addr in addresses:
        if addr.get("addressType") != "EXTERNAL" or addr.get("status") != "RESERVED":
            continue
        if (addr.get("purpose") or "") in NON_WASTE_ADDRESS_PURPOSES:
            continue
        if addr.get("name") in referenced_addresses or addr.get("address") in referenced_addresses:
            continue
        if HELD_ADDRESS_DESCRIPTION_RE.search(addr.get("description") or ""):
            continue
        age = _age_days(addr.get("creationTimestamp", ""), now=now)
        if age is None or age < IDLE_ADDRESS_MIN_AGE_DAYS:
            continue
        idle.append((addr, age))
    if len(idle) >= IDLE_ADDRESS_ROLLUP_MIN:
        # §3.5's roll-up is per *project*, and §5 requires a roll-up to be named
        # after the scope it covers rather than after one of its members. It was
        # named `Address/rollup-<region of idle[0]>`: a region only the first
        # address is necessarily in -- so a project leaking addresses across
        # three regions published one finding claiming all of them were in one --
        # and an identity that moves the moment that address is released, which
        # re-announces the same leak as a new finding. The scope is the project.
        by_location: dict[str, list[str]] = {}
        for addr, _ in idle:
            by_location.setdefault(_location_of(addr), []).append(addr.get("name", ""))
        breakdown = ", ".join(f"{loc} ({len(names)})" for loc, names in sorted(by_location.items()))
        names = sorted(n for n in (a.get("name", "") for a, _ in idle) if n)
        shown = ", ".join(names[:ROLLUP_EXCERPT_MEMBERS])
        if len(names) > ROLLUP_EXCERPT_MEMBERS:
            shown += f", and {len(names) - ROLLUP_EXCERPT_MEMBERS} more"
        return [
            {
                "object": f"Project/{project}",
                "excerpt": f"{len(idle)} external addresses RESERVED and unattached across {breakdown}: {shown}",
                "severity": "major",
            }
        ]
    for addr, age in idle:
        hits.append({"object": _located("Address", addr), "excerpt": f"RESERVED and unattached since {addr.get('creationTimestamp')}{_ago(age)} ({_scope_flag(addr)})", "severity": "minor"})
    return hits


def check_registry_no_cleanup(repositories: list[dict], *, project: str, now: datetime) -> list[dict]:
    """§3.14 -- Artifact Registry repositories nothing ever deletes from.

    The one waste class in this SOP that grows without anybody doing anything.
    Every other check finds a resource somebody provisioned and stopped using;
    this one finds the absence of a policy, and the bill climbs on every CI push
    until one is written. On the reference install the single repository reached
    91.9 GB in 44 days -- 2.1 GB/day, from image builds nothing prunes.

    A dry-run policy counts as no policy, deliberately. `cleanupPolicyDryRun`
    makes Artifact Registry log what it would delete and delete nothing, which
    is how a policy is supposed to be introduced -- and how one is forgotten. It
    reads as configured in the console and in `gcloud ... list`, so a check that
    tested only for the presence of `cleanupPolicies` would call the forgotten
    case clean.
    """
    hits = []
    for repo in repositories:
        if repo.get("mode") != REGISTRY_BILLED_MODE:
            continue
        # `sizeBytes` is a string in the API's JSON -- int64 over the wire -- and
        # is absent entirely on an empty repository.
        try:
            size = int(repo.get("sizeBytes") or 0)
        except (TypeError, ValueError):
            continue
        if size < REGISTRY_SIZE_FLOOR_BYTES:
            continue
        age = _age_days(repo.get("createTime", ""), now=now)
        if age is None or age < REGISTRY_MIN_AGE_DAYS:
            continue
        policies = repo.get("cleanupPolicies") or {}
        dry_run = bool(repo.get("cleanupPolicyDryRun"))
        if policies and not dry_run:
            continue
        # `name` is the full resource path; the location is what the remediation
        # has to carry, and it is only available here. `object` carries it too:
        # a repository name is unique per location, not per project (`_located`).
        parts = str(repo.get("name") or "").split("/")
        short = parts[-1] if parts else ""
        location = parts[3] if len(parts) > 4 else ""
        per_day = size / age
        severity = (
            "major"
            if size >= REGISTRY_SIZE_MAJOR_BYTES
            or per_day >= REGISTRY_GROWTH_MAJOR_BYTES_PER_DAY
            else "minor"
        )
        why = (
            f"{len(policies)} cleanup polic{'y' if len(policies) == 1 else 'ies'} "
            "configured, all in dry-run, so nothing is deleted"
            if policies
            else "no cleanup policy"
        )
        hits.append(
            {
                "object": f"ArtifactRegistryRepository/{location}:{short}",
                "excerpt": (
                    f"{repo.get('format', 'unknown')} repository in {location}: "
                    f"{size / BYTES_PER_GIB:.1f} GiB, {why}, created "
                    f"{repo.get('createTime')}{_ago(age)} "
                    f"(--location={location})"
                ),
                "severity": severity,
            }
        )
    return hits


def _backend_service_key(backend: dict) -> tuple[str, str]:
    """`(region, name)` for a listed backend service; region is empty for a global one."""
    return (str(backend.get("region") or "").rsplit("/", 1)[-1], str(backend.get("name") or ""))


def _backend_service_url_key(url: str) -> tuple[str, str] | None:
    """`_backend_service_key` for a URL a forwarding rule names, or None."""
    m = BACKEND_SERVICE_URL_RE.search(url)
    return (m.group(1) or "", m.group(2)) if m else None


def check_orphan_lb(forwarding_rules: list[dict], target_pools: list[dict], backend_services: list[dict], known_services: set[str], *, now: datetime) -> list[dict]:
    hits = []
    # `SERVICE_NAME_DESCRIPTION_RE`: the GKE service controller writes the
    # description as a JSON object --
    # `{"kubernetes.io/service-name":"ns/name","kubernetes.io/api-version":"v1"}`
    # -- so the key is followed by a closing quote before the colon. A pattern
    # requiring `service-name:` therefore matched no real forwarding rule at
    # all: every one of them fell through the `if not m: continue` and 3.6's
    # orphaned-rule leg has never emitted a finding against a live fleet. The
    # optional quotes accept both that shape and the bare `key: value` form the
    # SOP's own example uses.
    live_backends = {_backend_service_key(b) for b in backend_services if b.get("backends")}
    for rule in forwarding_rules:
        desc = rule.get("description", "") or ""
        m = SERVICE_NAME_DESCRIPTION_RE.search(desc)
        if not m:
            continue
        if "multiclusteringress" in desc.lower() or "multiclusterservice" in desc.lower():
            continue
        if m.group(1) in known_services:
            continue
        # §3.6's PSC exclusion, for the endpoint side the rule list shows: a
        # rule targeting a service attachment, or carrying a PSC connection.
        # A rule a service attachment *publishes* needs `service-attachments
        # list`, which is not read; SOP §2 leaves that one to be checked by hand.
        if SERVICE_ATTACHMENT_PATH in str(rule.get("target") or "") or rule.get("pscConnectionId"):
            continue
        # §3.6's other exclusion: an internal rule still delivering to a
        # backend service that has backends is serving, whatever its
        # description says. Internal passthrough rules name it in
        # `backendService`, not `target`.
        if str(rule.get("loadBalancingScheme") or "").startswith(INTERNAL_SCHEME_PREFIX):
            url = str(rule.get("backendService") or rule.get("target") or "")
            if _backend_service_url_key(url) in live_backends:
                continue
        age = _age_days(rule.get("creationTimestamp", ""), now=now)
        if age is None or age < ORPHAN_LB_MIN_AGE_DAYS:
            continue
        hits.append({"object": _located("ForwardingRule", rule), "excerpt": f"targets deleted Service {m.group(1)}, created {rule.get('creationTimestamp')}{_ago(age)} ({_scope_flag(rule)})", "severity": "major"})
    for pool in target_pools:
        if not pool.get("instances"):
            hits.append({"object": _located("TargetPool", pool), "excerpt": f"zero instances ({_scope_flag(pool)})", "severity": "major"})
    for backend in backend_services:
        if not backend.get("backends"):
            hits.append({"object": _located("BackendService", backend), "excerpt": f"zero backends ({_scope_flag(backend)})", "severity": "major"})
    return hits


def _api_disabled(result: Run) -> bool:
    """Whether a failed gcloud read failed because its API is off in the project."""
    return result.rc != 0 and any(marker in result.stderr for marker in API_DISABLED_MARKERS)


def forwarding_rules_argv(project: str) -> list[str]:
    """§3.6's rule list, named once because two callers now need the same read.

    `collect_fleet` runs it before the worker pool, because §3.13's traffic
    read has to turn a Service's external address into a forwarding rule name
    before any cluster is collected, and hands the answer to
    `collect_project_compute` below. One read, two consumers -- adding a second
    `gcloud compute forwarding-rules list` per project would double the call
    and leave two answers that can disagree.
    """
    return ["gcloud", "compute", "forwarding-rules", "list", "--project", project, "--format", "json"]


def collect_project_compute(project: str, all_reachable: bool, fleet_facts: dict, *, run: RunFn, now: datetime, known_clusters: set[str] | None = None, forwarding_rules: tuple[object | None, Run] | None = None, unread_clusters: frozenset[str] = frozenset(), clusters_read: int | None = None, unread_labels: list[str] | None = None, announce: bool = True) -> dict | None:
    # `--filter=-users:*` and not `"--filter", "-users:*"`: a filter value
    # starting with `-` reads as a flag to gcloud's own argument parser, which
    # then rejects the command for the argument it thinks is missing
    # (`argument --filter: expected one argument`, rc=2). This read therefore
    # failed on every run since it was written, and because the five reads below
    # gate as one it took the whole project target down with it -- every weekly
    # `fleet-wide-cost-analysis` published `project/<p>` as `gate-failed`, so
    # `unattached-disk` has never once been evaluated by the collector.
    disks_argv = ["gcloud", "compute", "disks", "list", "--project", project, "--filter=-users:*", "--format", "json"]
    disks_parsed, disks_result = run_and_gate(disks_argv, run=run)
    addr_argv = ["gcloud", "compute", "addresses", "list", "--project", project, "--filter", "status!=IN_USE", "--format", "json"]
    addr_parsed, addr_result = run_and_gate(addr_argv, run=run)
    # Read here only when nobody read it already. `collect_fleet` needs the
    # same answer before the worker pool starts and passes it down; running
    # this file by hand, or from a test that does not, still gets the read.
    fwd_argv = forwarding_rules_argv(project)
    fwd_parsed, fwd_result = forwarding_rules or run_and_gate(fwd_argv, run=run)
    tp_argv = ["gcloud", "compute", "target-pools", "list", "--project", project, "--format", "json"]
    tp_parsed, tp_result = run_and_gate(tp_argv, run=run)
    bs_argv = ["gcloud", "compute", "backend-services", "list", "--project", project, "--format", "json"]
    bs_parsed, bs_result = run_and_gate(bs_argv, run=run)
    # Anything but a list of objects is a failed read, and gates below as one.
    disks_parsed, addr_parsed, fwd_parsed, tp_parsed, bs_parsed = (
        object_list(parsed) for parsed in (disks_parsed, addr_parsed, fwd_parsed, tp_parsed, bs_parsed)
    )

    # Name the read that failed and what it said. "one or more compute list
    # reads failed" was what this returned for as long as the disks filter was
    # broken, and it is the reason nobody noticed: five reads gate as one, the
    # message fingers none of them, and the only way to learn which had been
    # failing all along was to run all five by hand against a live project.
    compute_reads = (
        (disks_argv, disks_parsed, disks_result),
        (addr_argv, addr_parsed, addr_result),
        (fwd_argv, fwd_parsed, fwd_result),
        (tp_argv, tp_parsed, tp_result),
        (bs_argv, bs_parsed, bs_result),
    )
    failed = [
        f"{shlex.join(argv)} rc={result.rc}: "
        + (
            result.stderr.strip()[:DETAIL_EXCERPT_CHARS]
            or ("no stderr" if result.rc else NOT_AN_OBJECT_LIST)
        )
        for argv, parsed, result in compute_reads
        if parsed is None
    ]
    # All five refused with the disabled-API answer: the project has no
    # Compute Engine, so it holds nothing these checks look for. Any other
    # mix is a read that failed, and gates as one below.
    # A project with a known cluster has Compute Engine, whatever the error
    # says: the refusal is then someone else's, such as a quota project's.
    # Without a cluster to say so, the refusal has to name this project.
    compute_disabled = (
        not known_clusters
        and len(failed) == len(compute_reads)
        and all(_api_disabled(result) for _, _, result in compute_reads)
        and refusal_names_project(project, disks_result.stderr, run=run)
    )
    compute_failed = bool(failed) and not compute_disabled
    compute_error = f"{len(failed)} of {len(compute_reads)} compute list reads failed -- " + "; ".join(failed)

    # Outside the five-read gate above, on purpose. Those five gate as one
    # because §3.4-§3.6 cross-reference each other's objects; §3.14 shares
    # nothing with them, so a project without the Artifact Registry API enabled
    # -- or without the allowlist entry for this read -- must not take
    # `unattached-disk` and `idle-address` down with it. That is not a
    # hypothetical: the disks filter bug above did exactly that to this whole
    # target for every run until it was found. No `--location`: unset lists
    # every location in one read, and the banner it prints goes to stderr.
    reg_argv = ["gcloud", "artifacts", "repositories", "list", "--project", project, "--format", "json"]
    reg_parsed, reg_result = run_and_gate(reg_argv, run=run)
    reg_parsed = object_list(reg_parsed)
    registry_disabled = reg_parsed is None and _api_disabled(reg_result) and refusal_names_project(project, reg_result.stderr, run=run)
    # The reverse holds too (SOP §3.14): a failed compute read leaves
    # `registry-no-cleanup` running, and only when that read has nothing to
    # give either is there no check left for the target to carry.
    if compute_failed and reg_parsed is None:
        return {
            "name": f"{PROJECT_TARGET_PREFIX}{project}",
            "project": project,
            "location": "global",
            "outcome": "gate-failed",
            "error": compute_error,
        }
    if compute_disabled and registry_disabled:
        # Nothing any project-scoped check looks for can exist here, so there
        # is no target to report: a row naming four inapplicable checks and
        # no read would still need a `limitations` note in the document,
        # which makes every such project a coverage gap. `announce` is off
        # for `_prefetch_compute`'s recording pass, which walks this same
        # path once before the replay does; the line is said once per run.
        if announce:
            log(f"{project}: Compute Engine and Artifact Registry APIs are not enabled; no project-scoped check applies")
        return None

    not_applicable: dict[str, str] = {}
    if compute_disabled:
        not_applicable.update({slug: COMPUTE_DISABLED_REASON.format(project=project) for slug in COMPUTE_CHECKS})
    if registry_disabled:
        not_applicable["registry-no-cleanup"] = REGISTRY_DISABLED_REASON.format(project=project)

    # §3.4: with none of the project's clusters read there is no PV handle to
    # clear any disk against, so the check is withheld rather than judged.
    # Counted on (name, location) where the caller has it: `unread_clusters`
    # is reduced to names, and two `c1`s with one read would otherwise read
    # as none read and withhold the disks no PVC created as well.
    if clusters_read is None:
        none_read = bool(known_clusters) and unread_clusters >= known_clusters
    else:
        none_read = bool(known_clusters) and clusters_read == 0
    # `compute_ok`: the three compute checks can run. False when the API is
    # off (they are inapplicable) or a read failed (they are unevaluated).
    compute_ok = not compute_disabled and not compute_failed
    disks_judged = compute_ok and not none_read
    unread_named = ", ".join(sorted(unread_clusters) if unread_labels is None else unread_labels)
    candidates = [_emit("unattached-disk", h) for h in check_unattached_disk(disks_parsed, fleet_facts["pv_handles"], now=now, known_clusters=known_clusters, unread_clusters=unread_clusters)] if disks_judged else []
    # §3.5 clears an address any Service or Ingress annotation names, and
    # `referenced_addresses` holds only the read clusters' annotations, so an
    # unread cluster's reference would read as idle: withheld, as `orphan-lb` is.
    addresses_judged = compute_ok and all_reachable
    if addresses_judged:
        candidates += [_emit("idle-address", h) for h in check_idle_address(addr_parsed, fleet_facts["referenced_addresses"], project=project, now=now)]
    # A project with the Compute Engine API off holds no cluster either, so
    # there is no unread one to withhold `orphan-lb` over; it is inapplicable.
    all_reachable = all_reachable and compute_ok
    if all_reachable:
        candidates += [_emit("orphan-lb", h) for h in check_orphan_lb(fwd_parsed, tp_parsed, bs_parsed, fleet_facts["service_names"], now=now)]
    if reg_parsed is not None:
        candidates += [_emit("registry-no-cleanup", h) for h in check_registry_no_cleanup(reg_parsed, project=project, now=now)]

    entry = {
        "name": f"{PROJECT_TARGET_PREFIX}{project}",
        "project": project,
        "location": "global",
        "outcome": "collected",
        "commands": ([{"check": "unattached-disk", **_record(shlex.join(disks_argv), disks_result)}] if disks_judged else [])
        + ([{"check": "idle-address", **_record(shlex.join(addr_argv), addr_result)}] if addresses_judged else [])
        + ([{"check": "orphan-lb", **_record(shlex.join(fwd_argv), fwd_result)}] if all_reachable else [])
        # Recorded only when the read succeeded, which is what puts
        # `registry-no-cleanup` into §6's `coverage_gaps` when it did not. A
        # command entry carrying a non-zero rc would instead read as a check
        # that ran and found nothing.
        + ([{"check": "registry-no-cleanup", **_record(shlex.join(reg_argv), reg_result)}] if reg_parsed is not None else []),
        "candidates": candidates,
    }
    if not_applicable:
        entry["checks_not_applicable"] = [{"check": slug, "reason": reason} for slug, reason in sorted(not_applicable.items())]
    if compute_failed:
        # The compute gate's sentence. It is assigned, as the orphan-lb one
        # below is, because the two cannot both apply.
        entry["limitations"] = (
            "unattached-disk, idle-address and orphan-lb were not evaluated for "
            f"this project: {compute_error}. §3.4-§3.6 cross-reference each "
            "other's objects, so the five reads gate as one."
        )
    elif not all_reachable and not compute_disabled:
        # §6 already reports the missing check -- `orphan-lb` drops out of
        # `commands`, so the roster half of `coverage_gaps` names it whatever
        # this entry says in prose. What it cannot supply is why, and a gap
        # reading "orphan-lb did not run" sends a reader looking for a broken
        # gcloud read that is not there: the three compute reads all succeeded
        # and the check was withheld on purpose. §3.6 needs every cluster in
        # the project to say which Services exist, because a forwarding rule is
        # only orphaned if *no* cluster claims it -- so one unreadable cluster
        # would turn every load balancer it serves into a false positive. The
        # unreadable clusters are their own manifest entries, carrying the
        # stderr that explains each one.
        entry["limitations"] = (
            "orphan-lb was not evaluated for this project: §3.6 compares "
            "forwarding rules against the Service names of every cluster in "
            "the project, and at least one of them could not be read, so a "
            "rule that cluster's Services reference would read as orphaned. "
            "See this project's cluster "
            "entries in this manifest for the reason each one failed."
        )
    if not disks_judged and compute_ok:
        disk_gap = (
            "unattached-disk was not evaluated for this project: none of its clusters "
            f"({unread_named}) could be read, so no disk can be "
            "cleared against a live PersistentVolume."
        )
        entry["limitations"] = f"{entry['limitations']} {disk_gap}" if entry.get("limitations") else disk_gap
    elif unread_clusters and compute_ok:
        disk_gap = (
            "unattached-disk skipped every disk that a PersistentVolumeClaim created "
            "and that could belong to a cluster this run did not read "
            f"({unread_named}): those clusters' PersistentVolumes "
            "are unknown, so a detached disk one of them still binds would read as abandoned."
        )
        entry["limitations"] = f"{entry['limitations']} {disk_gap}" if entry.get("limitations") else disk_gap
    if compute_ok and not addresses_judged:
        address_gap = (
            "idle-address was not evaluated for this project: §3.5 clears any address a "
            "Service or Ingress annotation names, and "
            + (f"the clusters this run did not read ({unread_named})" if unread_named else "a cluster this run did not read")
            + " could name one, so an address they hold would read as idle."
        )
        entry["limitations"] = f"{entry['limitations']} {address_gap}" if entry.get("limitations") else address_gap
    unevaluated = {}
    if compute_failed:
        unevaluated.update({slug: compute_error for slug in COMPUTE_CHECKS})
    elif not all_reachable and not compute_disabled:
        unevaluated["orphan-lb"] = "a cluster in this project could not be read, so its Services are unknown"
        unevaluated["idle-address"] = "a cluster in this project could not be read, so its Service and Ingress address annotations are unknown"
    if not disks_judged and compute_ok:
        unevaluated["unattached-disk"] = "none of this project's clusters could be read"
    if reg_parsed is None and not registry_disabled:
        unevaluated["registry-no-cleanup"] = (
            f"`gcloud artifacts repositories list` failed (rc={reg_result.rc})"
            if reg_result.rc != 0
            else f"`gcloud artifacts repositories list` exited 0 and {NOT_AN_OBJECT_LIST}"
        )
    if unevaluated:
        entry["checks_unevaluated"] = [{"check": slug, "reason": reason} for slug, reason in sorted(unevaluated.items())]
    # After the block above, not before it: that one assigns `limitations`
    # outright rather than appending, so a registry gap written first would be
    # overwritten on any project with an unreadable cluster -- which is most of
    # them on a fleet this size.
    if reg_parsed is None and not registry_disabled:
        # `run_and_gate` returns None three ways, and only one of them is a
        # non-zero exit. A gap that said "exited 0" over an empty or unparseable
        # answer would send a reader to look for an error gcloud never reported.
        why = (
            f"exited {reg_result.rc} ({reg_result.stderr.strip()[:DETAIL_EXCERPT_CHARS] or 'no stderr'})"
            if reg_result.rc != 0
            else f"exited 0 and {NOT_AN_OBJECT_LIST}"
        )
        registry_gap = (
            "registry-no-cleanup was not evaluated for this project: "
            f"`{shlex.join(reg_argv)}` {why}. The compute "
            "checks in this entry are unaffected -- §3.14 shares no data with "
            "them and is gated separately."
        )
        entry["limitations"] = (
            f"{entry['limitations']} {registry_gap}"
            if entry.get("limitations")
            else registry_gap
        )
    return entry


def _read_project(p: str, *, run: RunFn, session: SessionFn, now: datetime) -> tuple[list[dict], list[dict], str | None, tuple[object | None, Run] | None, tuple[dict, Run] | None]:
    """One project's reads that have to land before the cluster pool starts:
    its clusters, its forwarding rules, and the traffic behind them. Returns
    `(running, not_running, enumeration_error, forwarding_rules, lb_traffic)`.

    The rule list is here for a reason of ordering rather than economy.
    §3.13 asks what the load balancer in front of an idle workload metered,
    and the only thing that maps a Service's external address to the
    forwarding rule Cloud Monitoring reports under is this list -- so it has
    to be in hand before the first cluster is collected.
    `collect_project_compute` then reuses the same answer for §3.6 rather
    than re-reading it."""
    try:
        running, not_running = enumerate_clusters(p, run=run)
    except IncompleteEnumeration as exc:
        # The clusters that arrived are audited; the project's own target
        # fails, because §3.4 and §3.6 would read a silent zone's cluster as
        # gone and its disks and forwarding rules as orphans.
        return exc.running, exc.not_running, str(exc), None, None
    except RuntimeError as exc:
        # A log line is not a record. The manifest is the only account of
        # what this run managed to read, and a project whose clusters could
        # not be listed used to leave nothing in it -- its `project/<p>`
        # compute entry still arrived as `collected`, so the document saw a
        # project with two of three checks and zero clusters, which is
        # exactly what a genuinely cluster-free project looks like. The
        # project's own `project/<p>` entry carries the loss instead, as
        # `gate-failed`: §3.4 and §3.6 both need the cluster list to tell
        # an orphan from a disk or rule a cluster still owns, so the
        # project checks cannot run honestly without it either.
        log(f"{p}: cluster enumeration failed, no clusters known from this project: {exc}")
        return [], [], str(exc)[:ERROR_EXCERPT_CHARS], None, None
    forwarding_rules = run_and_gate(forwarding_rules_argv(p), run=run)
    rules, _ = forwarding_rules
    # A failed rule list is §3.6's problem to report -- the five-read gate
    # in `collect_project_compute` still sees it and still fails the target.
    # Here it means only that the traffic sentence goes unwritten, which is
    # the silence `_idle_traffic_clause` prefers to a fabricated zero.
    # Only a running cluster's idle workload reads the traffic, so a project
    # without one skips the three Monitoring calls.
    traffic = fetch_lb_traffic(p, rules, session=session, now=now) if running and object_list(rules) is not None else None
    return running, not_running, None, forwarding_rules, traffic


def _before(deadline: float) -> bool:
    return time.monotonic() < deadline


def _prefetch_compute(p: str, *, run: RunFn, now: datetime, known_clusters: set[str] | None, forwarding_rules: tuple[object | None, Run] | None) -> dict[tuple[str, ...], Run]:
    """Every read `collect_project_compute` makes for `p`, keyed by argv.

    None of those reads depends on what the clusters hold -- only the judging
    does -- so they run in the cluster pool rather than after it, and the time
    the clusters take no longer eats into the project reads' deadline. The
    reads are recorded by running the function itself against empty facts:
    which reads it makes depends only on their own answers, so replaying the
    record with the real facts takes the same path without a second call."""
    recorded: dict[tuple[str, ...], Run] = {}

    def recording(argv: list[str], **kwargs) -> Run:
        result = run(argv, **kwargs)
        recorded[tuple(argv)] = result
        return result

    collect_project_compute(p, False, empty_fleet_facts(), run=recording, now=now, known_clusters=known_clusters, forwarding_rules=forwarding_rules, announce=False)
    return recorded


class ProjectCrash(NamedTuple):
    """What a project read that raised maps to instead of its result."""

    error: str


def _pooled_by_project(projects: list[str], work, *, max_workers: int, deadline: float) -> dict[str, object]:
    """`work(p)` for every project, `max_workers` at a time.

    A project whose turn comes after `deadline` (a `time.monotonic()` value)
    is not started, and maps to `None`; see `PROJECT_READ_DEADLINE_S`. The
    per-project reads used to run one project at a time outside any pool,
    so a credential listing N projects paid N rounds of gcloud calls before
    the first cluster was read."""
    def guarded(p: str):
        if not _before(deadline):
            return None
        try:
            return work(p)
        except Exception as exc:  # noqa: BLE001 — see crashed_project_error
            return ProjectCrash(crashed_project_error(p, exc))

    results: dict[str, object] = {}
    if not projects:
        return results
    with ThreadPoolExecutor(max_workers=max(1, min(len(projects), max_workers))) as pool:
        # Started in a fresh order each run, so a deadline that cuts the list
        # short leaves a different tail unread each week rather than the same
        # projects forever.
        futures = {pool.submit(guarded, p): p for p in random.sample(projects, len(projects))}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results


def _only_a_scope_note(entry: dict, project: str | None) -> bool:
    """Whether a target's error is the discovery entry's note on what a run
    skipped -- a `--project` scope or a filtered listing -- rather than a
    failure that explains why nothing was collected."""
    if entry.get("name") != UNENUMERATED_PROJECTS_TARGET:
        return False
    return bool(project) or entry["error"].startswith(FILTERED_LISTING_NOTE)


def collect_fleet(project: str | None = None, *, run: RunFn = default_run, session: SessionFn = None, max_workers: int = MAX_WORKERS, now: datetime | None = None, workspace: Path | None = None, project_budget_s: float = PROJECT_READ_DEADLINE_S) -> dict:
    now = now or datetime.now(timezone.utc)
    started_at = time.strftime(MONITORING_TIME_FORMAT, time.gmtime())
    deadline = time.monotonic() + project_budget_s
    run = _describing_once(run)

    if session is None:
        # One session for the fleet: its connection pool is thread-safe, and
        # building one per cluster would re-resolve the credential every time.
        # Failing to build one is not fatal -- `fetch_usage_peaks` turns a
        # `None` session into the same honest per-cluster limitation an API
        # error produces, and every check that reads object state still runs.
        try:
            session = default_monitoring_session()
        except Exception as exc:
            log(f"Cloud Monitoring credentials unavailable, overrequest will be skipped fleet-wide: {exc}")

    try:
        projects, partial_discovery = get_target_projects(project, run=run)
    except NoProjectInScope as exc:
        # No active project and a `projects list` that answered with nothing:
        # the credential sees no project, which is not an empty fleet. Projects
        # that were listed but hold no cluster do not land here: their project
        # reads still run, and only a run that reads nothing at all ends in
        # `NOTHING_COLLECTED_ERROR` below. The manifest contract's top-level `error`, as
        # `fleet_stockout.py` sets when its enumeration fails, and `main` exits
        # non-zero on it.
        return {
            "version": MANIFEST_VERSION,
            "checks_revision": CHECKS_REVISION,
            "audit": AUDIT_NAME,
            "started_at": started_at,
            "finished_at": time.strftime(MONITORING_TIME_FORMAT, time.gmtime()),
            "error": str(exc),
            "clusters": [],
        }

    clusters: list[dict] = []
    unaudited: list[dict] = []
    # Every cluster name the project answers to, whatever its status, or `None`
    # where the enumeration failed. §3.4's short floor for a disk whose owning
    # cluster is gone reads this, and a DEGRADED or PROVISIONING cluster is not
    # gone -- it is unaudited, which is why the union is taken over both halves
    # of `enumerate_clusters` rather than over the RUNNING one this loop feeds
    # to the workers.
    known_by_project: dict[str, set[str] | None] = {}
    # The same, as (name, location): a name is unique only per location, so
    # which clusters went unread is decided on the pair.
    known_pairs_by_project: dict[str, set[tuple[str, str | None]]] = {}
    enumeration_failed: dict[str, str] = {}
    forwarding_rules: dict[str, tuple[object | None, Run]] = {}
    lb_traffic: dict[str, tuple[dict, Run]] = {}
    reads = _pooled_by_project(projects, lambda p: _read_project(p, run=run, session=session, now=now), max_workers=max_workers, deadline=deadline)
    for p in projects:
        read = reads.get(p)
        if read is None:
            known_by_project[p] = None
            enumeration_failed[p] = PROJECT_DEADLINE_ERROR.format(budget=int(project_budget_s), project=p)
            continue
        if isinstance(read, ProjectCrash):
            known_by_project[p] = None
            enumeration_failed[p] = read.error
            continue
        running, not_running, error, rules, traffic = read
        # Empty unless `clusters list` answered in part, in which case what it
        # listed is still read even though the project's own target fails.
        clusters.extend(running)
        unaudited.extend(not_running)
        if error is not None:
            known_by_project[p] = None
            enumeration_failed[p] = error
            continue
        known_by_project[p], known_pairs_by_project[p] = _known_clusters(running + not_running)
        forwarding_rules[p] = rules
        if traffic is not None:
            lb_traffic[p] = traffic

    # Built once for the whole fleet, before the pool: every cluster's
    # candidates resolve against the same clone, and walking it per cluster
    # would read the same tree sixteen times to get the same answer.
    declarations = workload_declarations(workspace) if workspace else {}
    releases = release_declarations(workspace) if workspace else {}

    readable = [p for p in projects if p not in enumeration_failed]

    def prefetch_or_skip(p: str) -> dict[tuple[str, ...], Run] | ProjectCrash | None:
        if not _before(deadline):
            return None
        try:
            return _prefetch_compute(p, run=run, now=now, known_clusters=known_by_project.get(p), forwarding_rules=forwarding_rules.get(p))
        except Exception as exc:  # noqa: BLE001 — see crashed_project_error
            return ProjectCrash(crashed_project_error(p, exc))

    results: list[tuple[dict, dict]] = [None] * len(clusters)
    prefetched: dict[str, dict[tuple[str, ...], Run] | ProjectCrash | None] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(len(clusters) + len(readable), max_workers))) as pool:
        # Submitted first, so they start while the deadline still admits them.
        prefetch_futures = {pool.submit(prefetch_or_skip, p): p for p in random.sample(readable, len(readable))}
        futures = {pool.submit(collect_cluster, c, run=run, session=session, now=now, declarations=declarations, releases=releases, lb_traffic=lb_traffic.get(c["project"])): i for i, c in enumerate(clusters)}
        for future in as_completed(prefetch_futures):
            prefetched[prefetch_futures[future]] = future.result()
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as exc:  # noqa: BLE001 — see crashed_entry
                results[index] = (crashed_entry(clusters[index], exc), empty_fleet_facts())

    # Group per project: the "all reachable" gate for orphan-lb (§3.6) and
    # the cross-cluster fact union it and the disk/address checks read are
    # each scoped to one project, per the SOP's own per-project Do-NOT-flag
    # rule -- a cluster unreachable in project A must not suppress project
    # B's checks, and a PV handle from project A's cluster must not suppress
    # a genuinely unattached disk in project B.
    by_project: dict[str, list[tuple[dict, dict]]] = {}
    collected_by_project: dict[str, set[tuple[str, str | None]]] = {}
    for cluster, result in zip(clusters, results):
        by_project.setdefault(cluster["project"], []).append(result)
        if result[0].get("outcome") == "collected":
            collected_by_project.setdefault(cluster["project"], set()).add((cluster["name"], cluster.get("location")))
    # The clusters that never reached a worker belong to the gate too. §3.6
    # withholds `orphan-lb` unless *every* cluster in the project was read, and
    # a DEGRADED or PROVISIONING cluster is one that was not: it goes straight
    # into `scope.skipped` per §2, and its Services are exactly the ones a
    # forwarding rule might still reference. Reading the gate off `by_project`
    # alone -- which only ever holds RUNNING clusters -- meant a project could
    # have a cluster in `scope.skipped` and still publish `orphan-lb` findings
    # against Service names it had not finished collecting, which is the false
    # positive §3.6 calls the highest-risk cross-check in the audit.
    skipped_by_project: dict[str, list[dict]] = {}
    for entry in unaudited:
        skipped_by_project.setdefault(entry.get("project", ""), []).append(entry)

    def gate_failed_project(p: str, error: str) -> dict:
        return {"name": f"{PROJECT_TARGET_PREFIX}{p}", "project": p, "location": "global", "outcome": "gate-failed", "error": error}

    def compute_for(p: str, recorded: dict[tuple[str, ...], Run]) -> dict | None:
        group = by_project.get(p, [])
        # A project with no clusters is fully read: no Service anywhere can
        # still claim its forwarding rules, which is §3.6's orphan at its
        # plainest. Requiring one cluster withheld the check there every week
        # and pinned the run `partial`.
        all_reachable = (
            not skipped_by_project.get(p)
            and all(entry["outcome"] == "collected" for entry, _ in group)
        )
        fleet_facts = empty_fleet_facts()
        for _, facts in group:
            for key in fleet_facts:
                fleet_facts[key] |= facts[key]
        # A read missing from the record goes to the live API rather than
        # failing; the replay is expected to find every one.
        def replay(argv: list[str], **kwargs) -> Run:
            return recorded.get(tuple(argv)) or run(argv, **kwargs)

        known_pairs = known_pairs_by_project.get(p, set())
        collected = collected_by_project.get(p, set())

        return collect_project_compute(p, all_reachable, fleet_facts, run=replay, now=now, known_clusters=known_by_project.get(p), forwarding_rules=forwarding_rules.get(p), unread_clusters=_unread_names(known_pairs, collected), clusters_read=len(known_pairs & collected), unread_labels=_unread_labels(known_pairs, collected))

    cluster_entries: list[dict] = []
    project_entries: list[dict] = []
    read_projects = 0
    for p in projects:
        cluster_entries.extend(entry for entry, _ in by_project.get(p, []))
        if p in enumeration_failed:
            project_entries.append(gate_failed_project(p, enumeration_failed[p]))
            continue
        recorded = prefetched.get(p)
        if recorded is None:
            project_entries.append(gate_failed_project(p, PROJECT_DEADLINE_ERROR.format(budget=int(project_budget_s), project=p)))
            continue
        if isinstance(recorded, ProjectCrash):
            project_entries.append(gate_failed_project(p, recorded.error))
            continue
        try:
            entry = compute_for(p, recorded)
        except Exception as exc:  # noqa: BLE001 — see crashed_project_error
            project_entries.append(gate_failed_project(p, crashed_project_error(p, exc)))
            continue
        if entry:
            if known_by_project.get(p) == set():
                entry[CLUSTERS_LISTED_KEY] = 0
            project_entries.append(entry)
            read_projects += 1

    # One rung up from a failed `clusters list`: a `projects list` that failed,
    # or a `--project` that skipped it, took the other projects' names with it.
    discovery_entries = []
    if partial_discovery:
        discovery_entries.append(
            {
                "name": UNENUMERATED_PROJECTS_TARGET,
                "project": "",
                "location": "global",
                "outcome": "gate-failed",
                "error": partial_discovery[:ERROR_EXCERPT_CHARS],
            }
        )

    entries = cluster_entries + project_entries + unaudited + discovery_entries
    # Only a cluster that is not running counts as unread, and it is in
    # `unaudited`, never here: an unreachable cluster whose credentials failed
    # stays eligible, since §2 retries it by hand under a `limitations` note.
    if not cluster_entries and not read_projects:
        # Every target left is one §2 sends straight to `scope.skipped` -- an
        # unlisted or unreached project, a cluster not running -- and `finish`
        # rejects an empty `scope.clusters`, so this is the top-level `error`
        # rather than a manifest nothing can be built from. A target the
        # collector read and gate-failed is not in that set: §2's manual
        # retry can still bring it into scope.
        # The `--project` note and a filtered listing's note are errors only in
        # form: each says what this run did not look at, never why what it did
        # look at yielded nothing. A `projects list` that failed is a real
        # failure, so it stays eligible.
        first = next(
            (e for e in entries if e.get("error") and not _only_a_scope_note(e, project)),
            None,
        )
        return {
            "version": MANIFEST_VERSION,
            "checks_revision": CHECKS_REVISION,
            "audit": AUDIT_NAME,
            "started_at": started_at,
            "finished_at": time.strftime(MONITORING_TIME_FORMAT, time.gmtime()),
            "error": NOTHING_COLLECTED_ERROR.format(count=len(projects), first=f"{first['name']}: {first['error']}" if first else NO_TARGET_REASON),
            "clusters": [],
        }

    return {
        "version": MANIFEST_VERSION,
        "checks_revision": CHECKS_REVISION,
        "audit": AUDIT_NAME,
        "started_at": started_at,
        "finished_at": time.strftime(MONITORING_TIME_FORMAT, time.gmtime()),
        "clusters": entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", help="single project to audit; omit to run §1's project discovery")
    parser.add_argument(
        "--workspace",
        help=(
            "the GitOps clone `audit_report.py start` made, so each candidate "
            "carries where the repository declares its object -- or, for a "
            "workload a chart renders, where it declares that chart release; "
            "omit and no candidate is annotated"
        ),
    )
    args = parser.parse_args(argv)
    workspace = Path(args.workspace) if args.workspace else None
    if workspace is not None and not workspace.is_dir():
        # Loud, and not fatal. A typo here would otherwise annotate nothing and
        # read exactly like a repository that declares none of the fleet, which
        # is the answer that sends every finding to `manual`.
        print(
            f"fleet_waste.py: --workspace {str(workspace)!r} is not a directory; "
            "no candidate will carry a declaration",
            file=sys.stderr,
        )
        workspace = None
    manifest = collect_fleet(args.project, workspace=workspace)
    print(json.dumps(manifest, indent=2))
    return 1 if manifest.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
