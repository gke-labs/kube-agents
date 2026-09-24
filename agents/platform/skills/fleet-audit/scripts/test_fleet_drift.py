#!/usr/bin/env python3
"""Tests for fleet_drift.py, the fleet-consistency-drift collector."""

import contextlib
import copy
import io
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import fleet_drift as fd  # noqa: E402

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def run_of(rc: int, stdout: str = "", stderr: str = "") -> fd.Run:
    return fd.Run(["x"], rc, stdout, stderr, 0.01)


def collected(run, *, project="acme", **kwargs) -> dict:
    """`collect_fleet` over a fleet of one project that discovery *found*.

    Passing the project to `collect_fleet` directly is `--project`, which is a
    deliberately narrowed scope and leaves an `UNENUMERATED_PROJECTS` row for
    the projects it did not name. Every test that wants "one project, nothing
    lost" has to answer discovery instead, and answering it in one place keeps
    that row out of assertions that are not about it.
    """
    def discovering(argv, **kw):
        if argv[:3] == ["gcloud", "projects", "list"]:
            return run_of(0, project)
        if argv[:4] == ["gcloud", "config", "get-value", "project"]:
            return run_of(0, project)
        return run(argv, **kw)

    return fd.collect_fleet(run=discovering, **kwargs)


def cluster(name, project="acme", location="us-central1", autopilot=False, status="RUNNING", created="2020-01-01T00:00:00Z", labels=None, **overrides):
    doc = {
        "name": name, "_project": project, "location": location, "status": status, "createTime": created,
        "autopilot": {"enabled": autopilot},
        "resourceLabels": labels if labels is not None else {"environment": "prod"},
        "releaseChannel": {"channel": "REGULAR"},
        "shieldedNodes": {"enabled": True},
        "nodePools": [
            {
                "name": "default-pool",
                "config": {"shieldedInstanceConfig": {"enableSecureBoot": True, "enableIntegrityMonitoring": True}, "imageType": "COS_CONTAINERD"},
                "autoscaling": {"enabled": True},
            }
        ],
        "networkConfig": {"datapathProvider": "ADVANCED_DATAPATH", "enableIntraNodeVisibility": True},
        "networkPolicy": {"enabled": False},
        "privateClusterConfig": {"enablePrivateNodes": True, "enablePrivateEndpoint": True},
        "masterAuthorizedNetworksConfig": {"enabled": True, "cidrBlocks": ["10.0.0.0/8"]},
        "loggingConfig": {"componentConfig": {"enableComponents": ["SYSTEM_COMPONENTS", "WORKLOADS"]}},
        "monitoringConfig": {"componentConfig": {"enableComponents": ["SYSTEM_COMPONENTS"]}, "managedPrometheusConfig": {"enabled": True}},
        "binaryAuthorization": {"evaluationMode": "PROJECT_SINGLETON_POLICY_ENFORCE"},
        "autoscaling": {"enableNodeAutoprovisioning": True},
        "databaseEncryption": {"state": "ENCRYPTED"},
    }
    for path, value in overrides.items():
        target = doc
        keys = path.split(".")
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = value
    return doc


def pool(name, *, secure_boot=True, integrity=True, autoscaling=True, image="COS_CONTAINERD", taints=None):
    """A node pool for the cohorts below, varying one `_pool_fraction` input
    at a time so a fleet differs on exactly the facet a test is about."""
    config = {"shieldedInstanceConfig": {"enableSecureBoot": secure_boot, "enableIntegrityMonitoring": integrity}, "imageType": image}
    if taints:
        config["taints"] = taints
    return {"name": name, "config": config, "autoscaling": {"enabled": autoscaling}}


def K(name, project="acme", location="us-central1"):
    """`fd.ckey` for a cluster built by `cluster()` above.

    The collector keys its per-cluster dicts by `(project, location, name)`
    rather than by name, because a GKE cluster name is only unique inside its
    project and this is the collector that sweeps every project.
    """
    return (project, location, name)


def T(name, project="acme", location="us-central1"):
    """The manifest target name for a cluster built by `cluster()` above.

    `qualify_targets` names every cluster `<project>/<location>/<name>`,
    because a name is unique only inside one project and location, and because
    a name that is qualified only when it collides moves when the rest of the
    fleet changes -- and a finding whose cluster moves is announced resolved
    and refiled as new.
    """
    return f"{project}/{location}/{name}"


class DiscoverProjectsTest(unittest.TestCase):
    @staticmethod
    def _discovery_run(projects_stdout, clusters_by_project=None):
        """A `run` that answers the three argv shapes discovery issues."""
        clusters_by_project = clusters_by_project or {}

        def run(argv, **_):
            if argv[:4] == ["gcloud", "config", "get-value", "project"]:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, projects_stdout)
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                project = argv[argv.index("--project") + 1]
                return run_of(0, json.dumps(clusters_by_project.get(project, [])))
            raise AssertionError(f"unexpected argv {argv}")

        return run

    def test_the_given_project_is_the_whole_scope(self):
        """`--project` scopes the run. It used to be a *seed*: the inventory
        scrape ran unconditionally after it, so a scoped run still fanned out
        across everything the scrape produced."""
        calls = []

        def run(argv, **_):
            calls.append(argv)
            return run_of(0)

        self.assertEqual(fd.discover_projects("acme", run=run), ["acme"])
        self.assertEqual(calls, [])

    def test_falls_back_to_active_gcloud_project(self):
        result = fd.discover_projects(None, run=self._discovery_run(""))
        self.assertEqual(result, ["acme"])

    def test_a_projects_list_that_omits_the_active_project_is_a_narrowing(self):
        """rc 0 and the active project absent from its own output. The
        credential just resolved that project and is about to list clusters in
        it, so the listing is filtered rather than complete -- a role carrying
        `container.clusters.list` but not `resourcemanager.projects.get`, or a
        proxy dropping rows instead of denying the call. Unrecorded it is the
        same loss as the rc != 0 fallback beside it: `finish` reads a fleet of
        one project as the whole fleet and stale-closes every remediation pull
        request outside it."""
        discovery = fd.discover_fleet(None, run=self._discovery_run("other-proj\n"))
        self.assertEqual(discovery.projects, ["acme", "other-proj"])
        self.assertIsNone(discovery.error)
        self.assertIn("did not name the active project", discovery.partial)

    def test_a_projects_list_naming_the_active_project_is_complete(self):
        """The guard above must not fire on the ordinary install. A stock
        `kube-agents-iam` grants `roles/compute.viewer` and
        `roles/monitoring.viewer`, both of which carry
        `resourcemanager.projects.get`, so `projects list` does return the
        project the run is standing in -- and a `partial` on every run is the
        permanent coverage gap that pins `resolved` at 0."""
        discovery = fd.discover_fleet(None, run=self._discovery_run("acme\nacme-staging\n"))
        self.assertEqual(discovery.projects, ["acme", "acme-staging"])
        self.assertIsNone(discovery.partial)

    def test_every_project_projects_list_returns_is_in_scope(self):
        result = fd.discover_projects(
            None,
            run=self._discovery_run("acme\nacme-staging\nempty-proj\n", {"acme-staging": [{"name": "c1"}]}),
        )
        self.assertEqual(result, ["acme", "acme-staging", "empty-proj"])

    def test_discovery_names_projects_and_lists_none_of_them(self):
        """Scope used to be decided by listing every candidate here and
        keeping the ones that answered with a cluster, which made the sweep's
        thread pool a cache lookup over work already done serially. A project
        holding nothing contributes no manifest entry either way."""
        calls = []

        def run(argv, **_):
            calls.append(argv)
            if argv[:4] == ["gcloud", "config", "get-value", "project"]:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nacme-staging\n")
            raise AssertionError(f"discovery listed clusters: {argv}")

        self.assertEqual(fd.discover_projects(None, run=run), ["acme", "acme-staging"])
        self.assertEqual([a for a in calls if "clusters" in a], [])

    def test_an_unlistable_fleet_falls_back_to_the_base_project(self):
        def run(argv, **_):
            if argv[:4] == ["gcloud", "config", "get-value", "project"]:
                return run_of(0, "acme\n")
            return run_of(1, "", "PERMISSION_DENIED")

        self.assertEqual(fd.discover_projects(None, run=run), ["acme"])

    def test_an_unlistable_fleet_says_the_scope_is_short(self):
        """The fallback above is a narrowing, and a narrowing nobody records
        is one `finish` reads as a clean fleet. Every previous ledger finding
        on a cluster in a project this run never saw is then absent from the
        document, absent from the manifest, announced resolved and its
        remediation pull request stale-closed."""

        def run(argv, **_):
            if argv[:4] == ["gcloud", "config", "get-value", "project"]:
                return run_of(0, "acme\n")
            return run_of(1, "", "PERMISSION_DENIED")

        discovery = fd.discover_fleet(None, run=run)
        self.assertEqual(discovery.projects, ["acme"])
        self.assertIsNone(discovery.error)
        self.assertIn("PERMISSION_DENIED", discovery.partial)

    def test_a_fleet_of_no_projects_at_all_is_an_error(self):
        """Both calls answered rc 0 and named nothing. Without the key, the
        manifest is `{"clusters": []}` with exit 0 -- a fleet holding no
        clusters and a run that could not look are then the same document."""

        def run(argv, **_):
            return run_of(0, "")

        discovery = fd.discover_fleet(None, run=run)
        self.assertEqual(discovery.projects, [])
        self.assertIn("named no project", discovery.error)

    def test_english_prose_is_no_longer_a_source_of_project_ids(self):
        """The scrape read `/opt/data/INVENTORY.raw.md` -- model-written prose
        with no project-ID marker in its contract -- with a regex matching any
        lowercase word of six to thirty characters, so `cluster`, `namespace`,
        `production` and `monitoring` each became a target the run issued a
        `clusters list` against."""
        self.assertFalse(hasattr(fd, "PROJECT_ID_RE"))
        self.assertFalse(hasattr(fd, "INVENTORY_PATH"))
        self.assertNotIn("read_text", inspect.signature(fd.discover_projects).parameters)


class EnumerateProjectClustersTest(unittest.TestCase):
    def test_tags_each_cluster_with_its_project(self):
        clusters_json = json.dumps([{"name": "c1"}])
        clusters, record, error = fd.enumerate_project_clusters("acme", run=lambda a: run_of(0, clusters_json))
        self.assertEqual(clusters[0]["_project"], "acme")
        self.assertIsNotNone(record)
        self.assertIsNone(error)

    def test_failed_list_returns_empty_with_no_record_and_says_why(self):
        clusters, record, error = fd.enumerate_project_clusters("acme", run=lambda a: run_of(1, "", "denied"))
        self.assertEqual(clusters, [])
        self.assertIsNone(record)
        # The error travels back so `collect_fleet` can put it in the manifest.
        # A log line alone leaves the failure nowhere a validator can read it.
        self.assertIn("denied", error)
        self.assertIn("rc=1", error)

    def test_a_listing_some_zones_did_not_answer_returns_its_clusters_and_the_loss(self):
        """gcloud warns on stderr and exits 0, and `--format json` carries only
        the clusters that answered -- the API's `missingZones` is dropped by the
        formatter. Read as complete, the clusters in the silent zones are
        indistinguishable from clusters that do not exist: their cohorts vote
        without them, and `finish` resolves every ledger finding on them."""
        warning = ("WARNING: The following zones did not respond: "
                   "[us-central1-a, us-central1-b]. List results may be incomplete.\n")
        clusters, record, error = fd.enumerate_project_clusters(
            "acme", run=lambda a: run_of(0, json.dumps([{"name": "c1"}]), warning))
        # The clusters that did come back are real and still get compared.
        self.assertEqual([c["name"] for c in clusters], ["c1"])
        self.assertIsNotNone(record)
        # And the shortfall travels with them, naming the zones.
        self.assertIn("incomplete", error)
        self.assertIn("us-central1-a", error)

    def test_a_partial_listing_is_a_gate_failed_row_beside_its_own_clusters(self):
        """The error and the record travel together, so the caller has to keep
        both. Under an `elif` the record swallowed the error and the project
        read as fully enumerated -- the one outcome the warning exists to
        prevent."""
        warning = "WARNING: The following zones did not respond: [us-central1-a]."

        def run(argv, **_):
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                return run_of(0, json.dumps([cluster("c1")]), warning)
            raise AssertionError(f"unexpected argv {argv}")

        manifest = collected(run)
        rows = {e["name"]: e for e in manifest["clusters"]}
        # The cluster is in the manifest, collected, as it should be.
        self.assertEqual(rows[T("c1")]["outcome"], fd.OUTCOME_COLLECTED)
        # And so is the project, gate-failed, which is what §6 turns into a
        # coverage gap the document must account for.
        gate_failed = rows[f"{fd.PROJECT_TARGET_PREFIX}acme"]
        self.assertEqual(gate_failed["outcome"], fd.OUTCOME_GATE_FAILED)
        self.assertIn("us-central1-a", gate_failed["error"])


class ClusterEligibilityTest(unittest.TestCase):
    def test_running_and_old_is_eligible(self):
        self.assertIsNone(fd.cluster_eligibility(cluster("c1"), now=NOW))

    def test_reconciling_still_votes(self):
        """A reconcile is work in progress on a cluster that is otherwise up,
        and this module compares the configuration `clusters list` returns
        rather than reading inside the cluster. Excluding it dropped a cluster
        out of every cohort for the duration of any routine change -- and a
        cohort is a majority vote, so a missing member can flip the majority it
        was meant to define."""
        self.assertIsNone(fd.cluster_eligibility(cluster("c1", status="RECONCILING"), now=NOW))

    def test_provisioning_is_ineligible(self):
        reason = fd.cluster_eligibility(cluster("c1", status="PROVISIONING"), now=NOW)
        self.assertIn("PROVISIONING", reason)

    def test_brand_new_is_ineligible(self):
        reason = fd.cluster_eligibility(cluster("c1", created="2026-07-31T23:00:00Z"), now=NOW)
        self.assertIn("under 24h", reason)


class EnvironmentOfTest(unittest.TestCase):
    def test_reads_resource_label(self):
        self.assertEqual(fd.environment_of(cluster("c1", labels={"environment": "staging"})), ("staging", "label"))

    def test_normalizes_synonyms(self):
        self.assertEqual(fd.environment_of(cluster("c1", labels={"environment": "prd"})), ("prod", "label"))

    def test_infers_from_name_when_no_label(self):
        self.assertEqual(fd.environment_of(cluster("prod-usc1", labels={})), ("prod", "inferred"))

    def test_unknown_when_neither(self):
        self.assertEqual(fd.environment_of(cluster("cluster-one", labels={}))[0], "unknown")

    def test_prefers_label_over_name(self):
        self.assertEqual(fd.environment_of(cluster("dev-box", labels={"environment": "prod"})), ("prod", "label"))


class CohortStrategyTest(unittest.TestCase):
    def test_environment_strategy_when_any_cluster_has_one(self):
        clusters = [cluster("a", labels={"environment": "prod"}), cluster("b", labels={})]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "environment")

    def test_project_strategy_when_multi_project_and_no_environment(self):
        clusters = [cluster("a", project="p1", labels={}), cluster("b", project="p2", labels={})]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "project")

    def test_mode_only_as_last_resort(self):
        clusters = [cluster("a", project="p1", labels={}), cluster("b", project="p1", labels={})]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "mode-only")

    def test_a_sparse_name_guess_does_not_select_environment(self):
        """One `test` token in sixteen names is our inference, not the fleet's
        convention, and acting on it costs coverage rather than buying
        precision: the guessed cluster lands alone in a cohort of one and is
        compared against nothing. Cohorting by mode compares all sixteen."""
        clusters = [cluster("deploy-test", labels={})] + [cluster(f"c{i}", labels={}) for i in range(15)]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "mode-only")

    def test_two_guesses_in_sixteen_do_not_select_environment(self):
        """The docstring's fleet: two `test` tokens in sixteen names. A rule
        that trusted any two inferred environments would still pass the
        one-in-sixteen test above; this one pins the half-the-fleet bar."""
        clusters = [cluster("deploy-test", labels={}), cluster("perf-test", labels={})]
        clusters += [cluster(f"c{i}", labels={}) for i in range(14)]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "mode-only")

    def test_exactly_half_the_fleet_named_selects_environment(self):
        """The bar is inclusive: inference resolving half the fleet earns the axis."""
        clusters = [cluster(f"prod-{i}", labels={}) for i in range(4)]
        clusters += [cluster(f"c{i}", labels={}) for i in range(4)]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "environment")

    def test_one_short_of_half_does_not_select_environment(self):
        clusters = [cluster(f"prod-{i}", labels={}) for i in range(3)]
        clusters += [cluster(f"c{i}", labels={}) for i in range(4)]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "mode-only")

    def test_a_fleetwide_naming_convention_does_select_environment(self):
        """Inference earns the strategy once it is the fleet's actual naming
        convention rather than a guess about a couple of stragglers."""
        clusters = [cluster(f"prod-{i}", labels={}) for i in range(3)]
        clusters += [cluster(f"dev-{i}", labels={}) for i in range(3)]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "environment")

    def test_one_real_label_settles_it_without_a_majority(self):
        """A label is the customer declaring how they organize their fleet, so
        it does not need numbers behind it the way a guess does."""
        clusters = [cluster("a", labels={"environment": "prod"})]
        clusters += [cluster(f"c{i}", labels={}) for i in range(15)]
        self.assertEqual(fd.decide_cohort_strategy(clusters), "environment")


class ComputeBaselineTest(unittest.TestCase):
    def test_no_baseline_under_the_floor(self):
        self.assertIsNone(fd.compute_baseline({"a": "X", "b": "X"}))

    def test_no_baseline_below_two_thirds(self):
        self.assertIsNone(fd.compute_baseline({"a": "X", "b": "X", "c": "Y", "d": "Y"}))

    def test_baseline_at_exactly_two_thirds(self):
        result = fd.compute_baseline({"a": "X", "b": "X", "c": "Y"})
        self.assertEqual(result[0], "X")
        self.assertAlmostEqual(result[3], 2 / 3)

    def test_unanimous_baseline(self):
        result = fd.compute_baseline({"a": "X", "b": "X", "c": "X"})
        self.assertEqual(result, ("X", 3, 3, 1.0))


class SeverityLadderTest(unittest.TestCase):
    def test_high_confidence_keeps_base_severity(self):
        sev, downgrades = fd.apply_severity_ladder("critical", 1.0, 1, False)
        self.assertEqual(sev, "critical")
        self.assertEqual(downgrades, [])

    def test_r_under_90_drops_one_level(self):
        sev, _ = fd.apply_severity_ladder("critical", 0.85, 1, False)
        self.assertEqual(sev, "major")

    def test_r_under_80_drops_two_levels_cumulative(self):
        sev, _ = fd.apply_severity_ladder("critical", 0.75, 1, False)
        self.assertEqual(sev, "minor")

    def test_three_or_more_outliers_drops_one_level(self):
        sev, _ = fd.apply_severity_ladder("critical", 1.0, 3, False)
        self.assertEqual(sev, "major")

    def test_inferred_environment_drops_one_level(self):
        sev, _ = fd.apply_severity_ladder("critical", 1.0, 1, True)
        self.assertEqual(sev, "major")

    def test_a_major_facet_at_weak_confidence_is_dropped_entirely(self):
        sev, _ = fd.apply_severity_ladder("major", 0.75, 1, False)
        self.assertIsNone(sev)

    def test_a_critical_facet_at_weak_confidence_survives_as_minor(self):
        sev, _ = fd.apply_severity_ladder("critical", 0.71, 1, False)
        self.assertEqual(sev, "minor")

    def test_downgrades_stack(self):
        sev, downgrades = fd.apply_severity_ladder("critical", 0.75, 3, True)
        self.assertIsNone(sev)  # 2 (r<0.80) + 1 (k>=3) + 1 (inferred) = 4 steps from critical
        self.assertEqual(len(downgrades), 4)


class FacetNormalizeTest(unittest.TestCase):
    """One flag / no-flag pair per facet -- the roster this stream's SOP
    validator checks against, and the reason a slug exists here at all."""

    def hit(self, slug, base_cluster, outlier_overrides):
        facet = fd.FACETS_BY_SLUG[slug]
        baseline_token = facet.normalize(base_cluster)
        outlier = copy.deepcopy(base_cluster)
        for path, value in outlier_overrides.items():
            target = outlier
            keys = path.split(".")
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = value
        outlier_token = facet.normalize(outlier)
        return baseline_token, outlier_token, facet.should_flag(outlier_token, baseline_token)

    def test_release_channel(self):
        base, out, flagged = self.hit("release-channel", cluster("c"), {"releaseChannel.channel": "STABLE"})
        self.assertTrue(flagged)

    def test_release_channel_unenrolled_is_excluded_not_flagged(self):
        c = cluster("c", **{"releaseChannel.channel": ""})
        self.assertIsNone(fd.norm_release_channel(c))

    def test_shielded_nodes(self):
        base, out, flagged = self.hit("shielded-nodes", cluster("c"), {"shieldedNodes.enabled": False})
        self.assertTrue(flagged)

    def test_secure_boot_all_vs_none(self):
        c = cluster("c", node_pools=None)
        base, out, flagged = self.hit("secure-boot", cluster("c"), {"nodePools": [{"config": {"shieldedInstanceConfig": {"enableSecureBoot": False}}}]})
        self.assertTrue(flagged)

    def test_secure_boot_excludes_windows_pools(self):
        c = cluster(
            "c",
            nodePools=[
                {"name": "linux", "config": {"shieldedInstanceConfig": {"enableSecureBoot": True}, "imageType": "COS_CONTAINERD"}},
                {"name": "win", "config": {"shieldedInstanceConfig": {"enableSecureBoot": False}, "imageType": "WINDOWS_LTSC_CONTAINERD"}},
            ],
        )
        self.assertEqual(fd.norm_secure_boot(c), "ALL")

    def test_integrity_monitoring(self):
        base, out, flagged = self.hit("integrity-monitoring", cluster("c"), {"nodePools": [{"config": {"shieldedInstanceConfig": {"enableIntegrityMonitoring": False}}}]})
        self.assertTrue(flagged)

    # SOP 4.3 and 4.8 both state a one-directional impact ("nodes boot
    # unverified", "cannot absorb load the way its peers do") and a remediation
    # that turns the feature on. A cluster covering *more* pools than its cohort
    # is the reverse, so flagging it renders an inverted impact and a fix that
    # never converges: enabling the feature everywhere lands on ALL, which still
    # is not a NONE baseline, and the finding recurs on every subsequent run.

    def test_less_only_ranks_none_below_some_below_all(self):
        self.assertTrue(fd._flag_less_only("NONE", "SOME"))
        self.assertTrue(fd._flag_less_only("NONE", "ALL"))
        self.assertTrue(fd._flag_less_only("SOME", "ALL"))
        self.assertFalse(fd._flag_less_only("ALL", "ALL"))
        self.assertFalse(fd._flag_less_only("SOME", "SOME"))

    def test_pool_autoscaling_some_against_none_baseline_is_not_flagged(self):
        # The live case: nine peers autoscale no pool, spot-capacity-test
        # autoscales its spot pool. It absorbs load better, not worse.
        base, out, flagged = self.hit(
            "pool-autoscaling",
            cluster("c", nodePools=[{"name": "default-pool"}, {"name": "spot-pool"}]),
            {"nodePools": [{"name": "default-pool"}, {"name": "spot-pool", "autoscaling": {"enabled": True}}]},
        )
        self.assertEqual(base, "NONE")
        self.assertEqual(out, "SOME")
        self.assertFalse(flagged)

    def test_pool_autoscaling_none_against_all_baseline_is_flagged(self):
        base, out, flagged = self.hit(
            "pool-autoscaling",
            cluster("c", nodePools=[{"name": "default-pool", "autoscaling": {"enabled": True}}]),
            {"nodePools": [{"name": "default-pool"}]},
        )
        self.assertEqual((base, out), ("ALL", "NONE"))
        self.assertTrue(flagged)

    def test_secure_boot_all_against_none_baseline_is_not_flagged(self):
        base, out, flagged = self.hit(
            "secure-boot",
            cluster("c", nodePools=[{"name": "p", "config": {"imageType": "COS_CONTAINERD"}}]),
            {"nodePools": [{"name": "p", "config": {"imageType": "COS_CONTAINERD", "shieldedInstanceConfig": {"enableSecureBoot": True}}}]},
        )
        self.assertEqual((base, out), ("NONE", "ALL"))
        self.assertFalse(flagged)

    def test_node_autoprovisioning_on_against_off_baseline_is_not_flagged(self):
        base, out, flagged = self.hit(
            "node-autoprovisioning",
            cluster("c", autoscaling={"enableNodeAutoprovisioning": False}),
            {"autoscaling": {"enableNodeAutoprovisioning": True}},
        )
        self.assertEqual((base, out), ("OFF", "ON"))
        self.assertFalse(flagged)

    def test_node_autoprovisioning_off_against_on_baseline_is_flagged(self):
        base, out, flagged = self.hit(
            "node-autoprovisioning",
            cluster("c", autoscaling={"enableNodeAutoprovisioning": True}),
            {"autoscaling": {"enableNodeAutoprovisioning": False}},
        )
        self.assertEqual((base, out), ("ON", "OFF"))
        self.assertTrue(flagged)

    def test_shielded_nodes_on_against_off_baseline_is_not_flagged(self):
        base, out, flagged = self.hit("shielded-nodes", cluster("c", shieldedNodes={"enabled": False}), {"shieldedNodes.enabled": True})
        self.assertEqual((base, out), ("OFF", "ON"))
        self.assertFalse(flagged)

    # The same asymmetry on the facets whose SOP impact accuses the outlier of
    # exposure (4.5), missing telemetry (4.6), or unwrapped etcd (4.13). Each
    # offers only an enable-the-control remediation, so flagging the hardened
    # side would report the one cluster that got it right and leave "weaken it
    # to match your peers" as the only recommendation that closes the finding.

    def test_hardened_outlier_is_never_flagged_against_a_lax_majority(self):
        for slug, lax, hardened in (
            ("private-nodes", "OFF", "ON"),
            ("private-endpoint", "OFF", "ON"),
            ("authorized-networks", "OFF", "ON"),
            ("managed-prometheus", "OFF", "ON"),
            ("database-encryption", "DECRYPTED", "ENCRYPTED"),
        ):
            with self.subTest(slug=slug):
                should_flag = fd.FACETS_BY_SLUG[slug].should_flag
                self.assertFalse(should_flag(hardened, lax), "%s flagged the hardened side" % slug)
                self.assertTrue(should_flag(lax, hardened), "%s missed the degraded side" % slug)

    def test_database_encryption_decrypted_against_encrypted_majority_is_flagged(self):
        base, out, flagged = self.hit(
            "database-encryption",
            cluster("c", databaseEncryption={"state": "ENCRYPTED"}),
            {"databaseEncryption": {"state": "DECRYPTED"}},
        )
        self.assertEqual((base, out), ("ENCRYPTED", "DECRYPTED"))
        self.assertTrue(flagged)

    def test_only_neutral_facets_still_compare_both_directions(self):
        # release-channel, intra-node-visibility and datapath-provider describe
        # a difference rather than a loss, so _flag_ne is right for them and
        # wrong everywhere else. Pin the membership so a new facet has to choose.
        bidirectional = {f.slug for f in fd.FACETS if f.should_flag is fd._flag_ne}
        self.assertEqual(bidirectional, {"release-channel", "intra-node-visibility", "datapath-provider"})

    def test_network_policy_reads_both_engines_as_one_token(self):
        # Two tokens, not three. A Calico cluster and a Dataplane V2 cluster
        # agree here, so neither is ever the other's outlier and both count
        # toward the baseline an `OFF` cluster is measured against.
        dpv2 = cluster("c")
        calico = cluster("c", **{"networkConfig.datapathProvider": "LEGACY_DATAPATH", "networkPolicy.enabled": True})
        self.assertEqual(fd.norm_network_policy(dpv2), "ENFORCED")
        self.assertEqual(fd.norm_network_policy(calico), "ENFORCED")

    def test_network_policy_off_against_enforcing_majority_is_flagged(self):
        base, out, flagged = self.hit("network-policy", cluster("c"), {"networkConfig.datapathProvider": "LEGACY_DATAPATH", "networkPolicy.enabled": False})
        self.assertEqual(out, "OFF")
        self.assertTrue(flagged)

    def test_private_nodes(self):
        base, out, flagged = self.hit("private-nodes", cluster("c"), {"privateClusterConfig.enablePrivateNodes": False})
        self.assertTrue(flagged)

    def test_private_endpoint_legacy_field(self):
        base, out, flagged = self.hit("private-endpoint", cluster("c"), {"privateClusterConfig.enablePrivateEndpoint": False})
        self.assertTrue(flagged)

    def test_private_endpoint_falls_back_to_newer_field(self):
        c = cluster("c", privateClusterConfig={"enablePrivateNodes": True}, controlPlaneEndpointsConfig={"ipEndpointsConfig": {"enablePublicEndpoint": False}})
        self.assertEqual(fd.norm_private_endpoint(c), "ON")

    def test_authorized_networks_requires_nonempty_cidrs(self):
        c = cluster("c", masterAuthorizedNetworksConfig={"enabled": True, "cidrBlocks": []})
        self.assertEqual(fd.norm_authorized_networks(c), "OFF")

    def test_authorized_networks_falls_back_to_newer_field(self):
        """The two surfaces are mutually exclusive, so one read is not enough.

        `norm_private_endpoint` above already falls back this way. Authorized
        networks did not, so a cluster configured through
        `ipEndpointsConfig.authorizedNetworksConfig` normalised to OFF and drifted
        as a critical against peers holding the identical setting.
        """
        c = cluster(
            "c",
            masterAuthorizedNetworksConfig={},
            controlPlaneEndpointsConfig={
                "ipEndpointsConfig": {
                    "authorizedNetworksConfig": {
                        "enabled": True,
                        "cidrBlocks": [{"displayName": "corp", "cidrBlock": "10.0.0.0/8"}],
                    }
                }
            },
        )
        self.assertEqual(fd.norm_authorized_networks(c), "ON")

    def test_logging_components_superset_not_flagged(self):
        baseline = fd.norm_logging_components(cluster("c"))
        outlier = fd.norm_logging_components(cluster("c", loggingConfig={"componentConfig": {"enableComponents": ["SYSTEM_COMPONENTS", "WORKLOADS", "APISERVER"]}}))
        self.assertFalse(fd._flag_not_superset(outlier, baseline))

    def test_logging_components_subset_is_flagged(self):
        baseline = fd.norm_logging_components(cluster("c"))
        outlier = fd.norm_logging_components(cluster("c", loggingConfig={"componentConfig": {"enableComponents": ["WORKLOADS"]}}))
        self.assertTrue(fd._flag_not_superset(outlier, baseline))

    def test_logging_severity_major_when_missing_system_components(self):
        self.assertEqual(fd._logging_severity("WORKLOADS"), "major")

    def test_logging_severity_minor_when_system_components_present(self):
        self.assertEqual(fd._logging_severity("SYSTEM_COMPONENTS,WORKLOADS"), "minor")

    def test_monitoring_components_disjoint_is_flagged(self):
        baseline = "SYSTEM_COMPONENTS"
        outlier = "WORKLOADS"
        self.assertTrue(fd._flag_not_superset(outlier, baseline))

    def test_managed_prometheus(self):
        base, out, flagged = self.hit("managed-prometheus", cluster("c"), {"monitoringConfig.managedPrometheusConfig": {"enabled": False}})
        self.assertTrue(flagged)

    def test_binary_authorization_mode_difference_not_flagged(self):
        self.assertFalse(fd._flag_off_only("SOME_OTHER_ENABLED_MODE", "PROJECT_SINGLETON_POLICY_ENFORCE"))

    def test_binary_authorization_off_is_flagged(self):
        c = cluster("c", binaryAuthorization={"evaluationMode": "DISABLED"})
        self.assertEqual(fd.norm_binary_authorization(c), "OFF")
        self.assertTrue(fd._flag_off_only("OFF", "ON"))

    def test_binary_authorization_legacy_enabled_field(self):
        c = cluster("c", binaryAuthorization={"enabled": True})
        self.assertEqual(fd.norm_binary_authorization(c), "ON")

    def test_node_autoprovisioning(self):
        base, out, flagged = self.hit("node-autoprovisioning", cluster("c"), {"autoscaling.enableNodeAutoprovisioning": False})
        self.assertTrue(flagged)

    def test_pool_autoscaling_excludes_tainted_pools(self):
        c = cluster("c", nodePools=[{"name": "pinned", "autoscaling": {"enabled": False}, "config": {"taints": [{"key": "dedicated"}]}}])
        self.assertIsNone(fd.norm_pool_autoscaling(c))

    def test_a_gke_accelerator_taint_is_not_an_owner_pinning_a_pool(self):
        """GKE taints a GPU pool itself, so "any taint" dropped every pool of
        a GPU-only cluster from the vote. `_pool_fraction` then returned
        `None`, `unvoted_facets` wrote a `limitations` sentence,
        `coverage_gaps` read it as a gap on every run, and the stream stayed
        `partial` with `resolved` pinned at 0 -- over a taint §4.0's "its
        owner can revisit it" does not describe, because the owner did not
        apply it."""
        gpu = {"name": "gpu", "autoscaling": {"enabled": False},
               "config": {"taints": [{"key": "nvidia.com/gpu", "value": "present", "effect": "NO_SCHEDULE"}]}}
        self.assertEqual(fd.norm_pool_autoscaling(cluster("c", nodePools=[gpu])), "NONE")
        # An owner-applied taint beside it still pins the pool.
        gpu_and_dedicated = dict(gpu, config={"taints": [
            {"key": "nvidia.com/gpu"}, {"key": "dedicated", "value": "batch"}]})
        self.assertIsNone(fd.norm_pool_autoscaling(cluster("c", nodePools=[gpu_and_dedicated])))

    def test_arm_sandbox_and_windows_pools_are_not_owners_pinning_a_pool(self):
        """Accelerators were never the only case. GKE taints an Arm pool
        `kubernetes.io/arch=arm64`, a GKE Sandbox pool
        `sandbox.gke.io/runtime=gvisor` and a Windows pool
        `node.kubernetes.io/os=windows`, none of them a choice its owner made
        or can revisit -- and reading any of them as a pin drops every pool of
        a single-architecture cluster from the vote, which is the same
        permanent `partial` the GPU taint used to cause."""
        for key, value in (("kubernetes.io/arch", "arm64"),
                           ("sandbox.gke.io/runtime", "gvisor"),
                           ("node.kubernetes.io/os", "windows")):
            with self.subTest(taint=key):
                managed = {"name": "p", "autoscaling": {"enabled": False},
                           "config": {"taints": [{"key": key, "value": value, "effect": "NO_SCHEDULE"}]}}
                self.assertEqual(fd.norm_pool_autoscaling(cluster("c", nodePools=[managed])), "NONE")
                # An owner-applied taint beside it still pins the pool.
                both = dict(managed, config={"taints": [
                    {"key": key, "value": value}, {"key": "dedicated", "value": "batch"}]})
                self.assertIsNone(fd.norm_pool_autoscaling(cluster("c", nodePools=[both])))

    def test_intra_node_visibility(self):
        base, out, flagged = self.hit("intra-node-visibility", cluster("c"), {"networkConfig.enableIntraNodeVisibility": False})
        self.assertTrue(flagged)

    def test_datapath_provider(self):
        base, out, flagged = self.hit("datapath-provider", cluster("c"), {"networkConfig.datapathProvider": "LEGACY_DATAPATH"})
        self.assertTrue(flagged)

    def test_label_keys_extra_keys_not_flagged(self):
        baseline = fd.norm_label_keys(cluster("c", labels={"environment": "prod", "team": "x"}))
        outlier = fd.norm_label_keys(cluster("c", labels={"environment": "prod", "team": "x", "extra": "y"}))
        self.assertFalse(fd._flag_not_superset(outlier, baseline))

    def test_label_keys_drops_goog_prefixed(self):
        c = cluster("c", labels={"environment": "prod", "goog-gke-node-pool-provisioning-model": "x"})
        self.assertEqual(fd.norm_label_keys(c), "environment")

    def test_label_keys_missing_key_is_flagged(self):
        baseline = fd.norm_label_keys(cluster("c", labels={"environment": "prod", "team": "x"}))
        outlier = fd.norm_label_keys(cluster("c", labels={"environment": "prod"}))
        self.assertTrue(fd._flag_not_superset(outlier, baseline))

    def test_image_type_windows_pool_excluded(self):
        c = cluster("c", nodePools=[
            {"config": {"imageType": "COS_CONTAINERD"}},
            {"config": {"imageType": "WINDOWS_LTSC_CONTAINERD"}},
        ])
        self.assertEqual(fd.norm_image_type(c), "COS")

    def test_image_type_containerd_rename_is_not_a_divergence(self):
        cos = fd.norm_image_type(cluster("c", nodePools=[{"config": {"imageType": "COS"}}]))
        cos_containerd = fd.norm_image_type(cluster("c", nodePools=[{"config": {"imageType": "COS_CONTAINERD"}}]))
        self.assertEqual(cos, cos_containerd)

    def test_image_type_real_divergence_is_flagged(self):
        baseline = fd.norm_image_type(cluster("c", nodePools=[{"config": {"imageType": "COS_CONTAINERD"}}]))
        outlier = fd.norm_image_type(cluster("c", nodePools=[{"config": {"imageType": "UBUNTU_CONTAINERD"}}]))
        self.assertTrue(fd._flag_not_superset(outlier, baseline))

    def test_database_encryption(self):
        base, out, flagged = self.hit("database-encryption", cluster("c"), {"databaseEncryption.state": "DECRYPTED"})
        self.assertTrue(flagged)

    def test_database_encryption_absent_block_is_decrypted(self):
        c = cluster("c", databaseEncryption={})
        self.assertEqual(fd.norm_database_encryption(c), "DECRYPTED")


class ComputeDriftTest(unittest.TestCase):
    def cohort(self, n=4, outlier_overrides=None, **base_overrides):
        clusters = [cluster(f"c{i}", labels={"environment": "prod"}, **base_overrides) for i in range(n)]
        if outlier_overrides:
            for path, value in outlier_overrides.items():
                target = clusters[-1]
                keys = path.split(".")
                for key in keys[:-1]:
                    target = target.setdefault(key, {})
                target[keys[-1]] = value
        return clusters

    def test_a_clean_cohort_produces_no_findings(self):
        checks_run, candidates = fd.compute_drift(self.cohort(), now=NOW)
        self.assertTrue(all(v == [] for v in candidates.values()))
        self.assertIn("shielded-nodes", checks_run[K("c0")])

    def test_a_single_outlier_is_flagged(self):
        # n=20 keeps r=0.95, well clear of the confidence ladder's r<0.90
        # step, so this exercises the plain outlier path without also
        # exercising the downgrade -- that is SeverityLadderTest's job.
        clusters = self.cohort(n=20, outlier_overrides={"shieldedNodes.enabled": False})
        _, candidates = fd.compute_drift(clusters, now=NOW)
        outlier_name = clusters[-1]["name"]
        self.assertEqual(len(candidates[K(outlier_name)]), 1)
        self.assertEqual(candidates[K(outlier_name)][0]["check"], "shielded-nodes")
        self.assertEqual(candidates[K(outlier_name)][0]["severity"], "major")
        self.assertEqual(candidates[K("c0")], [])

    def test_cohort_under_the_floor_produces_nothing(self):
        clusters = [cluster("a"), cluster("b")]
        _, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(candidates[K("a")], [])
        self.assertEqual(fd.compute_drift(clusters, now=NOW)[0][K("a")], [])

    def test_autopilot_and_standard_are_never_compared_together(self):
        # 20 clean Standard clusters and one Autopilot cluster with shielded
        # nodes off. Merged into one cohort that is r=0.95 with k=1 -- no rung
        # of the severity ladder touches it -- so the Autopilot cluster would
        # be published as a `major`. What keeps it silent is the mode in the
        # cohort key: it cohorts alone, under the floor. At the four-cluster
        # size this test used to run, the merged cohort read r=0.50 and
        # reached no baseline at all, so it passed with the mode dropped.
        clusters = self.cohort(n=20)
        clusters.append(cluster("c-auto", autopilot=True, labels={"environment": "prod"}, **{"shieldedNodes.enabled": False}))
        _, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(candidates[K("c-auto")], [])
        self.assertEqual(candidates[K("c0")], [])

    def test_standard_only_facets_are_never_computed_for_autopilot(self):
        clusters = [cluster(f"a{i}", autopilot=True, labels={"environment": "prod"}) for i in range(4)]
        checks_run, _ = fd.compute_drift(clusters, now=NOW)
        self.assertNotIn("secure-boot", checks_run[K("a0")])
        self.assertNotIn("image-type", checks_run[K("a0")])

    def test_datapath_provider_is_not_compared_on_autopilot(self):
        # n=20, so r=0.95 and k=1: nothing on the severity ladder touches this
        # finding, and the `standard_only` flag is the only thing between the
        # outlier and a `major`. A four-cluster cohort read r=0.75 and lost the
        # finding to two ratio downgrades instead, which would pass whether the
        # facet was skipped or not. The standard cohort below is the control:
        # the same shape at the same size, published.
        def fleet(autopilot):
            members = [cluster(f"a{i}", autopilot=autopilot, labels={"environment": "prod"}) for i in range(19)]
            # `networkPolicy.enabled` comes with it: `network-policy` reads
            # the same `datapathProvider` key, and a bare switch to
            # `LEGACY_DATAPATH` drops enforcement too, so the outlier would
            # carry a second finding this test is not about.
            members.append(cluster("a-outlier", autopilot=autopilot, labels={"environment": "prod"},
                                   **{"networkConfig.datapathProvider": "LEGACY_DATAPATH",
                                      "networkPolicy.enabled": True}))
            return members

        checks_run, candidates = fd.compute_drift(fleet(True), now=NOW)
        self.assertNotIn("datapath-provider", checks_run[K("a-outlier")])
        self.assertEqual(candidates[K("a-outlier")], [])

        _, standard = fd.compute_drift(fleet(False), now=NOW)
        self.assertEqual([c["check"] for c in standard[K("a-outlier")]], ["datapath-provider"])
        self.assertEqual(standard[K("a-outlier")][0]["severity"], "major")

    def test_ineligible_cluster_gets_no_facets_compared(self):
        clusters = self.cohort(n=3)
        clusters.append(cluster("provisioning", status="PROVISIONING", labels={"environment": "prod"}))
        checks_run, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(checks_run[K("provisioning")], [])
        self.assertEqual(candidates[K("provisioning")], [])

    def test_a_reconciling_cluster_is_still_compared(self):
        clusters = self.cohort(n=3)
        clusters.append(cluster("reconciling", status="RECONCILING", labels={"environment": "prod"}))
        checks_run, _ = fd.compute_drift(clusters, now=NOW)
        self.assertIn("release-channel", checks_run[K("reconciling")])

    def test_split_cluster_guard_replaces_many_findings_with_one(self):
        # n=20 keeps r=0.95 for every facet -- comfortably clear of the
        # confidence ladder, so all six overrides below survive as
        # findings and the split-cluster guard is what collapses them,
        # not a severity downgrade dropping two of the six first.
        clusters = self.cohort(n=20)
        outlier = clusters[-1]
        for facet_slug, path, value in [
            ("shielded-nodes", "shieldedNodes.enabled", False),
            ("private-nodes", "privateClusterConfig.enablePrivateNodes", False),
            ("private-endpoint", "privateClusterConfig.enablePrivateEndpoint", False),
            ("intra-node-visibility", "networkConfig.enableIntraNodeVisibility", False),
            ("managed-prometheus", "monitoringConfig.managedPrometheusConfig", {"enabled": False}),
            ("database-encryption", "databaseEncryption.state", "DECRYPTED"),
        ]:
            target = outlier
            keys = path.split(".")
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = value
        _, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(len(candidates[K(outlier["name"])]), 1)
        self.assertEqual(candidates[K(outlier["name"])][0]["check"], "uncohorted")

    def test_environment_strategy_separates_cohorts(self):
        # staging's own majority is DECRYPTED, so none of its three members is
        # an outlier there. Merged into prod's cohort they are: r=20/23=0.87
        # and k=3 cost two rungs, and `database-encryption` is base-critical,
        # so the finding survives as a `minor` rather than falling off the
        # ladder. A four-and-four split, which is what this test used to
        # build, merged to r=0.50 and reached no baseline either way.
        prod = self.cohort(n=20)
        staging = [cluster(f"s{i}", labels={"environment": "staging"}, **{"databaseEncryption.state": "DECRYPTED"}) for i in range(3)]
        _, candidates = fd.compute_drift(prod + staging, now=NOW)
        self.assertEqual(candidates[K("s0")], [])

    def test_baseline_at_exactly_two_thirds_still_fires_for_a_critical_facet(self):
        # r = 2/3 = 0.667 triggers both the r<0.90 and r<0.80 downgrade
        # steps -- two steps from critical (index 0) lands on minor (index
        # 2), which is exactly the SOP's own worked example: "a base-
        # critical facet at r=0.71 survives as minor."
        clusters = [
            cluster("c0", labels={"environment": "prod"}),
            cluster("c1", labels={"environment": "prod"}),
            cluster("c2", labels={"environment": "prod"}, **{"privateClusterConfig.enablePrivateNodes": False}),
        ]
        _, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(len(candidates[K("c2")]), 1)
        self.assertEqual(candidates[K("c2")][0]["severity"], "minor")

    def test_baseline_at_exactly_two_thirds_drops_a_major_facet_entirely(self):
        clusters = [
            cluster("c0", labels={"environment": "prod"}),
            cluster("c1", labels={"environment": "prod"}),
            cluster("c2", labels={"environment": "prod"}, **{"shieldedNodes.enabled": False}),
        ]
        _, candidates = fd.compute_drift(clusters, now=NOW)
        self.assertEqual(candidates[K("c2")], [])


class ClusterIdentityTest(unittest.TestCase):
    """A GKE cluster name is unique inside its project, not across the fleet,
    and this is the collector that sweeps every project."""

    @staticmethod
    def _two_projects():
        fleet = []
        for proj in ("p1", "p2"):
            fleet.append(cluster("web", project=proj, labels={"team": "x"}))
            fleet += [cluster(f"{proj}-{i}", project=proj, labels={"team": "x"}) for i in range(9)]
        fleet[0]["shieldedNodes"] = {"enabled": False}  # p1/web only
        return fleet

    def test_the_same_name_in_two_projects_stays_two_clusters(self):
        fleet = self._two_projects()
        self.assertEqual(fd.decide_cohort_strategy(fleet), "project")
        checks_run, candidates = fd.compute_drift(fleet, now=NOW)
        p1, p2 = K("web", project="p1"), K("web", project="p2")
        self.assertEqual([c["check"] for c in candidates[p1]], ["shielded-nodes"])
        # Keyed by name, p2/web was handed p1/web's finding as well as its own
        # empty list, and published it under an indistinguishable Cluster/web.
        self.assertEqual(candidates[p2], [])
        # §6 rejects a duplicated `checks_run` entry, and the merge produced one
        # by concatenating both clusters' facet lists into a single value.
        self.assertEqual(len(checks_run[p1]), len(set(checks_run[p1])))
        self.assertEqual(sorted(checks_run[p1]), sorted(checks_run[p2]))


class SplitCountTest(unittest.TestCase):
    """§3.2 defines `k` as `n - m` -- how split the cohort is -- not the number
    of clusters that ended up flagged."""

    @staticmethod
    def _fleet():
        # 27 SOME, 2 ALL, 1 NONE on secure-boot. r = 27/30 = 0.90 clears both
        # consensus steps, and k = 30 - 27 = 3 lands exactly on §3.5's `k >= 3`
        # step. Only `none0` is flagged -- `_flag_less_only` stays quiet for the
        # two clusters covering more pools than the cohort does -- so a `k` read
        # off the flagged count is 1 and skips the step.
        fleet = [cluster(f"s{i}", labels={"team": "x"}, nodePools=[pool("a"), pool("b", secure_boot=False)]) for i in range(27)]
        fleet += [cluster(f"all{i}", labels={"team": "x"}, nodePools=[pool("a"), pool("b")]) for i in range(2)]
        fleet.append(cluster("none0", labels={"team": "x"}, nodePools=[pool("a", secure_boot=False), pool("b", secure_boot=False)]))
        return fleet

    def test_off_baseline_clusters_count_toward_k_even_when_unflagged(self):
        _, candidates = fd.compute_drift(self._fleet(), now=NOW)
        found = [c for c in candidates[K("none0")] if c["check"] == "secure-boot"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "minor")  # major, one step for k
        self.assertIn("k=3>=3", found[0]["excerpt"])

    def test_a_cluster_that_diverges_upward_is_still_not_flagged(self):
        _, candidates = fd.compute_drift(self._fleet(), now=NOW)
        self.assertEqual(candidates[K("all0")], [])


class InferredEnvironmentTest(unittest.TestCase):
    """§3.5 downgrades a finding whose cohort membership rests on an inferred
    environment -- which is only ever true under the `environment` strategy."""

    def test_a_name_token_does_not_downgrade_when_cohorts_ignore_environment(self):
        fleet = [cluster(n, labels={"team": "x"}) for n in ("alpha", "beta", "gamma", "delta", "prod-eps")]
        fleet[0]["shieldedNodes"] = {"enabled": False}
        # One name token out of five does not earn the environment strategy, so
        # no cohort key holds an environment and no membership rests on one.
        self.assertEqual(fd.decide_cohort_strategy(fleet), "mode-only")
        _, candidates = fd.compute_drift(fleet, now=NOW)
        found = candidates[K("alpha")]
        self.assertEqual([f["check"] for f in found], ["shielded-nodes"])
        self.assertEqual(found[0]["severity"], "minor")  # r=0.80<0.90 only
        self.assertNotIn("inferred environment", found[0]["excerpt"])

    def test_an_inferred_environment_still_downgrades_when_it_drew_the_cohort(self):
        fleet = [cluster(f"prod-{i}") for i in range(10)]
        for c in fleet:
            c["resourceLabels"] = {"team": "x"}
        fleet[0]["shieldedNodes"] = {"enabled": False}
        self.assertEqual(fd.decide_cohort_strategy(fleet), "environment")
        _, candidates = fd.compute_drift(fleet, now=NOW)
        found = candidates[K("prod-0")]
        self.assertEqual(found[0]["severity"], "minor")  # major, one step
        self.assertIn("inferred environment", found[0]["excerpt"])


class MissingTokensTest(unittest.TestCase):
    """§3.8's `missing:` line, so a title does not have to re-derive the set
    difference the gate already took.

    Live case, `drift-peer-std-4` on 2026-09-01: logging off entirely against a
    `SYSTEM_COMPONENTS,WORKLOADS` cohort, published as "logging component set
    missing WORKLOADS relative to its cohort" -- one of the two, reading as
    though system logging still worked.
    """

    @staticmethod
    def _cohort(outlier_logging):
        fleet = [cluster(f"peer{i}", labels={"team": "x"}) for i in range(9)]
        fleet.append(cluster("odd", labels={"team": "x"}, **{"loggingConfig.componentConfig": outlier_logging}))
        return fleet

    def _logging_finding(self, outlier_logging):
        _, candidates = fd.compute_drift(self._cohort(outlier_logging), now=NOW)
        found = [c for c in candidates[K("odd")] if c["check"] == "logging-components"]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_a_cluster_with_no_logging_at_all_is_missing_the_whole_baseline(self):
        found = self._logging_finding({})
        self.assertIn("observed: NONE", found["excerpt"])
        self.assertIn("missing: SYSTEM_COMPONENTS, WORKLOADS", found["excerpt"])
        # Missing SYSTEM_COMPONENTS is the `major` leg of `_logging_severity`,
        # and r = 9/10 = 0.90 clears the `r < 0.90` step, so nothing downgrades.
        self.assertEqual(found["severity"], "major")

    def test_a_partial_set_names_only_what_it_actually_lacks(self):
        found = self._logging_finding({"enableComponents": ["SYSTEM_COMPONENTS"]})
        self.assertIn("missing: WORKLOADS", found["excerpt"])
        self.assertNotIn("SYSTEM_COMPONENTS,", found["excerpt"].split("missing: ")[1])
        self.assertEqual(found["severity"], "minor")

    def test_a_facet_that_is_not_set_valued_gets_no_missing_line(self):
        fleet = [cluster(f"peer{i}", labels={"team": "x"}) for i in range(9)]
        fleet.append(cluster("odd", labels={"team": "x"}, **{"shieldedNodes.enabled": False}))
        _, candidates = fd.compute_drift(fleet, now=NOW)
        found = [c for c in candidates[K("odd")] if c["check"] == "shielded-nodes"]
        self.assertEqual(len(found), 1)
        # `ON`/`OFF` is not a set, so "missing: ON" would assert a shape the
        # facet does not have.
        self.assertNotIn("missing:", found[0]["excerpt"])


class PeerListTest(unittest.TestCase):
    """`peers:` names the clusters holding the baseline, not every cluster that
    voted.

    Live case, `drift-peer-std-4` on 2026-09-01: the excerpt said the baseline
    held "in 9/10 clusters", then listed 10 names -- one of them
    `drift-peer-std-4` itself, whose `observed:` line directly underneath said
    it did not hold the baseline. Sorting put the outlier first as often as not,
    so the first name a reader saw in the comparison set was the cluster being
    compared.
    """

    @staticmethod
    def _peers_line(excerpt):
        return next(line for line in excerpt.splitlines() if line.startswith("peers:"))

    def _logging_excerpt(self):
        fleet = [cluster(f"peer{i}", labels={"team": "x"}) for i in range(9)]
        fleet.append(cluster("odd", labels={"team": "x"}, **{"loggingConfig.componentConfig": {}}))
        _, candidates = fd.compute_drift(fleet, now=NOW)
        found = [c for c in candidates[K("odd")] if c["check"] == "logging-components"]
        self.assertEqual(len(found), 1)
        return found[0]["excerpt"]

    def test_the_outlier_is_not_listed_among_the_peers_it_differs_from(self):
        excerpt = self._logging_excerpt()
        self.assertIn("in 9/10 clusters", excerpt)
        self.assertNotIn("odd", self._peers_line(excerpt))

    def test_the_peer_count_agrees_with_the_baseline_count_above_it(self):
        # 9 hold the baseline, 6 are printed, so the overflow is 3. Counting all
        # 10 voters printed "+4 more" beside a line claiming 9.
        self.assertIn("+3 more", self._peers_line(self._logging_excerpt()))


class PoolShapeTest(unittest.TestCase):
    """§4.8: do not flag single-pool clusters against multi-pool peers."""

    @staticmethod
    def _fleet(solo_pools):
        fleet = [cluster(f"m{i}", labels={"team": "x"}, nodePools=[pool("a"), pool("b", autoscaling=False)]) for i in range(9)]
        fleet.append(cluster("solo", labels={"team": "x"}, nodePools=solo_pools))
        return fleet

    def test_a_single_pool_cluster_is_not_flagged_against_a_some_baseline(self):
        # A one-pool cluster can only normalize to ALL or NONE, so against a
        # SOME baseline it is an outlier no change can close: turning
        # autoscaling on moves it to ALL, still not SOME.
        _, candidates = fd.compute_drift(self._fleet([pool("a", autoscaling=False)]), now=NOW)
        self.assertEqual([c for c in candidates[K("solo")] if c["check"] == "pool-autoscaling"], [])

    def test_a_multi_pool_cluster_is_still_flagged_against_the_same_baseline(self):
        fleet = self._fleet([pool("a", autoscaling=False), pool("b", autoscaling=False)])
        _, candidates = fd.compute_drift(fleet, now=NOW)
        found = [c for c in candidates[K("solo")] if c["check"] == "pool-autoscaling"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "minor")

    def test_the_suppression_does_not_reach_the_other_pool_fraction_facets(self):
        # §4.3's secure-boot shares the ALL/SOME/NONE scale but lists a
        # different set of suppressions, and not this one.
        fleet = [cluster(f"m{i}", labels={"team": "x"}, nodePools=[pool("a"), pool("b", secure_boot=False)]) for i in range(9)]
        fleet.append(cluster("solo", labels={"team": "x"}, nodePools=[pool("a", secure_boot=False)]))
        _, candidates = fd.compute_drift(fleet, now=NOW)
        self.assertEqual([c["check"] for c in candidates[K("solo")]], ["secure-boot"])


class EligibilityCreateTimeTest(unittest.TestCase):
    def test_a_null_create_time_is_treated_as_settled(self):
        self.assertIsNone(fd.cluster_eligibility(cluster("c", created=None), now=NOW))

    def test_an_unparseable_create_time_is_treated_as_settled(self):
        self.assertIsNone(fd.cluster_eligibility(cluster("c", created="not-a-date"), now=NOW))

    def test_a_create_time_with_no_timezone_is_treated_as_settled(self):
        """The one unreadable `createTime` that gets past `fromisoformat`.
        `2020-01-01` and `2020-01-01T00:00:00` both parse, and both return a
        *naive* datetime, so it is the subtraction against an aware `now` a
        line later that raises -- a `TypeError`, which an `except ValueError`
        does not catch. Nothing between here and `main` catches it either, and
        the SOP runs this module as `fleet_drift.py > manifest.json`, so the
        shell has already truncated the manifest by the time the traceback
        prints."""
        for created in ("2020-01-01T00:00:00", "2020-01-01"):
            with self.subTest(created=created):
                self.assertIsNone(fd.cluster_eligibility(cluster("c", created=created), now=NOW))

    def test_a_naive_create_time_does_not_truncate_the_manifest(self):
        docs = [cluster(f"c{i}", labels={"team": "x"}) for i in range(3)]
        docs[1]["createTime"] = "2020-01-01T00:00:00"

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, json.dumps(docs))
            return run_of(0)

        manifest = collected(run, now=NOW)
        self.assertEqual(len(manifest["clusters"]), 3)
        self.assertTrue(all(c["outcome"] == "collected" for c in manifest["clusters"]))

    def test_a_genuinely_fresh_cluster_is_still_excluded(self):
        why = fd.cluster_eligibility(cluster("c", created="2026-07-31T18:00:00Z"), now=NOW)
        self.assertIn("under 24h", why or "")

    def test_a_null_create_time_does_not_truncate_the_manifest(self):
        """`None.replace` is an AttributeError no caller catches, and the SOP
        runs this module as `fleet_drift.py > manifest.json` -- so the shell had
        already truncated the manifest by the time the traceback printed, and
        one cluster with an odd createTime lost the whole fleet."""
        docs = [cluster(f"c{i}", labels={"team": "x"}) for i in range(3)]
        docs[1]["createTime"] = None

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, json.dumps(docs))
            return run_of(0)

        manifest = collected(run, now=NOW)
        self.assertEqual(len(manifest["clusters"]), 3)
        self.assertTrue(all(c["outcome"] == "collected" for c in manifest["clusters"]))


class CollectFleetTest(unittest.TestCase):
    def test_manifest_shape(self):
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)])

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = collected(run, now=NOW)
        self.assertEqual(manifest["audit"], "fleet-consistency-drift")
        self.assertEqual(len(manifest["clusters"]), 4)
        self.assertTrue(all(c["outcome"] == "collected" for c in manifest["clusters"]))

    def test_a_project_that_fails_to_list_is_recorded_not_dropped(self):
        """Returning nothing made a project whose `clusters list` failed
        indistinguishable from one holding no clusters, so the manifest read
        complete and the document was held to nothing. It matters more here
        than in a per-cluster stream: drift ranks each cluster against its
        cohort, and clusters missing from the comparison silently change what
        counts as an outlier."""

        def run(argv, **kwargs):
            return run_of(1, "", "denied")

        manifest = collected(run, now=NOW)
        self.assertEqual([c["name"] for c in manifest["clusters"]], ["project/acme"])
        entry = manifest["clusters"][0]
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("denied", entry["error"])
        self.assertIn("1 of 1 project(s) in scope failed", manifest["error"])

    def test_the_uncohorted_candidate_carries_the_listing_command(self):
        """§3.6's `uncohorted` is derived from other facets' verdicts rather
        than voted on, so its slug is in no cohort's `checks_run` and the
        per-slug `commands` hold no record for it. `adopt_collector_evidence`
        moves excerpt and command together and adopts neither without one, so
        without the candidate's own `command` the one finding that replaces six
        is published on the model's paraphrase of the collector's words --
        silently, run after run. Deleting the two lines that set it left the
        whole suite green before this test existed."""
        clusters = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(20)]
        for path, value in [
            ("shieldedNodes.enabled", False),
            ("privateClusterConfig.enablePrivateNodes", False),
            ("privateClusterConfig.enablePrivateEndpoint", False),
            ("networkConfig.enableIntraNodeVisibility", False),
            ("monitoringConfig.managedPrometheusConfig", {"enabled": False}),
            ("databaseEncryption.state", "DECRYPTED"),
        ]:
            target = clusters[-1]
            keys = path.split(".")
            for key in keys[:-1]:
                target = target.setdefault(key, {})
            target[keys[-1]] = value
        clusters_json = json.dumps(clusters)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = collected(run, now=NOW)
        entry = next(c for c in manifest["clusters"] if c["name"].endswith("/c19"))
        candidate = next(c for c in entry["candidates"] if c["check"] == "uncohorted")
        listing = next(
            c["command"] for c in manifest["clusters"][0]["commands"] if c["check"] == "release-channel"
        )
        self.assertEqual(candidate["command"], listing)
        self.assertIn("gcloud container clusters list", candidate["command"])
        # The premise the command is needed for: no per-slug record backs it.
        self.assertNotIn("uncohorted", [c["check"] for c in entry["commands"]])

    def test_a_scoped_run_says_it_never_looked_at_the_rest_of_the_fleet(self):
        """`--project` reads one project and names no other, which is the loss
        a failed `projects list` produces minus the failure. Without a row
        saying so the manifest is a fleet of one project, `finish` finds no
        coverage gap, and every ledger finding on a cluster in another project
        is announced resolved and its remediation pull request closed -- the
        same outcome `UNENUMERATED_PROJECTS` was added to stop one rung up."""
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)])

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet("acme", run=run, now=NOW)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertIn("project/UNENUMERATED_PROJECTS", by_name)
        entry = by_name["project/UNENUMERATED_PROJECTS"]
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("--project", entry["error"])
        self.assertIn("acme", entry["error"])
        # The fleet itself is still collected; the row is about the projects
        # nobody named, not about this one.
        self.assertEqual(len([c for c in manifest["clusters"] if c["outcome"] == "collected"]), 3)
        self.assertNotIn("error", manifest)

    def test_a_fleet_discovery_found_leaves_no_unenumerated_row(self):
        """The counterpart: one project reached by discovery rather than by
        `--project` is the whole fleet, so nothing is unread and the row would
        be a coverage gap on a run that has none."""
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)])

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = collected(run, now=NOW)
        self.assertNotIn("project/UNENUMERATED_PROJECTS", [c["name"] for c in manifest["clusters"]])

    def test_a_project_with_the_gke_api_off_reads_as_empty_not_as_lost(self):
        """A project whose Kubernetes Engine API is disabled cannot hold a GKE
        cluster, and the condition does not change between runs. Reading its
        403 as a lost project put a `gate-failed` row in every manifest for as
        long as the project existed: a coverage gap on every run, `resolved`
        pinned at 0, and no stale remediation pull request ever closed. A
        credential that can see an organisation's projects sees mostly
        projects without GKE, so that is the ordinary case."""
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)])
        disabled = (
            "ERROR: (gcloud.container.clusters.list) ResponseError: code=403, "
            "message=Kubernetes Engine API has not been used in project logs-only before "
            "or it is disabled."
        )

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nlogs-only\n")
            if "list" in argv and "clusters" in argv:
                if "logs-only" in argv:
                    return run_of(1, "", disabled)
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet(run=run, now=NOW)
        self.assertEqual(
            [c["name"] for c in manifest["clusters"] if c["outcome"] != "collected"], []
        )
        self.assertNotIn("error", manifest)
        self.assertEqual(len(manifest["clusters"]), 3)

    def test_a_project_the_credential_may_not_read_is_still_a_loss(self):
        """The other 403. A project this credential cannot list may well hold
        clusters, so it stays a `gate-failed` row -- the disabled-API test
        above must not have widened into "any 403 is an empty project"."""
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)])
        denied = (
            "ERROR: (gcloud.container.clusters.list) ResponseError: code=403, "
            "message=Required \"container.clusters.list\" permission(s) for \"projects/locked\"."
        )

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nlocked\n")
            if "list" in argv and "clusters" in argv:
                if "locked" in argv:
                    return run_of(1, "", denied)
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet(run=run, now=NOW)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertIn("project/locked", by_name)
        self.assertEqual(by_name["project/locked"]["outcome"], "gate-failed")
        self.assertIn("permission", by_name["project/locked"]["error"])

    def test_one_project_crashing_costs_that_project_and_no_other(self):
        """`future.result()` re-raises, and the SOP redirects this collector's
        stdout into the manifest — so an unmodelled exception on one project
        used to leave a zero-byte file and lose the whole fleet. Only a failed
        `clusters list` was modelled; a `TypeError` off an unexpected API shape
        was not."""
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)])

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nboom\n")
            if "list" in argv and "clusters" in argv:
                if "boom" in argv:
                    raise TypeError("unsupported operand type(s) for /: 'str' and 'str'")
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet(run=run, now=NOW)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual({T(f"c{i}") for i in range(4)} - set(by_name), set())
        self.assertEqual(by_name["project/boom"]["outcome"], "gate-failed")
        self.assertIn("TypeError", by_name["project/boom"]["error"])

    def test_a_fleet_narrowed_to_one_project_says_so_in_the_manifest(self):
        """`projects list` failing is a coverage loss one rung above a project
        whose own `clusters list` failed, and it used to be a stderr warning
        and nothing else: exit 0, every entry `collected`, no gap for the
        document to carry, and `finish` free to resolve every previous
        finding in the projects nobody read."""
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)])

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(1, "", "PERMISSION_DENIED")
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet(run=run, now=NOW)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        entry = by_name[fd.UNENUMERATED_PROJECTS_TARGET]
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("PERMISSION_DENIED", entry["error"])
        # A coverage loss, not a failed run: the project that did answer is
        # still collected and still reported on.
        self.assertNotIn("error", manifest)
        self.assertEqual(by_name[T("c0")]["outcome"], "collected")
        self.assertIn("1 project(s) unread", fd.candidate_summary(manifest)[0])

    def test_a_scope_of_no_projects_is_an_error_not_an_empty_fleet(self):
        def run(argv, **kwargs):
            return run_of(0, "")

        manifest = fd.collect_fleet(run=run, now=NOW)
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("named no project", manifest["error"])

    def test_one_name_in_two_locations_of_one_project_stays_two_targets(self):
        """A GKE name is unique per project *and* location, so the
        multi-region `prod` pair comes back from one `clusters list` and
        qualifying by project alone leaves both called `acme/prod`."""
        fleet = [cluster("prod", labels={"environment": "prod"}),
                 cluster("prod", location="europe-west1", labels={"environment": "prod"}),
                 cluster("other", labels={"environment": "prod"})]
        clusters_json = json.dumps(fleet)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        names = [c["name"] for c in collected(run, now=NOW)["clusters"]]
        self.assertEqual(sorted(names), sorted([T("prod"), T("prod", location="europe-west1"), T("other")]))

    def test_a_uncolliding_name_is_qualified_anyway(self):
        """Qualifying only a colliding name makes a cluster's identity depend
        on the rest of the fleet: `Cluster/only-one` becomes
        `Cluster/acme/only-one` the week a second project gains the name, and
        `finish` reads the old id as resolved and refiles the same drift as
        new. §3.7's red line, broken by the collector."""
        clusters_json = json.dumps([cluster("only-one", labels={"environment": "prod"})])

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        names = [c["name"] for c in collected(run, now=NOW)["clusters"]]
        self.assertEqual(names, [T("only-one")])

    def test_every_target_is_qualified_by_project_and_location(self):
        """A GKE cluster name is unique inside one project and location, and
        the manifest is fleet-wide: `audit_report._vouching_clusters` keys a
        dict by it, so two `seeded-a`s used to leave one entry and one cluster
        nobody reported on. Every project in the evaluation pool carries the
        same three names. A name unique today is qualified too -- see
        `test_a_uncolliding_name_is_qualified_anyway`."""
        fleets = {
            "acme": [cluster(f"seeded-{x}") for x in "abc"],
            "other": [cluster(f"seeded-{x}", project="other") for x in "abc"]
            + [cluster("only-here", project="other")],
        }
        fleets["other"][2]["shieldedNodes"] = {"enabled": False}

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nother\n")
            if "list" in argv and "clusters" in argv:
                return run_of(0, json.dumps(fleets[argv[argv.index("--project") + 1]]))
            return run_of(0)

        manifest = fd.collect_fleet(run=run, now=NOW)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(
            sorted(by_name),
            sorted(
                [T(f"seeded-{x}") for x in "abc"]
                + [T(f"seeded-{x}", project="other") for x in "abc"]
                + [T("only-here", project="other")]
            ),
        )
        self.assertEqual(by_name[T("only-here", project="other")]["project"], "other")
        found = by_name[T("seeded-c", project="other")]["candidates"]
        # The entry name carries the qualification; the candidate's `object`
        # carries the leaf, because `derive_finding_id` already keys on the
        # entry name and a second copy costs the id its readable tail.
        self.assertEqual([c["object"] for c in found], ["Cluster/seeded-c"])
        # The `peers:` line names the clusters holding the baseline, and an
        # unqualified `seeded-a` there points at two different clusters.
        self.assertIn(T("seeded-a"), found[0]["excerpt"])
        self.assertNotIn("peers: seeded-a", found[0]["excerpt"])

    def test_qualifying_the_cluster_name_moved_every_id_and_needed_a_scheme_bump(self):
        """`derive_finding_id` joins `cluster` as its second segment, so
        qualifying the name re-spells every id this stream already carries.
        The delta is a set difference over strings and cannot tell a rename
        from a fix: without a new `ID_SCHEME` stamp the first run after this
        merge announces every carried finding resolved, re-files it as new,
        and closes its remediation pull request -- the identity rule §3.7
        calls the Red Line. The function did not change; its input did, which
        is exactly what the stamp rather than the id's shape is there for."""
        import audit_report

        def fid(cluster_name):
            return audit_report.derive_finding_id(
                {
                    "check": "authorized-networks",
                    "cluster": cluster_name,
                    "namespace": "",
                    "object": "Cluster/seeded-c",
                }
            )

        self.assertNotEqual(fid("seeded-c"), fid(T("seeded-c")))
        self.assertGreaterEqual(
            audit_report.ID_SCHEME,
            3,
            "the collector qualifies cluster names, so ledgers written under scheme 2 "
            "hold ids this stream can no longer mint; the stamp has to say so",
        )

    def test_a_candidate_derives_a_finding_id_that_still_names_its_cluster(self):
        """The id a candidate becomes is what an operator types into
        `/remediate` and what `compute_delta` joins on next week. It has a
        100-character ceiling, and `_shorten_id` spends the overflow on the
        longest segment -- so spelling the project and location in the
        candidate's `object` as well as in the entry name took the cluster's own
        name off the end and left
        `authorized-networks.agentic-harness-demo-us-central1-a._.cluster-agentic-harness-demo-us-cen-3284a2`,
        which names no cluster at all. Three live runs filed that before this
        test existed.
        """
        import audit_report

        project = "agentic-harness-demo"
        fleet = [cluster(f"fa2-seeded-{x}", project=project, location="us-central1-a") for x in "abc"]
        fleet[2]["masterAuthorizedNetworksConfig"] = {"enabled": False}

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, project + "\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, project + "\n")
            if "list" in argv and "clusters" in argv:
                return run_of(0, json.dumps(fleet))
            return run_of(0)

        manifest = fd.collect_fleet(run=run, now=NOW)
        entry = {c["name"]: c for c in manifest["clusters"]}[
            T("fa2-seeded-c", project=project, location="us-central1-a")
        ]
        self.assertTrue(entry["candidates"], "no candidate to derive an id from")
        for candidate in entry["candidates"]:
            with self.subTest(check=candidate["check"]):
                fid = audit_report.derive_finding_id(
                    {
                        "check": candidate["check"],
                        "cluster": entry["name"],
                        "namespace": candidate["namespace"],
                        "object": candidate["object"],
                    }
                )
                short = audit_report._shorten_id(fid)
                self.assertLessEqual(len(short), audit_report.MAX_FINDING_ID)
                self.assertEqual(short, fid, "the id was truncated, so it lost its tail")
                self.assertIn("fa2-seeded-c", short)

    def test_every_project_failing_to_list_is_an_error_on_the_manifest(self):
        """Each loss is already a `gate-failed` row, but a run that read no
        cluster at all is a failed run rather than a clean small fleet, and §4's
        stop rule reads the top-level key. Discovery itself succeeded here, so
        `discovery.error` is not the one that fires."""

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nother\n")
            return run_of(1, "", "PERMISSION_DENIED")

        manifest = fd.collect_fleet(run=run, now=NOW)
        self.assertEqual(
            [c["outcome"] for c in manifest["clusters"]], ["gate-failed", "gate-failed"]
        )
        self.assertIn("2 of 2 project(s) in scope failed", manifest["error"])
        self.assertIn("PERMISSION_DENIED", manifest["error"])

    def test_a_project_holding_nothing_is_not_an_error(self):
        """An empty project is an answer, not a loss: discovery no longer
        filters those out, and reading two of them is a healthy run."""

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nother\n")
            if "list" in argv and "clusters" in argv:
                return run_of(0, "[]")
            return run_of(0)

        manifest = fd.collect_fleet(run=run, now=NOW)
        self.assertEqual(manifest["clusters"], [])
        self.assertNotIn("error", manifest)

    def test_each_project_is_listed_exactly_once_by_the_sweep(self):
        """Discovery names projects and the sweep lists them, so every project
        costs one `clusters list`. Discovery used to list each candidate too,
        which billed a project whose list had failed for two."""
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)])
        lists = []

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "list"]:
                return run_of(0, "acme\nother\n")
            if "list" in argv and "clusters" in argv:
                lists.append(argv[argv.index("--project") + 1])
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = fd.collect_fleet(run=run, now=NOW)
        self.assertEqual(sorted(lists), ["acme", "other"])
        self.assertEqual(len(manifest["clusters"]), 8)
        self.assertNotIn("error", manifest)

    def test_a_failed_discovery_is_an_error_on_the_manifest_not_an_empty_fleet(self):
        """Neither the active project nor `projects list` answered: nothing was
        looked at, which is not the same manifest as a fleet with no clusters."""

        def run(argv, **kwargs):
            return run_of(1, "", "no credentials")

        manifest = fd.collect_fleet(run=run, now=NOW)
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("project discovery failed", manifest["error"])
        self.assertIn("no credentials", manifest["error"])

    def test_a_stub_gcloud_that_answers_nothing_exits_nonzero_with_a_warning(self):
        """End to end through `main`, with a `gcloud` on PATH that fails every
        call: the manifest is still printed (the SOP redirects stdout into the
        file), the discovery failure is on it, the exit code is non-zero and
        stderr carries the WARNING."""
        stub = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, stub, True)
        gcloud = os.path.join(stub, "gcloud")
        with open(gcloud, "w") as fh:
            fh.write("#!/bin/sh\necho 'ERROR: (gcloud) no credentials' >&2\nexit 1\n")
        os.chmod(gcloud, 0o755)
        env = dict(os.environ, PATH=f"{stub}{os.pathsep}{os.environ.get('PATH', '')}")
        done = subprocess.run(
            [sys.executable, fd.__file__], capture_output=True, text=True, env=env, timeout=120
        )
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("WARNING", done.stderr)
        manifest = json.loads(done.stdout)
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("no credentials", manifest["error"])

    def test_a_project_that_lists_cleanly_adds_no_project_entry(self):
        clusters_json = json.dumps([cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)])

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = collected(run, now=NOW)
        self.assertEqual([c for c in manifest["clusters"] if c["name"].startswith("project/")], [])

    def test_every_cluster_publishes_the_mode(self):
        """`cluster_mode` is part of the cohort key here and it silences five
        facets, so this collector knows the mode before it writes a line — and
        withheld it, leaving each stream to re-derive a fact already resolved."""
        clusters = [cluster(f"a{i}", autopilot=True, labels={"environment": "prod"}) for i in range(2)]
        clusters += [cluster(f"s{i}", labels={"environment": "prod"}) for i in range(2)]

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, json.dumps(clusters))
            return run_of(0)

        manifest = collected(run, now=NOW)
        self.assertEqual(
            {c["name"]: c["autopilot"] for c in manifest["clusters"]},
            {T("a0"): True, T("a1"): True, T("s0"): False, T("s1"): False},
        )

    def test_the_project_level_entry_claims_no_mode(self):
        """A project is not a cluster. The gate-failed entry stands for a whole
        `clusters list` that never answered, so there is no mode to publish and
        a `false` there would read as a fleet of Standard clusters."""

        def run(argv, **kwargs):
            return run_of(1, "", "denied")

        entry = collected(run, now=NOW)["clusters"][0]
        self.assertEqual(entry["name"], "project/acme")
        self.assertNotIn("autopilot", entry)


class AutopilotNotApplicableTest(unittest.TestCase):
    """The eleven `standard_only` facets have to be declared, not just dropped.

    `compute_drift` skips them for an Autopilot cohort, which is right — each
    reads a `.nodePools[]` field, or a node-management or dataplane setting
    Google owns and no operator can diverge on. But dropping a slug leaves it
    missing from `commands`, which is also how a check nobody ran looks, so §6
    counts it as a coverage gap unless the model excuses it by hand.

    The list is #1226's, and its length is load-bearing in both directions.
    Declaring too few leaves the difference as a `limitations` string on every
    Autopilot cluster; declaring too many claims a configurable setting cannot
    be compared when it can, and the eight that are configurable on both modes
    are exactly the ones §2.3 compares across them.
    """

    NA = (
        "secure-boot", "integrity-monitoring", "node-autoprovisioning", "pool-autoscaling",
        "image-type", "shielded-nodes", "datapath-provider", "intra-node-visibility",
        "managed-prometheus", "logging-components", "monitoring-components",
    )

    def manifest(self, clusters):
        clusters_json = json.dumps(clusters)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        return collected(run, now=NOW)

    def autopilot_cohort(self, n=4):
        return [cluster(f"a{i}", autopilot=True, labels={"environment": "prod"}) for i in range(n)]

    def test_the_eleven_are_declared_with_a_reason(self):
        entry = self.manifest(self.autopilot_cohort())["clusters"][0]
        declared = {n["check"]: n["reason"] for n in entry["checks_not_applicable"]}
        self.assertEqual(sorted(declared), sorted(self.NA))
        for reason in declared.values():
            self.assertIn("Autopilot", reason)

    def test_none_of_the_eleven_is_also_claimed_as_a_check_that_ran(self):
        entry = self.manifest(self.autopilot_cohort())["clusters"][0]
        ran = {c["check"] for c in entry["commands"]}
        self.assertEqual(ran & set(self.NA), set())

    def test_a_standard_cluster_declares_nothing_and_runs_them(self):
        clusters = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)]
        entry = self.manifest(clusters)["clusters"][0]
        self.assertNotIn("checks_not_applicable", entry)
        self.assertLessEqual(set(self.NA), {c["check"] for c in entry["commands"]})

    def test_an_undersized_autopilot_cohort_still_declares_them(self):
        """The live fleet's shape: two Autopilot clusters in one cohort against
        a floor of three, so no facet compared and every facet slug is missing
        from `commands`. The `limitations` sentence covers the ones that could
        have run; these five could not have, cohort or no cohort, and belong
        out of the denominator rather than inside the sentence.

        `no-environment-label` is the one slug still in `commands`, because it
        is the one check that does not need a cohort -- these clusters are
        labelled, so it ran and passed."""
        entry = self.manifest(self.autopilot_cohort(n=2))["clusters"][0]
        self.assertEqual([c["check"] for c in entry["commands"]], [fd.UNLABELLED_SLUG])
        self.assertIn("no facet compared", entry["limitations"])
        self.assertEqual(sorted(n["check"] for n in entry["checks_not_applicable"]), sorted(self.NA))

    def test_datapath_provider_is_declared_rather_than_computed_and_ignored(self):
        """It used to carry an `autopilot_excluded` flag of its own: computed,
        recorded in `checks_run`, and silently never flagged. That is a claim
        the manifest should not make -- the facet reads a dataplane Google
        chooses on Autopilot, so there is nothing operator-chosen to compare
        and `checks_not_applicable` is where it belongs. #1226 put it there and
        the separate flag is gone."""
        entry = self.manifest(self.autopilot_cohort())["clusters"][0]
        self.assertIn("datapath-provider", [n["check"] for n in entry["checks_not_applicable"]])
        self.assertNotIn("datapath-provider", {c["check"] for c in entry["commands"]})
        self.assertFalse(hasattr(fd.Facet, "autopilot_excluded"))

    def test_an_autopilot_cluster_is_compared_on_the_eight_that_are_configurable(self):
        """The other half of #1226, and the reason the eleven can be declared
        at all. An Autopilot cluster is a full member of its cluster-level
        cohort, so `binary-authorization` and the seven like it run on it --
        against Standard peers where those are what the fleet holds."""
        fleet = [cluster("a0", autopilot=True, labels={"environment": "prod"})]
        fleet += [cluster(f"s{i}", labels={"environment": "prod"}) for i in range(3)]
        entry = next(e for e in self.manifest(fleet)["clusters"]
                     if e["name"].endswith("a0"))
        ran = {c["check"] for c in entry["commands"]}
        configurable = {f.slug for f in fd.FACETS if not f.standard_only}
        self.assertEqual(len(configurable), 8)
        self.assertLessEqual(configurable, ran)
        self.assertNotIn("limitations", entry)

    def test_the_two_together_account_for_the_whole_roster(self):
        entry = self.manifest(self.autopilot_cohort())["clusters"][0]
        ran = {c["check"] for c in entry["commands"]}
        declared = {n["check"] for n in entry["checks_not_applicable"]}
        roster = {f.slug for f in fd.FACETS} | {fd.UNLABELLED_SLUG}
        self.assertEqual(roster - ran - declared, set())
        self.assertEqual(ran & declared, set())


class UnvotedFacetsTest(unittest.TestCase):
    """A facet a compared cluster did not vote on has to be accounted for.

    §6 reads `commands` as the roster of checks that ran and divides it by the
    full roster, so a slug in none of `commands`, `checks_not_applicable` or a
    `limitations` sentence reports the cluster `partial` on every run with
    nothing saying what is missing. `cohort_limitations` speaks only for
    clusters nothing compared at all, and a cluster compared on fifteen facets
    out of nineteen fell between the two.
    """

    POOL_FACETS = ["image-type", "integrity-monitoring", "pool-autoscaling", "secure-boot"]

    def manifest(self, clusters):
        clusters_json = json.dumps(clusters)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        return {c["name"]: c for c in collected(run, now=NOW)["clusters"]}

    def test_a_cluster_with_no_node_pools_declares_the_pool_facets(self):
        """The Autopilot position on a Standard cluster: nothing to read, so
        `autopilot_not_applicable`'s call applies for the same slugs."""
        fleet = [cluster(f"c{i}") for i in range(4)]
        fleet[0]["nodePools"] = []
        entry = self.manifest(fleet)[T("c0")]
        declared = {n["check"]: n["reason"] for n in entry["checks_not_applicable"]}
        self.assertEqual(sorted(declared), self.POOL_FACETS)
        for reason in declared.values():
            self.assertIn("runs no node pools", reason)
        # Not a gap as well as a declaration, and the facet that reads a
        # cluster-level field still voted.
        self.assertNotIn("limitations", entry)
        self.assertIn("node-autoprovisioning", {c["check"] for c in entry["commands"]})

    def test_a_windows_only_cluster_is_not_an_outlier_on_image_type(self):
        """`_flag_not_superset` reads the empty set as a subset of every
        baseline, so a cluster with no Linux pool was an outlier missing every
        image type its cohort runs -- a finding whose remediation is to set an
        image type on a node pool that does not exist."""
        # Eleven, so `r` clears §3.5's 0.90 rung and the finding is not
        # dropped by the severity ladder instead of by the fix.
        fleet = [cluster(f"c{i}") for i in range(11)]
        fleet[0]["nodePools"] = [pool("win", image="WINDOWS_LTSC_CONTAINERD")]
        entry = self.manifest(fleet)[T("c0")]
        self.assertEqual(entry["candidates"], [])
        declared = {n["check"]: n["reason"] for n in entry["checks_not_applicable"]}
        self.assertIn("image-type", declared)
        self.assertIn("every node pool runs Windows", declared["image-type"])

    def test_a_cluster_on_no_release_channel_declares_the_facet(self):
        """A gap forces `partial` and pins `resolved` at 0 on every run, and
        §4.1 hands enrolment to the Upgrade & Patch Readiness audit -- so a
        gap here is one nothing this stream may recommend would ever close.
        One static-version cluster would keep the whole fleet's remediation
        pull requests open for as long as it ran."""
        fleet = [cluster(f"c{i}") for i in range(4)]
        fleet[0]["releaseChannel"] = {}
        entry = self.manifest(fleet)[T("c0")]
        self.assertNotIn("release-channel", {c["check"] for c in entry["commands"]})
        declared = {n["check"]: n["reason"] for n in entry["checks_not_applicable"]}
        self.assertIn("release-channel", declared)
        self.assertIn("Upgrade & Patch Readiness", declared["release-channel"])
        self.assertNotIn("`release-channel`", entry.get("limitations", ""))

    def test_a_cohort_that_reached_no_baseline_says_so_for_every_member(self):
        """§3.3 wants one token on two thirds of the readable values. An evenly
        split cohort has no majority to be an outlier from, so the facet is
        uncompared for all four members and not one of them said so."""
        fleet = [cluster(f"c{i}", **{"networkConfig.enableIntraNodeVisibility": i < 2}) for i in range(4)]
        entries = self.manifest(fleet)
        for name in (T("c0"), T("c3")):
            entry = entries[name]
            self.assertNotIn("intra-node-visibility", {c["check"] for c in entry["commands"]})
            self.assertIn("reached no baseline in cohort standard/prod", entry["limitations"])
            self.assertIn("`intra-node-visibility` (4 readable value(s), commonest on 2)", entry["limitations"])

    def test_the_three_together_account_for_the_whole_roster(self):
        fleet = [cluster(f"c{i}", **{"networkConfig.enableIntraNodeVisibility": i < 2}) for i in range(4)]
        fleet[0]["nodePools"] = []
        fleet[1]["releaseChannel"] = {}
        roster = {f.slug for f in fd.FACETS} | {fd.UNLABELLED_SLUG}
        for entry in self.manifest(fleet).values():
            ran = {c["check"] for c in entry["commands"]}
            declared = {n["check"] for n in entry.get("checks_not_applicable", [])}
            named = {slug for slug in roster if f"`{slug}`" in entry.get("limitations", "")}
            self.assertEqual(roster - ran - declared - named, set(), entry["name"])
            self.assertEqual(ran & declared, set(), entry["name"])
            self.assertEqual(ran & named, set(), entry["name"])


class CohortLimitationsTest(unittest.TestCase):
    """A cluster no facet compared has to say so.

    The live four-cluster fleet, as `_floored_fleet` below builds it: two
    autopilot clusters labelled `environment=prod`, one autopilot labelled
    `test`, one standard labelled `prod`. A cohort key is (mode, environment),
    so that is cohorts of 2, 1 and 1 against a floor of 3 — every cohort
    abstained and not one facet was compared, and the manifest called all four
    `collected` with a `commands` list holding only `no-environment-label`,
    four seconds after it started. `collected` is what tells the model the
    target needs no manual fallback, so nothing downstream had any way to know
    the comparison never happened.
    """

    def _floored_fleet(self):
        return [
            cluster("auto-a", autopilot=True),
            cluster("auto-b", autopilot=True),
            cluster("auto-test", autopilot=True, labels={"environment": "test"}),
            cluster("std-a"),
        ]

    def test_every_member_of_an_undersized_cohort_is_explained(self):
        """§2.3's two cohortings give this fleet three different answers.

        `auto-a`, `auto-b` and `std-a` all carry `prod`, so the cluster-level
        cohort `prod` holds three and reaches the floor -- which is the whole
        of what #1226 bought: before it, mode split them 2 and 1 and every one
        of the eight configurable facets went uncompared on all three.
        """
        lim = fd.cohort_limitations(self._floored_fleet(), now=NOW)
        # Only `auto-test`, alone in the cluster-level cohort `test`.
        self.assertIn("only 1 comparable cluster ", lim[K("auto-test")])
        self.assertIn("no facet compared", lim[K("auto-test")])
        for text in lim.values():
            self.assertIn(f"minimum {fd.COHORT_FLOOR}", text)

    def test_a_standard_cluster_short_of_node_level_peers_says_which_class_it_lost(self):
        """The narrow sentence #1226 specifies. `std-a` is compared on the
        eight configurable facets in cohort `prod` and on none of the eleven
        node-level ones, because it is the only Standard cluster there -- so
        the sentence has to say that rather than "no facet compared", which
        would understate the coverage the run actually got."""
        lim = fd.cohort_limitations(self._floored_fleet(), now=NOW)
        text = lim[K("std-a")]
        self.assertIn("cohort standard/prod has only 1 comparable cluster", text)
        self.assertIn("11 node-level facets uncompared", text)
        self.assertNotIn("no facet compared", text)

    def test_an_autopilot_cluster_short_of_node_level_peers_has_nothing_to_report(self):
        """The asymmetry that keeps an Autopilot minority from pinning the
        ledger. `auto-a` and `auto-b` are two in `(autopilot, prod)`, under the
        floor -- but the eleven facets that cohort would have compared are
        already `checks_not_applicable` on Autopilot, so nothing went
        uncompared and a `limitations` string here would invent a coverage gap
        the mode had already settled."""
        lim = fd.cohort_limitations(self._floored_fleet(), now=NOW)
        self.assertNotIn(K("auto-a"), lim)
        self.assertNotIn(K("auto-b"), lim)

    def test_the_sentence_names_the_cohort_it_floored_out_of(self):
        lim = fd.cohort_limitations(self._floored_fleet(), now=NOW)
        # No mode on a cluster-level key, and a mode on a node-level one.
        self.assertIn("cohort test ", lim[K("auto-test")])
        self.assertIn("cohort standard/prod ", lim[K("std-a")])

    def test_the_lone_unlabelled_cluster_is_told_a_label_is_the_difference(self):
        # The live fleet's shape: fifteen of sixteen carry `environment=test`,
        # kube-agents-host carries none, so it cohorts alone under 2.3 and is
        # the one cluster drift can never compare -- on this run or any later
        # one. The floor sentence alone reads as a fleet-size quirk and gets
        # waited out; the cause is what makes it fixable.
        fleet = [cluster(f"c{i}", labels={"environment": "test"}) for i in range(3)]
        fleet.append(cluster("host", labels={}))
        lim = fd.cohort_limitations(fleet, now=NOW)
        self.assertEqual(list(lim), [K("host")])
        self.assertIn("cohort unknown has only 1 comparable cluster",
                      lim[K("host")])
        self.assertIn("no environment label while 3 of 4 do", lim[K("host")])

    def _live_shape(self, host_labels):
        """adamparco-kage: 5 Autopilot and 10 Standard all `test`, plus the
        install's own Standard host cluster carrying whatever is passed."""
        fleet = [cluster(f"ap-{i}", autopilot=True, labels={"environment": "test"})
                 for i in range(5)]
        fleet += [cluster(f"std-{i}", labels={"environment": "test"}) for i in range(10)]
        fleet.append(cluster("host", labels=host_labels))
        return fleet

    def test_the_advice_names_the_only_label_that_would_reach_the_floor(self):
        # A cluster-level cohort key is (environment), so a label compares this
        # cluster if COHORT_FLOOR - 1 clusters of any mode already carry it. On
        # the live fleet that is `test` and nothing else -- and all fifteen
        # peers count toward it, not just the ten that share the host's mode.
        lim = fd.cohort_limitations(self._live_shape({}), now=NOW)
        text = lim[K("host")]
        self.assertIn("Only `environment=test` would reach the floor here", text)
        self.assertIn("15 other clusters carry `test`", text)
        self.assertIn("Any other value opens a new cohort of one", text)

    def test_a_label_that_describes_the_cluster_is_the_no_op_the_advice_warns_of(self):
        """The reason the sentence has to name values: three of the four labels
        an operator would reach for on an install's host cluster leave the gap
        exactly where it was, and say less about it than before."""
        for env in ("prod", "platform", "hub"):
            with self.subTest(environment=env):
                lim = fd.cohort_limitations(
                    self._live_shape({"environment": env}), now=NOW)
                text = lim[K("host")]
                self.assertIn(f"cohort {env} has only 1 comparable cluster",
                              text)
                # Not `unknown` any more, so the whole cause clause drops: the
                # operator who did what the sentence asked is told strictly
                # less than they were before they did it.
                self.assertNotIn("environment label", text)
        # ... and the one value the sentence does name compares the cluster,
        # which is what makes naming it worth doing.
        self.assertEqual(
            fd.cohort_limitations(self._live_shape({"environment": "test"}), now=NOW),
            {})

    def test_a_fleet_where_no_label_would_help_says_so_instead(self):
        """Naming values must not become naming none of them silently. Every
        named value here is held by one cluster, so none of them has the two
        peers a label would need and no label closes the gap.

        The fleet this used to use -- three Standard clusters on `test` and a
        lone unlabelled Autopilot one -- no longer reaches this branch, and
        that is the fix rather than a weakening of the test: mode is out of the
        cluster-level key, so `test` has three holders of any mode and the
        advice now names it. `test_an_autopilot_cluster_joins_a_standard_
        cohort` covers that directly.
        """
        fleet = [cluster("p", labels={"environment": "prod"}),
                 cluster("t", labels={"environment": "test"}),
                 cluster("host", labels={})]
        text = fd.cohort_limitations(fleet, now=NOW)[K("host")]
        self.assertIn("No environment value on this fleet has the 2 other"
                      " clusters", text)
        self.assertNotIn("would reach the floor here", text)

    def test_an_autopilot_cluster_joins_a_standard_cohort(self):
        """#1226's point, as a sentence an operator reads. An Autopilot cluster
        with no label on a fleet of labelled Standard ones is told to set the
        label, because the cohort it would join is cluster-level and does not
        care about the mode. Under a mode-first key it was told instead that no
        label on the fleet would help it, which was true and permanent."""
        fleet = [cluster(f"std-{i}", labels={"environment": "test"}) for i in range(3)]
        fleet.append(cluster("host", autopilot=True, labels={}))
        text = fd.cohort_limitations(fleet, now=NOW)[K("host")]
        self.assertIn("Only `environment=test` would reach the floor here", text)
        self.assertIn("3 other clusters carry `test`", text)

    def test_two_joinable_values_are_both_named(self):
        fleet = [cluster(f"p{i}", labels={"environment": "prod"}) for i in range(2)]
        fleet += [cluster(f"t{i}", labels={"environment": "test"}) for i in range(4)]
        fleet.append(cluster("host", labels={}))
        text = fd.cohort_limitations(fleet, now=NOW)[K("host")]
        # Descending peer count, so the value that needs the least explaining
        # comes first; `prod` is floored itself at 2 but reaches 3 with this one.
        self.assertIn("Only `environment=test` or `environment=prod` would reach"
                      " the floor here", text)

    def test_a_fleet_nobody_labelled_is_not_told_to_add_a_label(self):
        # Every cluster unknown together cohorts by mode alone, so there is no
        # named cohort being kept out of and nothing to point at. Counting the
        # label source rather than the resolved environment is what keeps the
        # inferred strategy out too: those clusters carry no label either, and
        # a count of them would make the sentence false.
        fleet = [cluster(f"c{i}", labels={}) for i in range(2)]
        lim = fd.cohort_limitations(fleet, now=NOW)
        self.assertEqual(len(lim), 2)
        for text in lim.values():
            self.assertIn("no facet compared", text)
            self.assertNotIn("environment label", text)

    def test_a_cohort_that_reaches_the_floor_explains_nothing(self):
        """A compared cluster must not carry a limitation — it would read as a
        coverage gap on a cluster that was in fact fully voted on."""
        fleet = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)]
        self.assertEqual(fd.cohort_limitations(fleet, now=NOW), {})

    def test_an_ineligible_cluster_carries_its_eligibility_reason(self):
        fleet = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)]
        fleet.append(cluster("broken", labels={"environment": "prod"}, status="DEGRADED"))
        lim = fd.cohort_limitations(fleet, now=NOW)
        self.assertEqual(set(lim), {K("broken")})
        self.assertIn("status DEGRADED", lim[K("broken")])

    def test_the_manifest_carries_the_sentence(self):
        clusters_json = json.dumps(self._floored_fleet())

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = collected(run, now=NOW)
        self.assertEqual(len(manifest["clusters"]), 4)
        by_name = {e["name"].rsplit("/", 1)[-1]: e for e in manifest["clusters"]}
        # `auto-test` is alone in the cluster-level cohort `test`, so nothing
        # compared it and `no-environment-label` -- the one check that runs
        # without a cohort -- is all its `commands` holds.
        entry = by_name["auto-test"]
        self.assertEqual([c["check"] for c in entry["commands"]], [fd.UNLABELLED_SLUG])
        self.assertIn("no facet compared", entry["limitations"])
        # The other three share `prod` and reach the cluster-level floor
        # together, so the eight configurable facets ran on all three across
        # both modes -- the comparison a mode-first key threw away.
        for name in ("auto-a", "auto-b", "std-a"):
            slugs = [c["check"] for c in by_name[name]["commands"]]
            self.assertIn("binary-authorization", slugs)
            self.assertNotIn("image-type", slugs)

    def test_a_compared_fleet_gets_no_limitations_key_at_all(self):
        clusters_json = json.dumps(
            [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)]
        )

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = collected(run, now=NOW)
        for entry in manifest["clusters"]:
            self.assertNotIn("limitations", entry)
            self.assertTrue(entry["commands"])

    def test_the_limitation_reaches_the_coverage_arithmetic(self):
        """End to end: the sentence has to become a coverage gap, or the run
        still reports a fleet nobody compared as a clean one."""
        import audit_report

        lim = fd.cohort_limitations(self._floored_fleet(), now=NOW)
        doc = {
            "audit": "fleet-consistency-drift",
            "scope": {
                "clusters": [
                    {
                        "name": name,
                        "location": "us-central1",
                        "project": "acme",
                        "checks_run": [],
                        "limitations": text,
                    }
                    for name, text in sorted(lim.items())
                ],
                "skipped": [],
            },
            "findings": [],
        }
        gaps = audit_report.coverage_gaps(doc)
        # Two sentences, two gaps: `auto-test`, which nothing compared, and
        # `std-a`, which lost the eleven node-level facets. The two Autopilot
        # clusters produce neither, because what their undersized node cohort
        # would have compared is inapplicable on their mode.
        self.assertEqual(len(gaps), 2)
        self.assertTrue(any("no facet compared" in g for g in gaps))
        self.assertTrue(any("node-level facets uncompared" in g for g in gaps))


class NoEnvironmentLabelTest(unittest.TestCase):
    """§4.14 — the one check that fires outside a cohort.

    Every facet is a majority vote, so §2.4's floor silences all nineteen for a
    cluster with too few peers, and §2.3 gives an unlabelled cluster no peers at
    all. The cluster whose labelling diverges most from the fleet was therefore
    the one cluster `label-keys` could never see: `cohort_limitations` said so
    in a sentence, and a sentence in `limitations` opens no pull request.
    """

    def _live_shape(self, host_labels, *, host_autopilot=False):
        """adamparco-kage: 5 Autopilot and 10 Standard all `test`, plus the
        install's own host cluster carrying whatever is passed."""
        fleet = [cluster(f"ap-{i}", autopilot=True, labels={"environment": "test"})
                 for i in range(5)]
        fleet += [cluster(f"std-{i}", labels={"environment": "test"}) for i in range(10)]
        fleet.append(cluster("host", autopilot=host_autopilot, labels=host_labels))
        return fleet

    def check(self, fleet):
        return fd.unlabelled_environment_candidates(fleet, now=NOW)

    def test_the_lone_unlabelled_cluster_gets_a_finding(self):
        _run, candidates, _na = self.check(self._live_shape({}))
        self.assertEqual(list(candidates), [K("host")])
        found = candidates[K("host")][0]
        self.assertEqual(found["check"], fd.UNLABELLED_SLUG)
        self.assertEqual(found["object"], "Cluster/host")
        self.assertEqual(found["severity"], "minor")
        self.assertEqual(found["namespace"], "")

    def test_the_finding_names_the_label_to_set_and_the_peers_that_hold_it(self):
        _run, candidates, _na = self.check(self._live_shape({}))
        excerpt = candidates[K("host")][0]["excerpt"]
        self.assertIn("Set `resourceLabels.environment` to `test`", excerpt)
        self.assertIn("15 other clusters carry it", excerpt)

    def test_a_fleet_that_cohorts_on_inferred_names_claims_no_labels(self):
        """§2.3 takes the `environment` strategy from a naming convention too,
        so the finding can fire on a fleet where nothing is labelled at all.
        "0 of 4 eligible clusters on this fleet do" beside a finding about the
        one cluster that does not is a sentence arguing against itself, and
        `_unlabelled_cause` already drops its own version of that clause on the
        same count."""
        fleet = [cluster(f"prod-{i}", labels={}) for i in range(3)] + [cluster("host", labels={})]
        _run, candidates, _na = self.check(fleet)
        excerpt = candidates[K("host")][0]["excerpt"]
        self.assertNotIn("0 of 4", excerpt)
        self.assertIn("nor does any of the other 3 eligible clusters", excerpt)
        # And the peers do not *carry* the label the finding asks for, either.
        self.assertIn("3 other clusters resolve to it from their names", excerpt)

    def test_the_finding_and_the_coverage_gap_prescribe_the_same_value(self):
        """The reason `joinable_environments` is a function. The gap sentence
        and the finding are read by the same person minutes apart, and an audit
        that prescribes a label its own gap sentence says will not work has
        told them to do nothing twice."""
        fleet = self._live_shape({})
        _run, candidates, _na = self.check(fleet)
        gap = fd.cohort_limitations(fleet, now=NOW)[K("host")]
        self.assertIn("Only `environment=test` would reach the floor here", gap)
        self.assertIn("to `test`", candidates[K("host")][0]["excerpt"])

    def test_the_gap_sentence_withholds_a_value_that_closes_only_half_the_gap(self):
        """The test above is a fleet where the two cohortings happen to agree,
        so it cannot see the case where they do not. Here `test` is carried by
        two clusters that straddle the mode split: the cluster-level cohort
        `(test)` reaches the floor with `host` in it and the node-level
        `(standard, test)` does not, so labelling `host` starts the eight
        configurable facets comparing and leaves the eleven node-level ones
        exactly as they were. §4.14 intersects the two and publishes nothing;
        the gap sentence read only the cluster-level cohorts, so it prescribed
        `environment=test` beside a finding that had just withheld it."""
        fleet = [
            cluster("host", labels={}),
            cluster("one", labels={"environment": "test"}),
            cluster("two", labels={"environment": "test"}, autopilot=True),
        ]
        _run, candidates, _na = self.check(fleet)
        self.assertEqual(candidates, {})
        gap = fd.cohort_limitations(fleet, now=NOW)[K("host")]
        self.assertNotIn("Only `environment=test` would reach the floor", gap)
        # It still names the value rather than claiming none exists -- an
        # operator told "no value reaches the floor" about a cohort with two
        # `test` peers in it reads a sentence that looks false.
        self.assertIn("`environment=test` would reach this cohort's floor", gap)
        self.assertIn("no single label closes both gaps", gap)

    def test_a_labelled_cluster_runs_the_check_and_passes_it(self):
        run, candidates, na = self.check(self._live_shape({"environment": "test"}))
        self.assertEqual(candidates, {})
        self.assertEqual(na, {})
        # Every eligible cluster, not just the one that could have failed:
        # a slug missing from `checks_run` is how a check nobody ran looks.
        self.assertEqual(len(run), 16)
        self.assertEqual(run[K("host")], [fd.UNLABELLED_SLUG])

    def test_an_inferred_environment_is_not_this_defect(self):
        """A name token cohorts the cluster, on a guess §3.5 already charges a
        severity step for. `joins no cohort` would be false of it."""
        fleet = self._live_shape({})
        fleet[-1] = cluster("host-test", labels={})
        _run, candidates, _na = self.check(fleet)
        self.assertEqual(candidates, {})

    def test_unlabelled_clusters_that_reach_the_floor_together_are_compared(self):
        """Three unlabelled Standard clusters cohort as `standard/unknown` and
        compare each other, which is the coverage this finding claims is
        missing. Publishing it anyway would be false about its own impact."""
        fleet = [cluster(f"std-{i}", labels={"environment": "test"}) for i in range(4)]
        fleet += [cluster(f"bare-{i}", labels={}) for i in range(3)]
        run, candidates, _na = self.check(fleet)
        self.assertEqual(candidates, {})
        self.assertEqual(len(run), 7)

    def test_no_finding_where_no_label_would_close_the_gap(self):
        """The §3.7 rule: withhold a finding whose own remediation is a no-op.
        Here the fleet's two labels are held by one cluster each, so whichever
        value the unlabelled cluster took it would land in a cohort of two and
        stay under the floor. The check still ran -- the gap is real, a label
        just is not the fix.

        Mode is not what makes this fleet unfixable, and under #1226 it cannot
        be: the eight cluster-level facets cohort on `(environment)` alone, so
        an Autopilot cluster taking `test` joins its Standard peers there and a
        label would work."""
        fleet = [cluster("std-prod", labels={"environment": "prod"}),
                 cluster("std-test", labels={"environment": "test"})]
        fleet.append(cluster("host", autopilot=True, labels={}))
        run, candidates, na = self.check(fleet)
        self.assertEqual(candidates, {})
        self.assertEqual(na, {})
        self.assertEqual(run[K("host")], [fd.UNLABELLED_SLUG])

    def _straddling_shape(self):
        """The fleet shape #1226's two cohortings make possible and Adam's one
        did not: enough unlabelled clusters to reach the floor on
        `(environment)`, but not at either mode on `(mode, environment)`.

        Three unlabelled Autopilot clusters and two unlabelled Standard ones
        put five in the cluster-level `unknown` cohort, so the eight
        configurable facets compare -- against a group whose only shared
        property is the omission. The Standard pair's node-level cohort is two,
        so the eleven node-level facets compare nothing for them. A Standard
        `seeded` trio gives a value that closes both.
        """
        fleet = [cluster(f"ap-{i}", autopilot=True, labels={}) for i in range(3)]
        fleet += [cluster(f"std-{i}", labels={}) for i in range(2)]
        fleet += [cluster(f"seeded-{i}", labels={"environment": "seeded"}) for i in range(3)]
        return fleet

    def test_a_short_node_cohort_fires_even_where_the_cluster_cohort_does_not(self):
        """The defect a live run found on the restored cohorting, and the
        reason this check reads both cohorts rather than one.

        Before this, §4.14 asked only whether the cluster-level cohort reached
        the floor. On the fleet above it does, so the check abstained -- while
        the run's own `limitations` said "11 node-level facets uncompared" for
        the same cluster. The audit held both halves of the contradiction and
        published neither as a finding, on a gap the label it declines to
        recommend would have closed.
        """
        _run, candidates, _na = self.check(self._straddling_shape())
        self.assertEqual(sorted(candidates), [K("std-0"), K("std-1")])

    def test_an_autopilot_cluster_on_that_fleet_gets_no_finding(self):
        """Its eleven are `checks_not_applicable`, so a short
        `(autopilot, unknown)` cohort withholds nothing from it and no label
        would be closing a gap. It is compared on all eight it owes."""
        candidates = self.check(self._straddling_shape())[1]
        self.assertNotIn(K("ap-0"), candidates)

    def test_the_excerpt_does_not_claim_all_nineteen_abstained(self):
        """`abstaining` counts the cluster's whole roster, which is the right
        number only when both cohorts are short. Printed against a cluster
        being compared on eight, it overstates the gap by the eight it is
        wrong about -- and an operator who checks finds the finding contradicted
        by the same ledger's coverage column."""
        excerpt = self.check(self._straddling_shape())[1][K("std-0")][0]["excerpt"]
        self.assertIn("11 facets keyed `(mode, environment)` compare nothing", excerpt)
        self.assertIn("the other 8 reach a baseline", excerpt)
        self.assertNotIn("all 19 comparative checks", excerpt)

    def test_the_peers_named_are_the_ones_at_this_clusters_mode(self):
        """Where the gap is node-level, the floor being described is the
        node-level one, so the count beside the prescribed value has to be the
        peers at this mode. Quoting the cluster-level count names a number that
        does not reach the floor the sentence is about: here six clusters
        resolve to `seeded` but only the three Standard ones close this gap."""
        fleet = self._straddling_shape()
        fleet += [cluster(f"ap-seeded-{i}", autopilot=True,
                          labels={"environment": "seeded"}) for i in range(3)]
        excerpt = self.check(fleet)[1][K("std-0")][0]["excerpt"]
        self.assertIn("Set `resourceLabels.environment` to `seeded`", excerpt)
        self.assertIn("3 other clusters carry it", excerpt)

    def test_a_value_closing_only_one_of_two_gaps_is_not_prescribed(self):
        """With both cohorts short the advised value has to reach both floors.
        `ap` is held by two Autopilot clusters, so it reaches the cluster-level
        floor for the Standard host and closes nothing node-level; `seeded` is
        held by two Standard ones and closes both. Prescribing `ap` would be
        the no-op §3.7 of the cost SOP withholds, and worse than the no-op
        because the operator would see eight facets start comparing and take
        the other eleven for a different problem."""
        fleet = [cluster(f"ap-{i}", autopilot=True, labels={"environment": "ap"})
                 for i in range(2)]
        fleet += [cluster(f"seeded-{i}", labels={"environment": "seeded"}) for i in range(2)]
        fleet.append(cluster("host", labels={}))
        excerpt = self.check(fleet)[1][K("host")][0]["excerpt"]
        self.assertIn("Set `resourceLabels.environment` to `seeded`", excerpt)
        self.assertNotIn("`ap` would also reach it", excerpt)

    def test_a_second_joinable_value_is_offered_but_not_prescribed(self):
        fleet = [cluster(f"p{i}", labels={"environment": "prod"}) for i in range(2)]
        fleet += [cluster(f"t{i}", labels={"environment": "test"}) for i in range(4)]
        fleet.append(cluster("host", labels={}))
        _run, candidates, _na = self.check(fleet)
        excerpt = candidates[K("host")][0]["excerpt"]
        self.assertIn("Set `resourceLabels.environment` to `test`", excerpt)
        self.assertIn("(`prod` would also reach it, on 2)", excerpt)

    def test_the_abstaining_count_is_the_clusters_own_roster(self):
        """An Autopilot cluster owes eight facets, not nineteen — the eleven
        `standard_only` ones are declared inapplicable elsewhere and counting
        them here would overstate what the label buys."""
        fleet = [cluster(f"ap-{i}", autopilot=True, labels={"environment": "test"})
                 for i in range(3)]
        fleet.append(cluster("host", autopilot=True, labels={}))
        _run, candidates, _na = self.check(fleet)
        standard_only = sum(1 for f in fd.FACETS if f.standard_only)
        self.assertIn(f"all {len(fd.FACETS) - standard_only} comparative checks",
                      candidates[K("host")][0]["excerpt"])
        _run, standard, _na = self.check(self._live_shape({}))
        self.assertIn(f"all {len(fd.FACETS)} comparative checks",
                      standard[K("host")][0]["excerpt"])

    def test_a_fleet_that_does_not_cohort_by_environment_declares_it_na(self):
        """Under `project` or `mode-only` no cohort key holds an environment,
        so a label changes nothing about who this cluster is compared against.
        Silence would read as a check nobody ran, on every cluster, forever."""
        fleet = [cluster(f"c{i}", project=p, labels={})
                 for p in ("acme", "other") for i in range(2)]
        run, candidates, na = self.check(fleet)
        self.assertEqual(run, {})
        self.assertEqual(candidates, {})
        self.assertEqual(len(na), 4)
        reason = next(iter(na.values()))[0]["reason"]
        self.assertEqual(next(iter(na.values()))[0]["check"], fd.UNLABELLED_SLUG)
        self.assertIn("cohorts this fleet by `project`", reason)

    def test_an_ineligible_cluster_neither_runs_it_nor_declares_it(self):
        """§1 already excluded it for a reason a label cannot fix, and its
        `limitations` string covers the whole roster."""
        fleet = self._live_shape({})
        fleet[-1] = cluster("host", labels={}, status="PROVISIONING")
        run, candidates, na = self.check(fleet)
        self.assertNotIn(K("host"), run)
        self.assertEqual(candidates, {})
        self.assertEqual(na, {})

    def test_an_ineligible_cluster_is_not_declared_when_cohorts_ignore_environment(self):
        """Under `project` the N/A reason says every cluster is compared, which a
        cluster §1 excluded is not; its `limitations` already covers it."""
        fleet = [cluster(f"c{i}", project=p, labels={})
                 for p in ("acme", "other") for i in range(2)]
        fleet.append(cluster("host", project="other", labels={}, status="PROVISIONING"))
        run, candidates, na = self.check(fleet)
        self.assertEqual(len(na), 4)
        self.assertNotIn(K("host", project="other"), na)

    def test_the_finding_reaches_the_manifest(self):
        clusters_json = json.dumps(self._live_shape({}))

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = collected(run, now=NOW)
        host = next(e for e in manifest["clusters"] if e["name"] == T("host"))
        self.assertEqual([c["check"] for c in host["candidates"]], [fd.UNLABELLED_SLUG])
        self.assertIn(fd.UNLABELLED_SLUG, [c["check"] for c in host["commands"]])
        # The compared clusters ran it too, and none of them failed it.
        peer = next(e for e in manifest["clusters"] if e["name"] == T("std-0"))
        self.assertIn(fd.UNLABELLED_SLUG, [c["check"] for c in peer["commands"]])
        self.assertEqual(peer["candidates"], [])

    def test_the_slug_is_on_the_audit_report_roster(self):
        """A finding whose slug the roster does not carry is rejected at
        `finish`, so the collector would publish nothing."""
        import audit_report

        spec = audit_report.AUDITS["fleet-consistency-drift"]
        self.assertIn(fd.UNLABELLED_SLUG, spec.checks)
        self.assertEqual(len(spec.checks), len(fd.FACETS) + 1)


class ManifestComposesWithAuditReportTest(unittest.TestCase):
    def test_checks_run_copied_from_a_collected_cluster_survives_cross_check(self):
        import audit_report

        clusters = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(4)]
        clusters_json = json.dumps(clusters)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = collected(run, now=NOW)
        data = {
            "audit": "fleet-consistency-drift",
            "scope": {
                "clusters": [
                    {"name": e["name"], "checks_run": [{"check": c["check"], "command": c["command"]} for c in e["commands"]]}
                    for e in manifest["clusters"]
                ],
                "skipped": [],
            },
        }
        audit_report.cross_check_manifest(data, manifest)  # must not raise

    def test_a_check_absent_from_the_manifest_is_rejected(self):
        """The document is otherwise complete and correct: every cluster the
        manifest collected is named, under the manifest's own qualified name,
        with exactly the checks the collector recorded. The one difference is
        the fabricated `shielded-nodes` entry, so the rejection can only be
        the one this test is about -- an earlier version documented a single
        cluster as `c0` and was rejected for omitting both qualified names
        before `checks_run` was read at all."""
        import audit_report

        clusters = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(2)]  # under the floor
        clusters_json = json.dumps(clusters)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = collected(run, now=NOW)

        def document(extra_checks):
            return {
                "audit": "fleet-consistency-drift",
                "scope": {
                    "clusters": [
                        {
                            "name": entry["name"],
                            "project": entry["project"],
                            "location": entry["location"],
                            "checks_run": [
                                {"check": c["check"], "command": c["command"]} for c in entry["commands"]
                            ] + (extra_checks if entry["name"] == T("c0") else []),
                            "limitations": entry["limitations"],
                        }
                        for entry in manifest["clusters"]
                    ],
                    "skipped": [],
                },
                "findings": [],
            }

        # The control: under the floor no facet ran, so `no-environment-label`
        # is the whole of each cluster's `checks_run` and the document is honest.
        audit_report.cross_check_manifest(document([]), manifest)

        with self.assertRaises(audit_report.ValidationError) as caught:
            audit_report.cross_check_manifest(
                document([{"check": "shielded-nodes", "command": "x"}]), manifest
            )
        self.assertIn("shielded-nodes", str(caught.exception))
        self.assertIn("records no successful command for that check", str(caught.exception))


class CandidateSummaryTest(unittest.TestCase):
    def test_it_counts_candidates_per_check_and_names_the_clusters(self):
        manifest = {
            "clusters": [
                {"name": "a", "outcome": "collected", "candidates": [{"check": "no-environment-label"}]},
                {
                    "name": "b",
                    "outcome": "collected",
                    "candidates": [{"check": "no-environment-label"}, {"check": "authorized-networks"}],
                },
                {"name": "project/p", "outcome": "gate-failed"},
            ]
        }
        lines = fd.candidate_summary(manifest)
        self.assertIn(
            "2 cluster(s) collected, 1 project(s) unread; 3 candidate(s) to report", lines[0]
        )
        self.assertIn("no-environment-label: 2 (a, b)", lines[0])
        self.assertIn("authorized-networks: 1 (b)", lines[0])
        self.assertIn("resolved_because", lines[1])
        self.assertIn("drop it with the hand exclusion named", lines[1])

    def test_a_fleet_with_no_candidates_says_zero(self):
        manifest = {"clusters": [{"name": "a", "outcome": "collected", "candidates": []}]}
        self.assertEqual(
            fd.candidate_summary(manifest), ["1 cluster(s) collected; 0 candidate(s) to report"]
        )

    def test_it_caps_the_cluster_names_it_spells_out(self):
        over = fd.SUMMARY_MAX_CLUSTER_NAMES + 2
        manifest = {
            "clusters": [
                {"name": f"c{i}", "outcome": "collected", "candidates": [{"check": "no-environment-label"}]}
                for i in range(over)
            ]
        }
        line = fd.candidate_summary(manifest)[0]
        self.assertIn(f"no-environment-label: {over}", line)
        self.assertIn("and 2 more", line)

    def test_the_summary_follows_a_real_collection(self):
        clusters = [cluster(f"c{i}", labels={"environment": "prod"}) for i in range(3)]
        clusters[0]["resourceLabels"] = {}
        clusters_json = json.dumps(clusters)

        def run(argv, **kwargs):
            if "list" in argv and "clusters" in argv:
                return run_of(0, clusters_json)
            return run_of(0)

        manifest = collected(run, now=NOW)
        lines = fd.candidate_summary(manifest)
        self.assertIn(f"no-environment-label: 1 ({T('c0')})", lines[0])

    def test_a_project_that_could_not_be_listed_is_counted(self):
        manifest = {
            "clusters": [
                {"name": "a", "outcome": "collected", "candidates": []},
                {"name": "project/p", "outcome": "gate-failed"},
                {"name": "project/q", "outcome": "gate-failed"},
            ]
        }
        self.assertEqual(
            fd.candidate_summary(manifest),
            ["1 cluster(s) collected, 2 project(s) unread; 0 candidate(s) to report"],
        )

    def test_main_prints_the_summary_after_the_manifest(self):
        manifest = {
            "clusters": [{"name": "a", "outcome": "collected", "candidates": [{"check": "shielded-nodes"}]}]
        }
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(fd, "collect_fleet", return_value=manifest):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = fd.main([])
        self.assertEqual(rc, 0)
        self.assertIn('"clusters"', out.getvalue())
        self.assertIn("1 cluster(s) collected; 1 candidate(s) to report", err.getvalue())
        self.assertIn("shielded-nodes: 1 (a)", err.getvalue())

    def test_main_says_nothing_about_candidates_when_the_run_failed(self):
        manifest = {"clusters": [], "error": "project discovery failed"}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(fd, "collect_fleet", return_value=manifest):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = fd.main([])
        self.assertEqual(rc, 1)
        self.assertNotIn("candidate(s) to report", err.getvalue())


if __name__ == "__main__":
    unittest.main()
