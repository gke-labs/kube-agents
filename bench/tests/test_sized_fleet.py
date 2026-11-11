"""The sized test fleet's arithmetic, workloads and designed-state check (kube_agents_bench.scale.sized_fleet)."""

import collections
import re
import unittest
from pathlib import Path

from kube_agents_bench.scale import sized_fleet as sf

STACK_MAIN = Path(__file__).resolve().parents[1] / "tf" / "sized-fleet" / "main.tf"


class SizesTest(unittest.TestCase):
    def test_m_is_the_declared_fleet_and_s_and_l_halve_and_double_it(self):
        expected = {
            "S": (4, 125, 750, 250, 3),
            "M": (8, 250, 1500, 500, 5),
            "L": (16, 500, 3000, 1000, 10),
        }
        for size, (clusters, nodes, deployments, cronjobs, failing) in expected.items():
            with self.subTest(size=size):
                f = sf.fleet(size)
                self.assertEqual(
                    (len(f.clusters), f.nodes, f.deployments, f.cronjobs, f.failing_pods),
                    (clusters, nodes, deployments, cronjobs, failing),
                )

    def test_the_default_is_m(self):
        self.assertEqual(sf.fleet().size, "M")

    def test_nodes_divide_as_the_stack_divides_them(self):
        self.assertEqual([c.nodes for c in sf.fleet("M").clusters], [32, 32, 31, 31, 31, 31, 31, 31])
        self.assertEqual([c.nodes for c in sf.fleet("S").clusters], [32, 31, 31, 31])
        # The stack carries the same rule: floor, then one more for the first `remainder`.
        text = STACK_MAIN.read_text()
        self.assertIn("floor(local.nodes / local.clusters)", text)
        self.assertIn("(i < local.remainder ? 1 : 0)", text)

    def test_cluster_names_match_the_stack(self):
        self.assertEqual(sf.fleet("M").clusters[0].name, "sized-m-01")
        self.assertIn('format("sized-%s-%02d", local.size_label, i + 1)', STACK_MAIN.read_text())

    def test_the_designed_churn(self):
        self.assertEqual(sf.fleet("M").churn_per_minute, 100)
        self.assertEqual(sf.fleet("L").churn_per_minute, 200)

    def test_an_unknown_size_is_refused(self):
        with self.assertRaises(ValueError):
            sf.fleet("XL")


class WorkloadsTest(unittest.TestCase):
    def setUp(self):
        self.fleet = sf.fleet("M")
        self.shape = self.fleet.shape

    def test_each_cluster_carries_its_share(self):
        for cluster in self.fleet.clusters:
            kinds = collections.Counter(m["kind"] for m in sf.cluster_manifests(cluster, self.shape))
            self.assertEqual(kinds["Namespace"], self.shape["namespaces_per_cluster"])
            self.assertEqual(kinds["Service"], cluster.deployments)
            self.assertEqual(kinds["Deployment"], cluster.deployments + cluster.failing_pods)
            self.assertEqual(kinds["CronJob"], cluster.cronjobs)

    def test_names_are_unique_within_a_namespace(self):
        for cluster in self.fleet.clusters:
            keys = [(m["kind"], m["metadata"].get("namespace"), m["metadata"]["name"])
                    for m in sf.cluster_manifests(cluster, self.shape)]
            self.assertEqual(len(keys), len(set(keys)))

    def test_start_minutes_spread_evenly(self):
        period = self.shape["cronjob_period_minutes"]
        offsets = collections.Counter()
        for cluster in self.fleet.clusters:
            for m in sf.cluster_manifests(cluster, self.shape):
                if m["kind"] == "CronJob":
                    match = re.fullmatch(rf"(\d)-59/{period} \* \* \* \*", m["spec"]["schedule"])
                    self.assertIsNotNone(match, m["spec"]["schedule"])
                    offsets[int(match.group(1))] += 1
        self.assertEqual(sorted(offsets), list(range(period)))
        # 500 CronJobs over 5 start minutes: 100 starts in every minute, give or take one a cluster.
        self.assertLessEqual(max(offsets.values()) - min(offsets.values()), len(self.fleet.clusters))

    def test_a_client_targets_a_service_in_its_own_cluster(self):
        cluster = self.fleet.clusters[0]
        manifests = sf.cluster_manifests(cluster, self.shape)
        services = {f"{m['metadata']['name']}.{m['metadata']['namespace']}" for m in manifests if m["kind"] == "Service"}
        for m in manifests:
            if m["kind"] == "CronJob":
                script = m["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][-1]
                host = re.search(r"http://([^.]+\.[^.]+)\.svc", script).group(1)
                self.assertIn(host, services)

    def test_a_client_lives_inside_its_bounds(self):
        m = sf.client_manifest(0, 10, self.shape)
        script = m["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["command"][-1]
        low, high = self.shape["cronjob_pod_seconds_min"], self.shape["cronjob_pod_seconds_max"]
        self.assertIn(f"sleep $(( {low} + RANDOM % {high - low + 1} ))", script)
        self.assertGreater(m["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"], high)

    def test_every_container_is_small(self):
        for m in sf.cluster_manifests(self.fleet.clusters[0], self.shape):
            spec = m.get("spec", {})
            pod = spec.get("template", {}).get("spec") or spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec")
            for container in (pod or {}).get("containers", []):
                self.assertEqual(container["resources"]["requests"]["cpu"], self.shape["cpu_request"])

    def test_failing_pods_alternate_their_failure(self):
        a, b = sf.broken_manifest(0, self.shape), sf.broken_manifest(1, self.shape)
        self.assertEqual(a["spec"]["template"]["spec"]["containers"][0]["image"], self.shape["missing_image"])
        self.assertIn("exit 1", b["spec"]["template"]["spec"]["containers"][0]["command"][-1])


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.fleet = sf.fleet("S")

    def designed(self):
        return {c.name: sf.Observed(c.nodes, c.deployments, c.cronjobs, c.failing_pods) for c in self.fleet.clusters}

    def test_a_fleet_in_its_designed_state_is_sound(self):
        result = sf.check(self.fleet, self.designed(), self.fleet.churn_per_minute)
        self.assertTrue(result["sound"], result["findings"])

    def test_each_shortfall_is_named(self):
        observed = self.designed()
        first = self.fleet.clusters[0].name
        observed[first].ready_nodes -= 1
        observed[first].available_web -= 3
        del observed[self.fleet.clusters[1].name]
        result = sf.check(self.fleet, observed, None)
        self.assertFalse(result["sound"])
        text = " ".join(result["findings"])
        self.assertIn(f"{first}: 31 ready nodes, designed 32", text)
        self.assertIn("available servers", text)
        self.assertIn("not read", text)

    def test_churn_outside_the_tolerance_is_a_finding(self):
        designed = self.fleet.churn_per_minute
        self.assertTrue(sf.check(self.fleet, self.designed(), designed * 0.95)["sound"])
        result = sf.check(self.fleet, self.designed(), designed * 0.5)
        self.assertFalse(result["sound"])
        self.assertIn("churn", result["findings"][0])

    def test_more_failing_pods_than_designed_is_not_a_finding(self):
        observed = self.designed()
        observed[self.fleet.clusters[0].name].failing_pods += 2
        self.assertTrue(sf.check(self.fleet, observed, None)["sound"])


if __name__ == "__main__":
    unittest.main()
