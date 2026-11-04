"""Run a measurement plan: apply each level, time each operation, merge the records.

    python -m kube_agents_bench.scale.runner plan <plan.yaml> [--only-factor F] [--only-operation O] [--limit N]
    python -m kube_agents_bench.scale.runner run <plan.yaml> [--only-factor F] [--only-operation O] [--limit N] [--force]
    python -m kube_agents_bench.scale.runner apply <plan.yaml> --factor F --level L
    python -m kube_agents_bench.scale.runner teardown <plan.yaml> [--delete-fleet]
    python -m kube_agents_bench.scale.runner reclassify <plan.yaml>

``run`` skips every run whose key is already in the data file, so an
interrupted run resumes where it stopped; ``--force`` re-runs and replaces.
The target (project, host cluster, test-cluster prefix and label) comes from
the plan's ``target`` block and ``SCALE_*`` environment variables.
"""

from __future__ import annotations

import argparse
import atexit
import itertools
import json
import logging
import math
import os
import re
import signal
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from kube_agents_bench.scale import collect, fixtures, kube, operations, records
from kube_agents_bench.scale.config import BASE_FACTOR, Plan, RunSpec, load_plan

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
RESPONSE_FITS = ["response_seconds"]
LEVEL_LABEL = "harness-scale/level"
ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DEFAULT_DATA_FILE = "harness-limits.json"
SECONDS_PER_MINUTE = 60
FIXTURES = ("objects", "listing", "warning", "churn")
# The tag of an image pinned by commit is the commit; anything else is recorded as the image only.
COMMIT_LENGTH = 40
HEX_DIGITS = set("0123456789abcdef")
PREFLIGHT_SHELL = "/bin/sh"
PREFLIGHT_TIMEOUT_SECONDS = 600
TERMINATED_EXIT_CODE = 143
PEAK_METRIC = "memory_peak_bytes"
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

    # --- the fleet ----------------------------------------------------------------------------

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
            with ThreadPoolExecutor(max_workers=len(missing) + len(extra)) as pool:
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
                kube.create_cluster(self.project, self.zone, name, TEST_CLUSTER_MACHINE_TYPE,
                                    DEFAULT_POOL_NODES, self.label)
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
        """
        names = set(self.cluster_names(count))
        self.watch_errors = []
        timeout = float(self.settings.get("cluster_registration_timeout_seconds", 0))
        deadline = time.monotonic() + timeout
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
            test_watched = self.test_clusters(self.install.watched_clusters())
            if test_watched != names:
                self.watch_errors.append(f"event watcher watches {sorted(test_watched)}, test fleet is {sorted(names)}")

    def ensure_nodes(self, values: dict[str, float]) -> None:
        """Size each test cluster's fixture pool for the level's node count and the Pods it must hold."""
        names = self.cluster_names(int(values["clusters"]))
        lifetime = int(self.settings["churn_pod_lifetime_seconds"])
        per_node = int(self.settings["pods_per_node"])
        sizes = {}
        for index, name in enumerate(names):
            pods = fixtures.fixture_pods(values, lifetime, first=index == 0)
            sizes[name] = max(int(values["nodes"]), DEFAULT_POOL_NODES + math.ceil(pods / per_node)) - DEFAULT_POOL_NODES
        if not names:
            return
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            jobs = [
                pool.submit(kube.ensure_node_pool, self.project, self.zone, name, self.settings["fixture_pool"],
                            self.settings["fixture_machine_type"], size)
                for name, size in sizes.items()
            ]
            for job in jobs:
                job.result()

    # --- fixtures -----------------------------------------------------------------------------

    def ensure_fixtures(self, values: dict[str, float]) -> None:
        contexts = self.contexts(int(values["clusters"]))
        objects = int(values["objects"])
        for context in contexts:
            if self._applied_level(context, "objects") not in (None, objects):
                fixtures.delete_fixture(context, "objects")
                self._wait_fixture_gone(context, "objects")
            manifests = fixtures.objects_manifests(objects)
            for manifest in manifests:
                if manifest["kind"] == "Namespace":
                    manifest["metadata"]["labels"][LEVEL_LABEL] = str(objects)
            kube.apply(context, manifests)
        first = contexts[0]
        kube.apply(first, fixtures.listing_manifests(int(values["listing_pods"])))
        kube.apply(first, fixtures.warning_manifests(int(values["failing_pods"])))

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
        self.preflight()
        self.stop_churn()
        self.ensure_clusters(int(values["clusters"]))
        self.ensure_nodes(values)
        self.ensure_fixtures(values)
        self.churner = fixtures.Churner(self.contexts(int(values["clusters"])), values["churn_per_minute"],
                                        int(self.settings["churn_pod_lifetime_seconds"]))
        self.churner.start()
        settle = int(self.settings.get("settle_seconds", 0))
        LOG.info("settling for %ss", settle)
        time.sleep(settle)
        return values

    def scrub(self, text: str) -> str:
        """Replace the target project's ID in recorded text, so the committed data names no project."""
        return text.replace(self.project, PROJECT_PLACEHOLDER)

    def reclassify(self) -> int:
        """Apply the current rules to every stored record; returns how many changed.

        The rules for what counts as a failed or unfinished prompt improve over
        time; a record keeps its answers and errors, so it can be judged again
        without re-running it. A prompt record without answers was written before
        answers were kept, and its operation's completion cannot be checked.
        """
        data = records.load(self.data_file)
        changed = []
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
            if json.dumps(record.to_dict(), sort_keys=True) != before:
                changed.append(record)
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
        replicas = self.install.replicas()
        image = self.install.image()
        mode = self.install.mode() or self.plan.target["mode"]
        if mode != self.plan.target["mode"]:
            self.watch_errors.append(f"install runs mode {mode!r}, plan says {self.plan.target['mode']!r}")
        commit = commit_of(image)
        errors = list(self.watch_errors)
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
        # tokens_* (from LiteLLM) count every model call in the window, background jobs included, and
        # their input counts the whole prompt; run_tokens_* (from the harness) count the sessions this
        # run's prompts opened, and their input is the non-cached part alone.
        for name, value in outcome.tokens.items():
            metrics.append(records.Metric(f"run_{name}", value))
        errors += outcome.errors + before.errors + after.errors
        # LiteLLM logs every retry of one refusal; the client sees one per prompt. Take the larger.
        provider_429s = max(logs["provider_429s"], outcome.provider_429s)
        excluded = RESPONSE_FITS if op.kind == "prompt" and (provider_429s or outcome.unfinished_prompts) else []
        errors = [self.scrub(e) for e in errors]
        return records.Record(
            commit=commit, mode=mode, phase=self.plan.phase, runner=RUNNER_REVISION,
            factor=spec.factor, level=spec.level, operation=op.name, run=spec.run,
            started=started_iso, ended=ended_iso, image=image, values=values, totals=totals,
            limits=limits, replicas=replicas, metrics=metrics,
            provider_429s=provider_429s, answers=[self.scrub(a) for a in outcome.previews],
            excluded_from=list(excluded), errors=errors,
        )

    def pending(self, specs: list[RunSpec]) -> list[RunSpec]:
        """The runs whose key is not in the data file yet, for the build the install runs now."""
        done = records.existing_keys(self.data_file)
        operations.wait_gateway_ready(self.install.context, self.install.namespace)
        image = self.install.image()
        commit = commit_of(image) or image.rsplit(":", 1)[-1] or "unknown"
        mode = self.install.mode() or self.plan.target["mode"]
        return [s for s in specs
                if records.record_key(commit, mode, s.factor, s.level, s.operation.name, s.run) not in done]

    def run(self, specs: list[RunSpec], force: bool, limit: int = 0) -> None:
        self.backfill_peaks()
        pending = specs if force else self.pending(specs)
        todo = pending[:limit] if limit else pending
        LOG.info("%s runs to do (%s pending of %s)", len(todo), len(pending), len(specs))
        awaiting: list[tuple[float, records.Record]] = []
        relay = operations.Relay(self.install.context, self.install.namespace)
        try:
            relay.start()
            self.relay_port = relay.port
            for (factor, level), group in itertools.groupby(todo, key=lambda s: (s.factor, s.level)):
                try:
                    values = self.apply_level(factor, level)
                except Exception:  # a level that cannot be applied is skipped; a resume retries it
                    LOG.exception("could not apply %s=%s; skipping its runs", factor, level)
                    continue
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
                    awaiting.append((time.time(), record))
                    awaiting = self.flush_peaks(awaiting, wait=False)
                    self.backfill_peaks(skip={pending.key for _, pending in awaiting})
        finally:
            self.stop_churn()
            relay.stop()
            self.flush_peaks(awaiting, wait=True)

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["plan", "run", "apply", "teardown", "reclassify"])
    parser.add_argument("config", type=Path)
    parser.add_argument("--only-factor")
    parser.add_argument("--only-operation")
    parser.add_argument("--factor", default=BASE_FACTOR)
    parser.add_argument("--level", type=float, default=0)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--limit", type=int, default=0, help="stop after this many runs (0: all)")
    parser.add_argument("--delete-fleet", action="store_true", help="teardown: delete the test clusters")
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
    if args.command == "run":
        runner.run(specs, args.force, args.limit)
    elif args.command == "reclassify":
        print(f"{runner.reclassify()} records changed")
    elif args.command == "apply":
        try:
            runner.apply_level(args.factor, args.level)
        finally:
            runner.stop_churn()
    else:
        runner.teardown(args.delete_fleet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
