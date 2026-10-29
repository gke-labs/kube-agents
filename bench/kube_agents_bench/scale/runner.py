"""Run a measurement plan: apply each level, time each operation, merge the records.

    python -m kube_agents_bench.scale.runner plan <plan.yaml> [--only-factor F] [--only-operation O] [--limit N]
    python -m kube_agents_bench.scale.runner run <plan.yaml> [--only-factor F] [--only-operation O] [--limit N] [--force]
    python -m kube_agents_bench.scale.runner apply <plan.yaml> --factor F --level L
    python -m kube_agents_bench.scale.runner teardown <plan.yaml> [--delete-fleet]
    python -m kube_agents_bench.scale.runner reclassify <plan.yaml>
    python -m kube_agents_bench.scale.runner remonitor <plan.yaml>

``run`` skips every run whose key is already in the data file, so an
interrupted run resumes where it stopped; ``--force`` re-runs and replaces.
The target (project, host cluster, test-cluster prefix and label) comes from
the plan's ``target`` block and ``SCALE_*`` environment variables.
"""

from __future__ import annotations

import argparse
import atexit
import contextlib
import itertools
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from kube_agents_bench.scale import collect, fixtures, kube, operations, records
from kube_agents_bench.scale.config import (
    BASE_FACTOR, COMBINED_FACTOR, PROXY_LOAD_KIND, TRIGGER_SOAK_KIND, Operation, Plan, RunSpec, load_plan,
)

LOG = logging.getLogger("harness-scale")

TEST_CLUSTER_FORMAT = "{prefix}{index:02d}"
TEST_CLUSTER_MACHINE_TYPE = "e2-standard-2"
DEFAULT_POOL_NODES = 2
CLUSTER_CREATE_ATTEMPTS = 3
# Cloud Monitoring needs a few minutes before a window's last points are queryable.
MONITORING_DELAY_SECONDS = 240
REGISTRATION_POLL_SECONDS = 60
# Time for a restarted watcher to list and sync every cluster before it is asked what it watches.
WATCHER_START_SECONDS = 180
WATCHER_POLL_SECONDS = 30
# How long the runner waits after a trigger_soak for the jobs it started, and how often it looks.
TRIGGER_DRAIN_SECONDS = 1800
# The commands that change the test clusters or the install, and so hold the live-test lease.
MUTATING_COMMANDS = ("run", "apply", "teardown")
LEASE_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "live_test_lease.py"
LEASE_ENVS_VAR = "KUBE_AGENTS_LIVE_TEST_ENVS"
LEASE_SESSION_VAR = "KUBE_AGENTS_LEASE_SESSION"
LEASE_SESSION_PREFIX = "harness-scale-"
LEASE_ENV_NAME = "harness-scale-host"
LEASE_ENVS_NAME = "live-test-envs.json"
LEASE_DIRECTORY_PREFIX = "harness-scale-lease-"
# Long enough for a plan of about 30 runs; a plan sets lease_ttl_minutes for more.
LEASE_TTL_MINUTES = 720
# Operation kinds whose runs make model calls of their own: prompts, triggered jobs, an onboarding.
MODEL_CALL_KINDS = ("prompt", "trigger_soak", "reset_soak")
DRAIN_POLL_SECONDS = 60
# The fits a run leaves out when work other than its own loaded the harness during it.
RESOURCE_FITS = ("cpu_peak_cores", "cpu_seconds", "memory_peak_bytes", "proxy_command_seconds", "proxy_commands",
                 "proxy_wait_max_seconds", "proxy_wait_p50_seconds")
# A proxy load run that shared the broker also leaves out its throughput and command times.
PROXY_LOAD_FITS = ("proxy_load_per_second", "proxy_load_p50_seconds", "proxy_load_p95_seconds", "proxy_load_max_seconds")
# A prompt answered without its operation measures none of the operation's cost.
TOKEN_FITS = ("tokens_input", "tokens_output", "tokens_cached", "run_tokens_input", "run_tokens_output",
              "run_tokens_cached")
# The Platform Agent's Stall Watch reads every cluster through the broker, one at a time, at
# :00 and :30 (agents/platform/cron/jobs.json). How long a tick keeps the broker busy, from the
# stored prompt runs: the ones that started 2 to 6 minutes after a tick at 2 to 16 clusters show
# its commands and shell CPU, and the ones 4 to 8 minutes after it at 2 to 8 clusters do not.
STALL_WATCH_PERIOD_SECONDS = 1800
STALL_WATCH_BASE_SECONDS = 120
STALL_WATCH_SECONDS_PER_CLUSTER = 30
# A trigger_soak's jobs: the slowest one's run time and how many completed, both read after the drain.
JOB_SECONDS_METRIC = "job_seconds"
JOBS_COMPLETED_METRIC = "jobs_completed"
STALL_WATCH_NOTE = "the Stall Watch's half-hourly read of every cluster overlapped the run"
# The operation kinds whose windows are shorter than the period; an hour-long soak holds two ticks every time.
STALL_WATCH_KINDS = ("prompt", "restart_soak")
OUTSIDE_COMMANDS_NOTE = "brokered commands from outside the load ran during it"
RESPONSE_FITS = ["response_seconds"]
LEVEL_LABEL = "harness-scale/level"
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_DATA_FILE = "harness-limits.json"
SECONDS_PER_MINUTE = 60
FIXTURES = ("objects", "listing", "warning", "churn")
# The values that shape the fleet; a level that changes none of them needs no settle time.
FLEET_VALUES = ("clusters", "nodes", "objects", "churn_per_minute", "failing_pods", "listing_pods")
FIXTURE_PARALLELISM = 8
# The tag of an image pinned by commit is the commit; anything else is recorded as the image only.
COMMIT_LENGTH = 40
HEX_DIGITS = set("0123456789abcdef")
PREFLIGHT_SHELL = "/bin/sh"
PREFLIGHT_TIMEOUT_SECONDS = 600
TERMINATED_EXIT_CODE = 143
STOPPED_EXIT_CODE = 2
PEAK_METRIC = "memory_peak_bytes"
# What add_peaks reads from Cloud Monitoring; remonitor replaces exactly these.
MONITORING_METRICS = ("memory_peak_bytes", "cpu_peak_cores", "cpu_seconds")
PROJECT_PLACEHOLDER = "<project>"
MONITORING_ERROR = "monitoring:"
REVISION_LENGTH = 12
DIRTY_SUFFIX = "-dirty"
PRIVATE_KUBECONFIG_PREFIX = "harness-scale-runner-"
KUBECONFIG_SUFFIX = ".kubeconfig"
# Operations whose answer has to be read to know the operation ran: they can be declined or
# cancelled (they need a GitOps repository; they honour the scope's exclusions).
NEEDS_ANSWER_CHECK = ("fleet_audit", "upgrade_readiness")
UNFINISHED_METRIC = "unfinished_prompts"
PROXY_CONCURRENCY = "proxy_concurrency"
# A Cluster Agent profile's kubeconfig in the sandbox shell, which proxy_load points its commands at.
PROFILE_KUBECONFIG_GLOB = "/opt/data/profiles/cluster-{project}-{cluster}-*/kubeconfig.yaml"
PROFILES_METRIC = "profiles"
CLUSTERS_TOTAL = "clusters"
STALE_PROFILES_NOTE = "profiles of deleted test clusters still present"
UNCHECKED_ANSWER = "no answer recorded (written before answers were kept), so completion cannot be checked"


def _runner_revision() -> str:
    """The runner's own commit, so records written by two runner versions can be told apart.

    Marked dirty when the runner's directory has uncommitted edits, since then the
    commit does not describe the code that ran.
    """
    here = str(Path(__file__).resolve().parent)
    try:
        revision = kube.run(["git", "-C", here, "rev-parse", f"--short={REVISION_LENGTH}", "HEAD"]).strip()
        dirty = kube.run(["git", "-C", here, "status", "--porcelain", "--", "."]).strip()
    except (kube.CommandError, OSError):
        return ""
    return revision + DIRTY_SUFFIX if dirty else revision


RUNNER_REVISION = _runner_revision()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(ISO_FORMAT)


def _epoch(iso: str) -> float:
    return datetime.strptime(iso, ISO_FORMAT).replace(tzinfo=timezone.utc).timestamp()


def commit_of(image: str) -> str:
    """The commit an image is pinned to, when its tag is a full commit SHA; else ''."""
    tag = image.rsplit(":", 1)[-1] if ":" in image.rsplit("/", 1)[-1] else ""
    return tag if len(tag) == COMMIT_LENGTH and set(tag) <= HEX_DIGITS else ""



def _overlaps_stall_watch(started: str, ended: str, clusters: float) -> bool:
    """Whether the window shares the broker with a Stall Watch tick: starts inside one, or holds one."""
    start, end = _epoch(started), _epoch(ended)
    tick = start - start % STALL_WATCH_PERIOD_SECONDS
    busy = STALL_WATCH_BASE_SECONDS + STALL_WATCH_SECONDS_PER_CLUSTER * clusters
    return start < tick + busy or end > tick + STALL_WATCH_PERIOD_SECONDS

class Runner:
    def __init__(self, plan: Plan, data_file: Path) -> None:
        self.plan = plan
        self.data_file = data_file
        target = plan.target
        self.project = target["project_id"]
        self.zone = target["zone"]
        self.prefix = target["cluster_prefix"]
        self.label = target["cluster_label"]
        self.install = collect.Install(
            self.project, target["host_context"], target["namespace"], target["host_cluster"], target["host_location"]
        )
        self.settings = plan.settings
        self.churner: fixtures.Churner | None = None
        # What the latest watcher check found; replaced at each check, so a record carries only its own.
        self.watch_errors: list[str] = []
        self.relay_port = 0
        # The fleet values of the level applied last, so a level that keeps them skips the settle.
        self.applied_fleet: dict[str, float] | None = None
        # The latest trigger_soak's operation and start, until every job it started has finished.
        self.triggered: tuple[Operation, str] | None = None

    # --- the fleet ----------------------------------------------------------------------------

    def parallelism(self, jobs: int) -> int:
        """How many GKE operations to run at once: ``cluster_create_parallelism``, or all of them.

        A bound keeps a large level from opening dozens of cluster or node-pool operations at once.
        """
        return int(self.settings.get("cluster_create_parallelism", 0)) or jobs

    def cluster_names(self, count: int) -> list[str]:
        return [TEST_CLUSTER_FORMAT.format(prefix=self.prefix, index=index) for index in range(1, count + 1)]

    def test_clusters(self, names: set[str]) -> set[str]:
        """The test-fleet names among ``names``: ``<prefix>NN`` only, so a host cluster named
        ``<prefix>host`` is never taken for a test cluster."""
        pattern = re.compile(rf"^{re.escape(self.prefix)}[0-9]+$")
        return {name for name in names if pattern.match(name)}

    def contexts(self, count: int) -> list[str]:
        return [kube.context_name(self.project, self.zone, name) for name in self.cluster_names(count)]

    def ensure_clusters(self, count: int) -> None:
        """Create the missing test clusters and delete the extra ones, then make the watcher watch them."""
        wanted = self.cluster_names(count)
        existing = kube.clusters_with_prefix(self.project, self.zone, self.prefix, self.label)
        missing = [name for name in wanted if name not in existing]
        extra = [name for name in existing if name not in wanted]
        if missing or extra:
            changes = len(missing) + len(extra)
            with ThreadPoolExecutor(max_workers=min(self.parallelism(changes), changes)) as pool:
                jobs = [pool.submit(self._create_cluster, name) for name in missing]
                jobs += [pool.submit(kube.delete_cluster, self.project, self.zone, name) for name in extra]
                for job in jobs:
                    job.result()
        # One at a time: gcloud writes the kubeconfig without a lock.
        for name in wanted:
            if not kube.has_context(kube.context_name(self.project, self.zone, name)):
                kube.get_credentials(self.project, self.zone, name)
        self.wait_registered(count)

    def _create_cluster(self, name: str) -> None:
        """Create a test cluster; a create GKE fails (an internal error) is deleted and tried again."""
        for attempt in range(1, CLUSTER_CREATE_ATTEMPTS + 1):
            LOG.info("creating test cluster %s (attempt %s)", name, attempt)
            try:
                # A plan can name another machine type, for a zone that runs out of the default one.
                machine_type = self.settings.get("test_cluster_machine_type", TEST_CLUSTER_MACHINE_TYPE)
                kube.create_cluster(self.project, self.zone, name, machine_type, DEFAULT_POOL_NODES, self.label)
                return
            except kube.CommandError:
                if attempt == CLUSTER_CREATE_ATTEMPTS:
                    raise
                LOG.warning("creating %s failed; deleting what GKE left and retrying", name)
                if name in kube.clusters_with_prefix(self.project, self.zone, self.prefix, self.label):
                    kube.delete_cluster(self.project, self.zone, name)

    def wait_registered(self, count: int) -> None:
        """Wait for a Cluster Agent profile per test cluster, then make the event watcher watch exactly them.

        The watcher builds its watch set once, at start, from the profiles that
        exist then: a profile the reconcile scaffolds later is not watched, and
        a deleted cluster stays watched, until the watcher restarts. So once the
        profiles match, a watch set that differs from the test fleet is fixed by
        restarting the gateway pod, which is what an operator would have to do.
        Whatever is still wrong after that is recorded on every run of the level.
        With ``trigger_reconcile`` set, a profile set that differs from the fleet
        runs the reconcile at once instead of waiting for its hourly tick.
        """
        names = set(self.cluster_names(count))
        self.watch_errors = []
        timeout = float(self.settings.get("cluster_registration_timeout_seconds", 0))
        deadline = time.monotonic() + timeout
        profiles = self.install.profile_clusters()
        if self.settings.get("trigger_reconcile") and self.test_clusters(profiles) != names:
            LOG.info("profiles for %s of %s test clusters; running the Cluster Agent reconcile",
                     len(profiles & names), count)
            try:
                operations.reconcile_profiles(self.install.context, self.install.namespace)
            except kube.CommandError as exc:
                LOG.warning("the reconcile failed; waiting for its hourly run instead: %s", exc)
            profiles = self.install.profile_clusters()
        stale = self.test_clusters(profiles) - names
        if stale:
            # A deleted test cluster's profile stays until the reconcile prunes it; the run says so.
            self.watch_errors.append(f"{STALE_PROFILES_NOTE}: {sorted(stale)}")
        while profiles & names != names:
            if time.monotonic() >= deadline:
                self.watch_errors.append(f"no Cluster Agent profile for {sorted(names - profiles)} after {timeout:.0f}s")
                break
            LOG.info("waiting for Cluster Agent profiles: %s of %s", len(profiles & names), count)
            time.sleep(REGISTRATION_POLL_SECONDS)
            profiles = self.install.profile_clusters()
        test_watched = self.test_clusters(self.install.watched_clusters())
        if test_watched != names:
            LOG.warning("event watcher watches %s, test fleet is %s; restarting the gateway so it re-reads profiles",
                        sorted(test_watched), sorted(names))
            operations.restart(self.install.context, self.install.namespace)
            time.sleep(WATCHER_START_SECONDS)
            test_watched = self._wait_watched(names)
            if test_watched != names:
                self.watch_errors.append(f"event watcher watches {sorted(test_watched)}, test fleet is {sorted(names)}")

    def _wait_watched(self, names: set[str]) -> set[str]:
        """The test clusters the watcher reports as up, polled until they are ``names`` or
        ``watcher_wait_seconds`` (default 0: one look) runs out; a large fleet takes longer to sync."""
        deadline = time.monotonic() + float(self.settings.get("watcher_wait_seconds", 0))
        while True:
            watched = self.test_clusters(self.install.watched_clusters())
            if watched == names or time.monotonic() >= deadline:
                return watched
            time.sleep(WATCHER_POLL_SECONDS)

    def ensure_nodes(self, values: dict[str, float]) -> None:
        """Size each test cluster's fixture pool for the level's node count and the Pods it must hold."""
        names = self.cluster_names(int(values["clusters"]))
        lifetime = int(self.settings["churn_pod_lifetime_seconds"])
        per_node = int(self.settings["pods_per_node"])
        # Fixture Pods the default pool holds before a fixture pool is needed; 0 puts every one there.
        on_default = int(self.settings.get("default_pool_pods", 0))
        sizes = {}
        for index, name in enumerate(names):
            spill = max(0, fixtures.fixture_pods(values, lifetime, first=index == 0) - on_default)
            sizes[name] = max(int(values["nodes"]) - DEFAULT_POOL_NODES, math.ceil(spill / per_node))
        if not names:
            return
        with ThreadPoolExecutor(max_workers=min(self.parallelism(len(names)), len(names))) as pool:
            jobs = [
                (name, size, pool.submit(kube.ensure_node_pool, self.project, self.zone, name,
                                         self.settings["fixture_pool"], self.settings["fixture_machine_type"], size))
                for name, size in sizes.items()
            ]
            for name, size, job in jobs:
                try:
                    job.result()
                except kube.CommandError as exc:
                    if size:
                        raise
                    # A pool the level does not need, mid-operation or failed: its nodes, if any come,
                    # are in the run's totals, and the level goes on without it.
                    LOG.warning("could not empty the fixture pool on %s; leaving it: %s", name, exc)

    # --- fixtures -----------------------------------------------------------------------------

    def ensure_fixtures(self, values: dict[str, float]) -> None:
        contexts = self.contexts(int(values["clusters"]))
        objects = int(values["objects"])
        with ThreadPoolExecutor(max_workers=min(FIXTURE_PARALLELISM, len(contexts))) as pool:
            for job in [pool.submit(self._ensure_objects, context, objects) for context in contexts]:
                job.result()
        first = contexts[0]
        kube.apply(first, fixtures.listing_manifests(int(values["listing_pods"])))
        kube.apply(first, fixtures.warning_manifests(int(values["failing_pods"])))

    def _ensure_objects(self, context: str, objects: int) -> None:
        if self._applied_level(context, "objects") not in (None, objects):
            fixtures.delete_fixture(context, "objects")
            self._wait_fixture_gone(context, "objects")
        manifests = fixtures.objects_manifests(objects)
        for manifest in manifests:
            if manifest["kind"] == "Namespace":
                manifest["metadata"]["labels"][LEVEL_LABEL] = str(objects)
        kube.apply(context, manifests)

    def _applied_level(self, context: str, fixture: str) -> int | None:
        items = kube.kubectl_json(context, "get", "namespaces", "-l", f"{fixtures.FIXTURE_LABEL}={fixture}")["items"]
        if not items:
            return None
        levels = {item["metadata"].get("labels", {}).get(LEVEL_LABEL, "") for item in items}
        # Namespaces of two levels mean an interrupted change; report a mismatch so it is redone.
        return int(levels.pop()) if len(levels) == 1 and next(iter(levels)).isdigit() else -1

    def _wait_fixture_gone(self, context: str, fixture: str) -> None:
        kube.kubectl(context, "wait", "--for=delete", "namespace", "-l", f"{fixtures.FIXTURE_LABEL}={fixture}",
                     f"--timeout={kube.APPLY_TIMEOUT_SECONDS}s", timeout=kube.APPLY_TIMEOUT_SECONDS)

    def preflight(self) -> None:
        """Run the target's preflight command, if it names one, before a level and before each run.

        A project whose IAM an outside process rewrites (an organization's policy
        automation, a second operator) can point this at whatever restores the
        install's preconditions; a failure is logged and the run goes on.
        """
        command = self.plan.target.get("preflight")
        if not command:
            return
        try:
            kube.run([PREFLIGHT_SHELL, "-c", command], timeout=PREFLIGHT_TIMEOUT_SECONDS)
        except kube.CommandError as exc:
            LOG.warning("preflight failed: %s", exc)

    def apply_level(self, factor: str, level: float) -> dict[str, float]:
        values = self.plan.level_values(factor, level)
        LOG.info("applying %s=%s: %s", factor, level, values)
        # Forgotten until the level is in place, so a level that fails half-way is settled after.
        previous, self.applied_fleet = self.applied_fleet, None
        self.preflight()
        self.stop_churn()
        self.ensure_clusters(int(values["clusters"]))
        self.ensure_nodes(values)
        self.ensure_fixtures(values)
        self.churner = fixtures.Churner(self.contexts(int(values["clusters"])), values["churn_per_minute"],
                                        int(self.settings["churn_pod_lifetime_seconds"]))
        self.churner.start()
        fleet = {key: values[key] for key in FLEET_VALUES if key in values}
        if fleet != previous:
            settle = int(self.settings.get("settle_seconds", 0))
            LOG.info("settling for %ss", settle)
            time.sleep(settle)
        self.applied_fleet = fleet
        return values

    def scrub(self, text: str) -> str:
        """Replace the target project's ID in recorded text, so the committed data names no project."""
        return text.replace(self.project, PROJECT_PLACEHOLDER)

    @staticmethod
    def mark_shared(record: records.Record, kind: str) -> None:
        """Leave a short run out of the fits that work other than its own loaded.

        A prompt or boot-burst run, shorter than the Stall Watch's period, is marked when it
        overlaps a tick; a proxy load run when the broker ran a command its workers did not send.
        """
        fits: tuple[str, ...] = ()
        if kind in STALL_WATCH_KINDS and _overlaps_stall_watch(record.started, record.ended, record.totals.get(CLUSTERS_TOTAL, 0)):
            note, fits = STALL_WATCH_NOTE, RESOURCE_FITS
        elif kind == PROXY_LOAD_KIND:
            readings = {m.name: m.value for m in record.metrics if m.container is None}
            issued = readings.get("proxy_load_completed", 0.0) + readings.get("proxy_load_errors", 0.0)
            outside = readings.get("proxy_commands", 0.0) - issued
            if outside > 0:
                # The run makes no model call, so tokens in its window are the other work's too.
                note, fits = f"{OUTSIDE_COMMANDS_NOTE}: {outside:.0f}", RESOURCE_FITS + PROXY_LOAD_FITS + TOKEN_FITS
        if not fits:
            return
        if not any(e.startswith(note.split(":")[0]) for e in record.errors):
            record.errors.append(note)
        record.excluded_from += [fit for fit in fits if fit not in record.excluded_from]

    def reclassify(self) -> int:
        """Apply the current rules to every stored record; returns how many changed.

        The rules for what counts as a failed or unfinished prompt improve over
        time; a record keeps its answers and errors, so it can be judged again
        without re-running it. A prompt record without answers was written before
        answers were kept, and its operation's completion cannot be checked.
        """
        data = records.load(self.data_file)
        changed = []
        unnamed = 0
        for raw in data["records"]:
            record = records.Record.from_dict(raw)
            before = json.dumps(record.to_dict(), sort_keys=True)
            record.errors = [self.scrub(e) for e in record.errors]
            record.answers = [self.scrub(a) for a in record.answers]
            profiles = next((m.value for m in record.metrics if m.name == PROFILES_METRIC), None)
            clusters = record.totals.get(CLUSTERS_TOTAL)
            if profiles is not None and clusters and profiles > clusters:
                # More Cluster Agent profiles than test clusters: a larger level's profiles were still there.
                note = f"{STALE_PROFILES_NOTE}: {profiles:.0f} profiles for {clusters} test clusters"
                if note not in record.errors:
                    record.errors.append(note)
            operation = next((o for o in self.plan.operations if o.name == record.operation), None)
            unnamed += operation is None
            if operation and operation.kind == "prompt":
                # Each answer is judged on its own text; a delegation-wait marker in the run's errors
                # counts once, not once per answer.
                reasons = [operations.unfinished_reason(answer, []) for answer in record.answers or [""]]
                reasons += [operations.unfinished_reason("", [error]) for error in record.errors
                            if operations.DELEGATION_CEILING_MARKER in error]
                failed = any(e.startswith(operations.PROMPT_FAILED) for e in record.errors)
                if record.answers or failed:
                    # The count of prompts answered without their operation, from the kept answers.
                    record.metrics = [m for m in record.metrics if m.name != UNFINISHED_METRIC]
                    record.metrics.append(records.Metric(UNFINISHED_METRIC, float(sum(1 for r in reasons if r))))
                elif operation.name in NEEDS_ANSWER_CHECK:
                    # Written before answers were kept: whether the operation ran is unknown.
                    reasons.append(UNCHECKED_ANSWER)
                for reason in filter(None, reasons):
                    note = f"{operations.PROMPT_UNFINISHED}: {reason}"
                    if note not in record.errors:
                        record.errors.append(note)
                if any(reasons):
                    for fit in RESPONSE_FITS:
                        if fit not in record.excluded_from:
                            record.excluded_from.append(fit)
                if any(reason and reason != UNCHECKED_ANSWER for reason in reasons):
                    record.excluded_from += [f for f in RESOURCE_FITS + TOKEN_FITS if f not in record.excluded_from]
            if operation:
                self.mark_shared(record, operation.kind)
            if json.dumps(record.to_dict(), sort_keys=True) != before:
                changed.append(record)
        if unnamed:
            # The operation's kind decides its rules, and only the plan holds it.
            LOG.warning("%s records name an operation this plan lacks; run reclassify with their plans too", unnamed)
        records.merge(self.data_file, changed)
        return len(changed)

    def check_resettable(self) -> None:
        """Refuse to reset an install whose host cluster does not carry the plan's label.

        An onboarding run deletes the install's state volumes; the label is the
        operator's statement that this install exists to be measured.
        """
        key, _, value = self.label.partition("=")
        labels = kube.cluster_labels(self.project, self.plan.target["host_location"], self.plan.target["host_cluster"])
        if labels.get(key) != value:
            raise kube.CommandError(
                f"host cluster {self.plan.target['host_cluster']} does not carry {self.label}; refusing to reset it"
            )

    def stop_churn(self) -> None:
        if self.churner:
            self.churner.stop()
            self.churner = None

    # --- one run ------------------------------------------------------------------------------

    def execute(self, spec: RunSpec, values: dict[str, float]) -> records.Record:
        op = spec.operation
        count = int(values["clusters"])
        contexts = self.contexts(count)
        totals = kube.totals(contexts)
        limits = self.install.limits()
        requests = self.install.requests()
        replicas = self.install.replicas()
        image = self.install.image()
        mode = self.install.mode() or self.plan.target["mode"]
        if mode != self.plan.target["mode"]:
            self.watch_errors.append(f"install runs mode {mode!r}, plan says {self.plan.target['mode']!r}")
        commit = commit_of(image)
        carried, carried_fits = self.carried_work()
        errors = list(self.watch_errors) + carried
        if not commit:
            errors.append(f"image {image!r} is not pinned by commit; the record is keyed by its tag")
            commit = image.rsplit(":", 1)[-1] or "unknown"
        before = self.install.snapshot()
        churned_before = self.churner.created if self.churner else 0
        started_iso = _now_iso()
        LOG.info("run %s %s=%s #%s", op.name, spec.factor, spec.level, spec.run)
        if op.kind == "prompt":
            env = operations.agent_env(self.install.context, self.install.namespace, self.relay_port)
            outcome = operations.run_prompts(op, env, int(values["concurrent"]), self.cluster_names(count)[0])
        elif op.kind == "soak":
            outcome = operations.soak(op.window_seconds)
        elif op.kind == "restart_soak":
            operations.restart(self.install.context, self.install.namespace)
            outcome = operations.soak(op.window_seconds)
        elif op.kind == PROXY_LOAD_KIND:
            concurrency = int(values.get(PROXY_CONCURRENCY, 1))
            kubeconfig_glob = PROFILE_KUBECONFIG_GLOB.format(project=self.project, cluster=self.cluster_names(count)[0])
            outcome = operations.proxy_load(self.install.context, self.install.namespace, op, concurrency, kubeconfig_glob)
        elif op.kind == TRIGGER_SOAK_KIND:
            # Work the install would start later (the OOBE first-run audits) is started now, so a
            # short window holds its peak; jobs still running at the window's end are noted.
            outcome = operations.OperationResult()
            outcome.errors += operations.trigger_jobs(self.install.context, self.install.namespace, op.jobs_home, op.jobs)
            outcome.errors += operations.soak(op.window_seconds).errors
            outcome.errors += operations.unfinished_jobs(self.install.context, self.install.namespace, op.jobs_home,
                                                         op.jobs, started_iso)
        elif op.kind == "reset_soak":
            self.check_resettable()
            operations.reset(self.install.context, self.install.namespace)
            outcome = operations.soak(op.window_seconds)
        else:
            raise ValueError(f"unknown operation kind {op.kind!r}")
        ended_iso = _now_iso()
        minutes = max((_epoch(ended_iso) - _epoch(started_iso)) / SECONDS_PER_MINUTE, 1 / SECONDS_PER_MINUTE)
        after = self.install.snapshot()
        metrics = collect.counter_metrics(before, after)
        metrics += self.install.pvc_metrics()
        logs = self.install.log_counts(started_iso)
        seen = next((m.value for m in metrics if m.name == "watcher_events_seen"), None)
        if seen is not None:
            metrics.append(records.Metric("watcher_events_seen_per_minute", seen / minutes))
        if self.churner:
            metrics.append(records.Metric("churn_achieved_per_minute", (self.churner.created - churned_before) / minutes))
            errors += self.churner.errors
            self.churner.errors = []
        metrics.append(records.Metric("triage_lost", float(logs["triage_lost"])))
        # The same refusal can reach both the client and the agent's log; count it once.
        metrics.append(records.Metric("run_slot_refusals", float(max(logs["run_slot_refusals"], outcome.run_slot_refusals))))
        metrics.append(records.Metric("failed_prompts", float(outcome.failed_prompts)))
        if op.kind == "prompt":
            metrics.append(records.Metric("unfinished_prompts", float(outcome.unfinished_prompts)))
        if outcome.response_seconds:
            metrics.append(records.Metric("response_seconds", max(outcome.response_seconds)))
        for name, value in outcome.metrics.items():
            metrics.append(records.Metric(name, value))
        # tokens_* (from LiteLLM) count every model call in the window, background jobs included, and
        # their input counts the whole prompt; run_tokens_* (from the harness) count the sessions this
        # run's prompts opened, and their input is the non-cached part alone.
        for name, value in outcome.tokens.items():
            metrics.append(records.Metric(f"run_{name}", value))
        errors += outcome.errors + before.errors + after.errors
        # LiteLLM logs every retry of one refusal; the client sees one per prompt. Take the larger.
        provider_429s = max(logs["provider_429s"], outcome.provider_429s)
        excluded = RESPONSE_FITS if op.kind == "prompt" and (provider_429s or outcome.unfinished_prompts) else []
        if op.kind == "prompt" and outcome.unfinished_prompts:
            excluded = excluded + list(RESOURCE_FITS + TOKEN_FITS)
        errors = [self.scrub(e) for e in errors]
        record = records.Record(
            commit=commit, mode=mode, phase=self.plan.phase, runner=RUNNER_REVISION,
            factor=spec.factor, level=spec.level, operation=op.name, run=spec.run,
            started=started_iso, ended=ended_iso, image=image, values=values, totals=totals,
            limits=limits, requests=requests, replicas=replicas, metrics=metrics,
            provider_429s=provider_429s, answers=[self.scrub(a) for a in outcome.previews],
            excluded_from=list(excluded) + [f for f in carried_fits if f not in excluded], errors=errors,
        )
        self.mark_shared(record, op.kind)
        return record

    def pending(self, specs: list[RunSpec]) -> list[RunSpec]:
        """The runs whose key is not in the data file yet, for the build the install runs now."""
        stored = records.load(self.data_file)["records"]
        done = {record["key"]: record.get("values", {}) for record in stored}
        phases = {record["key"]: record.get("phase", "") for record in stored}
        operations.wait_gateway_ready(self.install.context, self.install.namespace)
        image = self.install.image()
        commit = commit_of(image) or image.rsplit(":", 1)[-1] or "unknown"
        mode = self.install.mode() or self.plan.target["mode"]
        pending = []
        for spec in specs:
            key = records.record_key(commit, mode, spec.factor, spec.level, spec.operation.name, spec.run)
            if key not in done or not self.same_point(done[key], spec):
                pending.append(spec)
            elif phases[key] != self.plan.phase:
                # A key carries the operation's name, not the plan's phase: say when another phase's
                # record stands for this run, so a reused name does not skip it unnoticed.
                LOG.warning("%s is already recorded by %s; not running it again", key, phases[key])
        return pending

    def same_point(self, stored: dict[str, float], spec: RunSpec) -> bool:
        """Whether a stored combined run measured this plan's point: a combined level is only an
        index, so another plan's (or an edited) Nth point has the same key."""
        if spec.factor != COMBINED_FACTOR:
            return True
        point = self.plan.combined[int(spec.level) - 1]
        return all(stored.get(name) == value for name, value in point.items())

    def run(self, specs: list[RunSpec], force: bool, limit: int = 0) -> bool:
        """Run the specs; False when it stopped at a level it could not apply."""
        self.backfill_peaks()
        self.triggered = self.last_trigger()
        pending = specs if force else self.pending(specs)
        todo = pending[:limit] if limit else pending
        LOG.info("%s runs to do (%s pending of %s)", len(todo), len(pending), len(specs))
        awaiting: list[tuple[float, records.Record]] = []
        stopped = False
        relay = operations.Relay(self.install.context, self.install.namespace)
        try:
            relay.start()
            self.relay_port = relay.port
            for (factor, level), group in itertools.groupby(todo, key=lambda s: (s.factor, s.level)):
                try:
                    values = self.apply_level(factor, level)
                except Exception:
                    # Stop: the next level would reshape the fleet on top of a half-applied one (delete
                    # the clusters this level made, or create more in a zone that just refused some).
                    # A resume starts again at this level.
                    LOG.exception("could not apply %s=%s; stopping the run", factor, level)
                    stopped = True
                    break
                for spec in group:
                    try:
                        self.preflight()
                        if spec.operation.kind != "reset_soak":
                            # A reset (or a restart the agent did itself) leaves the watcher on the host
                            # cluster only; every other operation is measured with the fleet watched.
                            self.wait_registered(int(values["clusters"]))
                        record = self.execute(spec, values)
                    except Exception:  # one failed run is skipped; its key stays pending for a resume
                        LOG.exception("run %s %s=%s #%s failed; skipping it", spec.operation.name, factor, level, spec.run)
                        continue
                    # Merged at once without its peaks, so a crash loses nothing; merged again with them.
                    records.merge(self.data_file, [record])
                    if spec.operation.kind == TRIGGER_SOAK_KIND:
                        self.drain_jobs(spec.operation, record.started)
                        record.metrics += self.job_metrics(spec.operation, record.started)
                        # Written now: the Monitoring wait below can outlast the process.
                        records.merge(self.data_file, [record])
                    awaiting.append((time.time(), record))
                    awaiting = self.flush_peaks(awaiting, wait=False)
                    self.backfill_peaks(skip={pending.key for _, pending in awaiting})
        finally:
            self.stop_churn()
            relay.stop()
            self.flush_peaks(awaiting, wait=True)
        return not stopped

    def dry_run(self, specs: list[RunSpec], force: bool) -> list[str]:
        """What ``run`` would do, as lines: each level's cluster creates and deletes, and each run.

        Reads the project's clusters and, unless ``force``, the data file and the install's build to
        leave out runs already recorded; changes nothing.
        """
        pending = specs if force else self.pending(specs)
        current = set(kube.clusters_with_prefix(self.project, self.zone, self.prefix, self.label))
        lines = [f"{len(pending)} runs to do (of {len(specs)}); test clusters now: {len(current)}"]
        for (factor, level), group in itertools.groupby(pending, key=lambda s: (s.factor, s.level)):
            values = self.plan.level_values(factor, level)
            wanted = set(self.cluster_names(int(values[CLUSTERS_TOTAL])))
            create, delete = sorted(wanted - current), sorted(current - wanted)
            lines.append(f"level {factor}={level:g}: {dict(values)}")
            lines.append(f"  create {len(create)} clusters: {' '.join(create) or '-'}")
            lines.append(f"  delete {len(delete)} clusters: {' '.join(delete) or '-'}")
            for spec in group:
                calls = "model calls" if spec.operation.kind in MODEL_CALL_KINDS else "no model call of its own"
                lines.append(f"  run {spec.operation.name} #{spec.run} ({spec.operation.kind}, {calls})")
            current = wanted
        return lines

    def drain_jobs(self, op: Operation, since: str) -> None:
        """Wait up to ``trigger_drain_seconds`` for the jobs a trigger_soak started, so they do not load the next run.

        Every later run checks them again (``carried_work``) until they have finished.
        """
        self.triggered = (op, since)
        deadline = time.monotonic() + float(self.settings.get("trigger_drain_seconds", TRIGGER_DRAIN_SECONDS))
        while time.monotonic() < deadline:
            try:
                statuses = operations.job_statuses(self.install.context, self.install.namespace, op.jobs_home,
                                                   op.jobs, since)
            except kube.CommandError:
                return
            active = [job for job, status in statuses.items() if status in operations.JOB_ACTIVE]
            if not active:
                return
            LOG.info("waiting for %s to finish before the next run", ", ".join(active))
            time.sleep(DRAIN_POLL_SECONDS)

    def job_metrics(self, op: Operation, since: str) -> list[records.Metric]:
        """How long the slowest job the run started took, and how many completed, read after the drain."""
        try:
            durations = operations.job_seconds(self.install.context, self.install.namespace, op.jobs_home, op.jobs, since)
        except (kube.CommandError, ValueError, TypeError) as exc:
            # A run log the read cannot parse costs this run its job metrics, not the whole campaign.
            LOG.warning("could not read how long the jobs took: %s", exc)
            return []
        metrics = [records.Metric(JOBS_COMPLETED_METRIC, float(len(durations)))]
        if durations:
            metrics.append(records.Metric(JOB_SECONDS_METRIC, max(durations.values())))
        return metrics

    def carried_work(self) -> tuple[list[str], list[str]]:
        """Notes and fit exclusions for a run that jobs the latest trigger_soak started are still loading.

        A job marked due is claimed at the next cron tick, within a minute, so a job with no run
        after the window was never marked (it is not on the roster) and is not waited for.
        """
        if not self.triggered:
            return [], []
        op, since = self.triggered
        try:
            statuses = operations.job_statuses(self.install.context, self.install.namespace, op.jobs_home, op.jobs, since)
        except kube.CommandError as exc:
            return [f"could not tell whether the jobs an earlier run started had finished: {exc}"], list(RESOURCE_FITS)
        active = [job for job, status in statuses.items() if status in operations.JOB_ACTIVE]
        if not active:
            self.triggered = None
            return [], []
        return [f"{job}, started by an earlier run, was {statuses[job]}" for job in active], list(RESOURCE_FITS)

    def last_trigger(self) -> tuple[Operation, str] | None:
        """The latest stored trigger_soak run of this plan, so a resumed runner still checks its jobs."""
        kinds = {op.name: op for op in self.plan.operations if op.kind == TRIGGER_SOAK_KIND}
        runs = [r for r in records.load(self.data_file)["records"] if r["operation"] in kinds]
        if not runs:
            return None
        latest = max(runs, key=lambda r: r["started"])
        return kinds[latest["operation"]], latest["started"]

    def flush_peaks(self, awaiting: list[tuple[float, records.Record]], wait: bool) -> list[tuple[float, records.Record]]:
        """Add Cloud Monitoring peaks to the records whose window is old enough, and merge them again.

        Monitoring needs a few minutes before a window's last points are
        queryable, so a record waits here while the next run proceeds.
        """
        left: list[tuple[float, records.Record]] = []
        for finished, record in awaiting:
            remaining = finished + MONITORING_DELAY_SECONDS - time.time()
            if remaining > 0 and not wait:
                left.append((finished, record))
                continue
            if remaining > 0:
                time.sleep(remaining)
            self.add_peaks(record)
            records.merge(self.data_file, [record])
            LOG.info("recorded %s", record.key)
        return left

    def backfill_peaks(self, skip: set[str] | None = None) -> None:
        """Add peaks to records a stopped run merged without them (it stopped inside the wait).

        Runs at start and after every run, so a record that was too fresh at start is
        filled once Monitoring has its window; ``skip`` names records this run still holds.
        """
        data = records.load(self.data_file)
        # A record whose Monitoring read already failed keeps its error and is not queried again.
        stale = [r for r in data["records"] if not any(m["name"] == PEAK_METRIC for m in r["metrics"])
                 and not any(e.startswith(MONITORING_ERROR) for e in r["errors"])
                 and r["key"] not in (skip or set())
                 and _epoch(r["ended"]) + MONITORING_DELAY_SECONDS < time.time()]
        for raw in stale:
            record = records.Record.from_dict(raw)
            self.add_peaks(record)
            records.merge(self.data_file, [record])
            LOG.info("back-filled peaks for %s", record.key)

    def add_peaks(self, record: records.Record) -> None:
        try:
            peaks = self.install.container_peaks(_epoch(record.started), _epoch(record.ended))
        except Exception as exc:  # a monitoring failure costs this record its peaks, not the campaign
            record.errors.append(f"{MONITORING_ERROR} {type(exc).__name__}: {exc}")
            return
        if not peaks:
            # No series at all (system metrics off, or not ingested yet): say so, and stop asking.
            record.errors.append(f"{MONITORING_ERROR} no container series for {self.install.cluster}/"
                                 f"{self.install.namespace} in [{record.started}, {record.ended}]")
            return
        for container, values in peaks.items():
            for name, value in values.items():
                record.metrics.append(records.Metric(name, value, container))

    def remonitor(self) -> int:
        """Read every stored record's Cloud Monitoring metrics again, with the current queries.

        A change in what the runner reads from Monitoring then reaches the stored records,
        within Monitoring's retention; a record it cannot read keeps what it has. Returns how
        many records changed.
        """
        data = records.load(self.data_file)
        changed = []
        for raw in data["records"]:
            record = records.Record.from_dict(raw)
            try:
                peaks = self.install.container_peaks(_epoch(record.started), _epoch(record.ended))
            except Exception as exc:  # one unreadable window keeps its record as it is
                LOG.warning("could not read Monitoring for %s: %s", record.key, exc)
                continue
            if not peaks:
                continue
            record.metrics = [m for m in record.metrics if m.name not in MONITORING_METRICS] + [
                records.Metric(name, value, container)
                for container, values in peaks.items() for name, value in values.items()
            ]
            record.errors = [e for e in record.errors if not e.startswith(MONITORING_ERROR)]
            record.remonitored = RUNNER_REVISION
            changed.append(record)
        records.merge(self.data_file, changed)
        return len(changed)

    def teardown(self, delete_fleet: bool) -> None:
        """Remove every fixture and shrink the pools; ``delete_fleet`` deletes the test clusters too."""
        self.stop_churn()
        existing = kube.clusters_with_prefix(self.project, self.zone, self.prefix, self.label)
        # The runner's kubeconfig is a fresh copy each time; fetch the test clusters' contexts into it.
        for name in existing:
            if not kube.has_context(kube.context_name(self.project, self.zone, name)):
                kube.get_credentials(self.project, self.zone, name)
        if delete_fleet:
            with ThreadPoolExecutor(max_workers=max(1, len(existing))) as pool:
                for job in [pool.submit(kube.delete_cluster, self.project, self.zone, name) for name in existing]:
                    job.result()
            return
        for name in existing:
            context = kube.context_name(self.project, self.zone, name)
            for fixture in FIXTURES:
                fixtures.delete_fixture(context, fixture)
            # Shrink a fixture pool that exists; never create one just to leave it empty.
            if kube.node_pool_size(self.project, self.zone, name, self.settings["fixture_pool"]) is not None:
                kube.ensure_node_pool(self.project, self.zone, name, self.settings["fixture_pool"],
                                      self.settings["fixture_machine_type"], 0)


@contextlib.contextmanager
def hold_lease(plan: Plan, note: str):
    """Hold the experiment install's live-test lease for as long as the block runs.

    The lease is the repository's (scripts/live_test_lease.py, docs/designs/live-test-lease.md): a
    ConfigMap in the install's namespace that another runner, an agent's live validation or a
    person's work also takes before changing the install, so two of them cannot apply at once.
    A lease another holder has refuses the run.
    """
    target = plan.target
    with tempfile.TemporaryDirectory(prefix=LEASE_DIRECTORY_PREFIX) as directory:
        envs = Path(directory) / LEASE_ENVS_NAME
        envs.write_text(json.dumps({"installs": [
            {"name": LEASE_ENV_NAME, "context": target["host_context"], "namespace": target["namespace"]}
        ]}))
        env = {**os.environ, LEASE_ENVS_VAR: str(envs), LEASE_SESSION_VAR: f"{LEASE_SESSION_PREFIX}{os.getpid()}"}
        ttl = str(int(plan.settings.get("lease_ttl_minutes", LEASE_TTL_MINUTES)))

        def lease(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run([sys.executable, str(LEASE_SCRIPT), *args, "--env", LEASE_ENV_NAME],
                                  env=env, capture_output=True, text=True, check=False)

        acquired = lease("acquire", "--ttl", ttl, "--note", note)
        if acquired.returncode != 0:
            # Held by another holder, or not resolvable at all (a checkout's install.env naming the same
            # context, a kubectl failure): the script's own output says which.
            raise SystemExit(f"could not take the install's live-test lease (exit {acquired.returncode}); "
                             f"not running:\n{acquired.stdout}{acquired.stderr}")
        LOG.info("holding the live-test lease on %s for up to %s minutes", target["host_cluster"], ttl)
        try:
            yield
        finally:
            released = lease("release")
            if released.returncode != 0:
                LOG.warning("could not release the live-test lease; it expires on its own after %s minutes: %s",
                            ttl, released.stdout + released.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["plan", "run", "apply", "teardown", "reclassify", "remonitor"])
    parser.add_argument("config", type=Path)
    parser.add_argument("--only-factor")
    parser.add_argument("--only-operation")
    parser.add_argument("--factor", default=BASE_FACTOR)
    parser.add_argument("--level", type=float, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many runs (0: all)")
    parser.add_argument("--delete-fleet", action="store_true", help="teardown: delete the test clusters")
    parser.add_argument("--dry-run", action="store_true",
                        help="run: print each level's cluster creates and deletes and each run, and change nothing")
    args = parser.parse_args(argv)
    # SIGTERM ends the run like Ctrl-C does, so the churner, the relay and pending peaks are cleaned up.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(TERMINATED_EXIT_CODE))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    plan = load_plan(args.config, validate_target=args.command != "plan")
    specs = [
        s for s in plan.matrix()
        if (not args.only_factor or s.factor == args.only_factor)
        and (not args.only_operation or s.operation.name == args.only_operation)
    ]
    if args.limit and args.command == "plan":
        specs = specs[: args.limit]
    if args.command == "apply" and args.factor != BASE_FACTOR and not args.level:
        parser.error("apply needs --level for a factor other than base")
    if args.command == "plan":
        for spec in specs:
            print(f"{spec.factor:18} {spec.level:>8g} {spec.operation.name:18} run {spec.run}")
        print(f"{len(specs)} runs")
        return 0
    data_file = args.config.resolve().parent / plan.target.get("data_file", DEFAULT_DATA_FILE)
    handle, name = tempfile.mkstemp(prefix=PRIVATE_KUBECONFIG_PREFIX, suffix=KUBECONFIG_SUFFIX)
    os.close(handle)
    private = Path(name)
    kube.use_private_kubeconfig(private)
    atexit.register(private.unlink, missing_ok=True)
    runner = Runner(plan, data_file)
    if args.command == "run" and args.dry_run:
        print("\n".join(runner.dry_run(specs, args.force)))
        return 0
    if args.command in MUTATING_COMMANDS:
        with hold_lease(plan, f"harness scale {args.command} {args.config.name}"):
            return _mutate(runner, args, specs)
    if args.command == "reclassify":
        print(f"{runner.reclassify()} records changed")
    elif args.command == "remonitor":
        print(f"{runner.remonitor()} records changed")
    return 0


def _mutate(runner: Runner, args: argparse.Namespace, specs: list[RunSpec]) -> int:
    """The commands that change the test clusters or the install, run under the lease."""
    if args.command == "run":
        # Non-zero when the run stopped at a level, so a script does not go on to its next step.
        return 0 if runner.run(specs, args.force, args.limit) else STOPPED_EXIT_CODE
    if args.command == "apply":
        try:
            runner.apply_level(args.factor, args.level)
        finally:
            runner.stop_churn()
        return 0
    runner.teardown(args.delete_fleet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
