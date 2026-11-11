"""The sized fleet run's cost record: price picking from the catalog and the cost arithmetic."""

import unittest

from kube_agents_bench.scale import sized_records as sr


def sku(sku_id, description, usd, region="us-central1", usage="OnDemand"):
    units, nanos = int(usd), round((usd - int(usd)) * 1e9)
    return {
        "skuId": sku_id, "description": description, "serviceRegions": [region],
        "category": {"usageType": usage},
        "pricingInfo": [{"pricingExpression": {"tieredRates": [{"unitPrice": {"units": str(units), "nanos": nanos}}]}}],
    }


CATALOG = {
    sr.COMPUTE_SERVICE: [
        sku("p1", "N1 Predefined Instance Core running in Americas", 0.5, usage="Preemptible"),
        sku("c1", "N1 Predefined Instance Core running in Americas", 0.031611),
        sku("r1", "N1 Predefined Instance Ram running in Americas", 0.004237),
        sku("d1", "Storage PD Capacity", 0.04),
        sku("d2", "Storage PD Capacity", 0.09, region="europe-west1"),
    ],
    sr.GKE_SERVICE: [sku("g1", "Zonal Kubernetes Clusters", 0.1, region="global")],
}


class PricesTest(unittest.TestCase):
    def test_picks_on_demand_in_the_region(self):
        prices = sr.pick_prices(CATALOG)
        self.assertEqual(prices["n1_core_hour"]["sku"], "c1")
        self.assertAlmostEqual(prices["n1_core_hour"]["usd"], 0.031611)
        self.assertEqual(prices["pd_standard_gib_month"]["sku"], "d1")
        self.assertEqual(prices["zonal_cluster_hour"]["usd"], 0.1)

    def test_a_missing_sku_is_an_error_not_a_zero(self):
        with self.assertRaises(LookupError):
            sr.pick_prices({sr.COMPUTE_SERVICE: [], sr.GKE_SERVICE: []})


class CostTest(unittest.TestCase):
    def test_an_n1_standard_1_node_hour_and_a_cluster_hour(self):
        prices = {"prices": sr.pick_prices(CATALOG)}
        cost = sr.fleet_cost([sr.Instance("n1-standard-1", 30, 1.0)], [1.0], prices)
        lines = cost["lines"]
        self.assertAlmostEqual(lines["node_cores"]["usd"], 0.0316, places=4)
        self.assertAlmostEqual(lines["node_ram"]["usd"], round(3.75 * 0.004237, 4), places=4)
        self.assertAlmostEqual(lines["node_disks"]["usd"], round(30 * 0.04 / 730, 4), places=4)
        self.assertEqual(lines["cluster_fees"]["usd"], 0.1)
        self.assertEqual(cost["usd"], round(sum(line["usd"] for line in lines.values()), 2))

    def test_the_m_fleet_for_three_and_a_half_hours_is_near_the_design_estimate(self):
        prices = {"prices": sr.pick_prices(CATALOG)}
        nodes = [sr.Instance("n1-standard-1", 30, 3.5)] * 250
        cost = sr.fleet_cost(nodes, [3.5] * 8, prices)
        # The design: about $42 of nodes and $3 of cluster fees a run.
        self.assertAlmostEqual(cost["lines"]["node_cores"]["usd"] + cost["lines"]["node_ram"]["usd"], 41.6, delta=1)
        self.assertAlmostEqual(cost["lines"]["cluster_fees"]["usd"], 2.8, delta=0.01)

    def test_an_unknown_machine_type_is_refused(self):
        with self.assertRaises(ValueError):
            sr.fleet_cost([sr.Instance("e2-medium", 30, 1.0)], [], {"prices": sr.pick_prices(CATALOG)})


if __name__ == "__main__":
    unittest.main()


class RegionTest(unittest.TestCase):
    def test_skus_follow_the_region(self):
        europe = {key: (description, region) for key, _s, description, region in sr.skus("europe-west1")}
        self.assertEqual(europe["n1_core_hour"], ("N1 Predefined Instance Core running in EMEA", "europe-west1"))
        self.assertEqual(europe["zonal_cluster_hour"][1], "global")

    def test_an_unknown_region_is_an_error_not_a_wrong_price(self):
        with self.assertRaises(LookupError):
            sr.skus("mars-north1")


class RetryTest(unittest.TestCase):
    def test_a_body_cut_off_part_way_is_retried(self):
        import http.client
        from unittest import mock
        calls = []

        class Body:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                calls.append(1)
                if len(calls) == 1:
                    raise http.client.IncompleteRead(b"partial")
                return b'{"skus": []}'

        with mock.patch.object(sr, "_token", return_value="t"), \
                mock.patch.object(sr.urllib.request, "urlopen", return_value=Body()), \
                mock.patch.object(sr.time, "sleep"):
            self.assertEqual(sr._get("https://example.invalid", "p"), {"skus": []})
        self.assertEqual(len(calls), 2)
