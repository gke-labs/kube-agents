"""The Autopilot band helper reads a requests pair the way the chart does.

Three band tests (the operator's, the web console's and hindsight-api's) take their ratio from
`_autopilot.gib_per_vcpu`, which parses the two quantities through the footprint generator, so a
request re-spelt in `values.yaml`, `2Gi` for `2048Mi` or `1` for `1000m`, is the same number in
each test, and a string that is not a quantity raises instead of being sliced into a wrong one.

Run: python3 -m unittest discover -s tests -p 'test_autopilot.py' -v
"""

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _autopilot import (  # noqa: E402
    AUTOPILOT_MAX_GIB_PER_VCPU,
    AUTOPILOT_MIN_GIB_PER_VCPU,
    cpu_millis,
    gib_per_vcpu,
    gib_per_vcpu_in_band,
    memory_gib,
    requests_in_band,
)


class QuantityParsersTest(unittest.TestCase):
    def test_cpu_in_millicores_and_in_cores(self):
        self.assertEqual(cpu_millis("500m"), 500)
        self.assertEqual(cpu_millis("1"), 1000)
        self.assertEqual(cpu_millis("4"), 4000)
        self.assertEqual(cpu_millis("0.5"), 500)
        self.assertEqual(cpu_millis(2), 2000)

    def test_memory_in_every_spelling_the_chart_reads(self):
        self.assertEqual(memory_gib("256Mi"), 0.25)
        self.assertEqual(memory_gib("2048Mi"), 2.0)
        self.assertEqual(memory_gib("2Gi"), 2.0)
        self.assertEqual(memory_gib("1.5Gi"), 1.5)
        self.assertEqual(memory_gib(str(2 * 1024**3)), 2.0)
        self.assertAlmostEqual(memory_gib("2G"), 2 * 10**9 / 1024**3)

    def test_a_string_that_is_not_a_quantity_raises(self):
        for text in ("lots", "500mi", "2Gib"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                memory_gib(text)
        for text in ("lots", "500mi"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                cpu_millis(text)


class BandTest(unittest.TestCase):
    def test_the_ratio_is_the_same_in_every_spelling(self):
        # The pair the review walked: 2Gi beside 500m is 4 GiB per vCPU, not 2 MiB.
        self.assertEqual(gib_per_vcpu("500m", "2Gi"), 4.0)
        self.assertEqual(gib_per_vcpu("500m", "2048Mi"), 4.0)
        self.assertEqual(gib_per_vcpu("0.5", "2Gi"), 4.0)
        self.assertEqual(gib_per_vcpu("1", "4096Mi"), 4.0)

    def test_the_band_edges_are_inside_it(self):
        self.assertTrue(gib_per_vcpu_in_band(AUTOPILOT_MIN_GIB_PER_VCPU))
        self.assertTrue(gib_per_vcpu_in_band(AUTOPILOT_MAX_GIB_PER_VCPU))
        self.assertFalse(gib_per_vcpu_in_band(AUTOPILOT_MIN_GIB_PER_VCPU - 0.01))
        self.assertFalse(gib_per_vcpu_in_band(AUTOPILOT_MAX_GIB_PER_VCPU + 0.01))

    def test_requests_in_band_reads_the_pair(self):
        self.assertTrue(requests_in_band("100m", "128Mi"))
        self.assertTrue(requests_in_band("500m", "2Gi"))
        # 50m beside 128Mi is 2.5 GiB per vCPU and admitted; 50m beside 512Mi is 10 and is not.
        self.assertTrue(requests_in_band("50m", "128Mi"))
        self.assertFalse(requests_in_band("50m", "512Mi"))
        self.assertFalse(requests_in_band("1", "512Mi"))


if __name__ == "__main__":
    unittest.main()
