#!/usr/bin/env python3
"""Tests for fleet_waste.py, the fleet-wide-cost-analysis collector."""

import inspect
import json
import shlex
import subprocess
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import fleet_waste as fw  # noqa: E402

NOW = datetime(2026, 8, 1, tzinfo=timezone.utc)


def run_of(rc: int, stdout: str = "", stderr: str = "") -> fw.Run:
    return fw.Run(["x"], rc, stdout, stderr, 0.01)


def dump_of(*items) -> dict:
    return {"items": list(items)}


def obj(kind, name, ns=None, **overrides):
    meta = {"name": name, "creationTimestamp": "2026-01-01T00:00:00Z", "labels": {}, "annotations": {}}
    if ns is not None:
        meta["namespace"] = ns
    doc = {"kind": kind, "metadata": meta, "spec": {}, "status": {}}
    for path, value in overrides.items():
        target = doc
        keys = path.split(".")
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = value
    return doc


class ParseCpuMemTest(unittest.TestCase):
    def test_millicores(self):
        self.assertEqual(fw.parse_cpu_cores("150m"), 0.15)

    def test_whole_cores(self):
        self.assertEqual(fw.parse_cpu_cores("2"), 2.0)

    def test_mebibytes(self):
        self.assertEqual(fw.parse_mem_mib("512Mi"), 512.0)

    def test_gibibytes(self):
        self.assertEqual(fw.parse_mem_mib("2Gi"), 2048.0)

    def test_kibibytes(self):
        self.assertAlmostEqual(fw.parse_mem_mib("2048Ki"), 2.0)

    def test_decimal_suffixes_are_powers_of_a_thousand(self):
        self.assertAlmostEqual(fw.parse_mem_mib("1G"), 1e9 / (1024 * 1024))
        self.assertAlmostEqual(fw.parse_mem_mib("500M"), 500e6 / (1024 * 1024))
        self.assertAlmostEqual(fw.parse_mem_mib("128k"), 128e3 / (1024 * 1024))

    def test_the_large_binary_suffixes_parse(self):
        self.assertEqual(fw.parse_mem_mib("1Pi"), 1024.0**3)

    def test_pv_capacity_in_decimal_units_counts_toward_the_size_floor(self):
        self.assertAlmostEqual(fw._gib("200G"), 200e9 / 1024**3)
        self.assertEqual(fw._gib("1Ti"), 1024.0)

    def test_bare_number_is_bytes_not_mebibytes(self):
        self.assertAlmostEqual(fw.parse_mem_mib(str(512 * 1024 * 1024)), 512.0)

    def test_unparseable_is_none(self):
        self.assertIsNone(fw.parse_cpu_cores("garbage"))
        self.assertIsNone(fw.parse_mem_mib("garbage"))


MIB = 1024 * 1024


def series_of(ns, pod, *values, key="doubleValue"):
    return {
        "resource": {"labels": {"namespace_name": ns, "pod_name": pod}},
        "points": [{"value": {key: v}} for v in values],
    }


class FakeResponse:
    def __init__(self, status_code, payload=None, text=""):
        self.status_code, self._payload, self.text = status_code, payload, text

    def json(self):
        return self._payload


class FakeSession:
    """Answers the two metric queries `fetch_usage_peaks` issues."""

    def __init__(self, cpu=(), mem=(), status=200, text="", raises=None):
        self.cpu, self.mem, self.status, self.text, self.raises = list(cpu), list(mem), status, text, raises
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        if self.raises:
            raise self.raises
        if self.status != 200:
            return FakeResponse(self.status, text=self.text)
        is_cpu = "cpu/core_usage_time" in params["filter"]
        return FakeResponse(200, {"timeSeries": self.cpu if is_cpu else self.mem})


def usage_session(*pods, **kwargs):
    """A `FakeSession` answering with one series per `(ns, pod, cores, mib)`.

    Collector tests need *some* usage data or `overrequest` reads as degraded,
    which is a different code path from the one they are exercising.
    """
    pods = pods or (("default", "idle-1", 0.01, 8.0),)
    return FakeSession(
        cpu=[series_of(ns, pod, cores) for ns, pod, cores, _ in pods],
        mem=[series_of(ns, pod, mib * MIB) for ns, pod, _, mib in pods],
        **kwargs,
    )


NO_USAGE = dict(cpu=[], mem=[])


def requests_in_label(label):
    """The query each `curl` in a Monitoring label sends, in `session.get`'s shape."""
    requests = []
    for curl in label.split(" && "):
        words = shlex.split(curl)
        params = {}
        for flag, value in zip(words, words[1:]):
            if flag in ("--data-urlencode", "-d"):
                key, _, item = value.partition("=")
                if key not in params:
                    params[key] = item
                else:
                    params[key] = [*(params[key] if isinstance(params[key], list) else [params[key]]), item]
        requests.append(params)
    return requests


def sent_without_paging(calls):
    """What the fake session saw, with list values as a label spells them and no page size."""
    return [{k: (v if not isinstance(v, list) or len(v) > 1 else v[0]) for k, v in call.items() if k != "pageSize"} for call in calls]


class MonitoringLabelTest(unittest.TestCase):
    """The label is published as the command that backs the check, so it has to
    be the requests the collector sent and has to survive `finish`'s redaction."""

    def test_each_label_is_the_requests_the_collector_sent(self):
        peaks = FakeSession(cpu=[series_of("d", "p", 0.1)], mem=[series_of("d", "p", MIB)])
        _, _, peaks_run = fw.fetch_usage_peaks("acme", "prod-usc1", session=peaks, now=NOW)
        means = FakeSession(mem=[series_of("d", "p", MIB)])
        _, _, means_run = fw.fetch_memory_means("acme", "prod-usc1", session=means, now=NOW)
        lb = FakeLbSession(ingress=[lb_series("rule-a", 1)])
        _, lb_run = fw.fetch_lb_traffic("acme", FetchLbTrafficTest.RULES, session=lb, now=NOW)
        for label, session in ((peaks_run.argv[0], peaks), (means_run.argv[0], means), (lb_run.argv[0], lb)):
            with self.subTest(label=label[:80]):
                self.assertEqual(requests_in_label(label), sent_without_paging(session.calls))

    def test_the_label_survives_redaction_and_passes_finish_s_command_check(self):
        import audit_report

        # The longest names GCP allows: a 30-character project, a 40-character cluster, the longest zone.
        project, cluster, zone = "p" * 30, "c" * 40, "northamerica-northeast1-a"
        labels = [
            fw.fetch_usage_peaks(project, cluster, location=zone, session=FakeSession(), now=NOW)[2].argv[0],
            fw.fetch_memory_means(project, cluster, location=zone, session=FakeSession(), now=NOW)[2].argv[0],
            fw.fetch_lb_traffic(project, FetchLbTrafficTest.RULES, session=FakeLbSession(), now=NOW)[1].argv[0],
        ]
        for label in labels:
            with self.subTest(label=label[:80]):
                self.assertEqual(audit_report.publishable_text(label), label)
                audit_report.validate_check_command(label, "scope.clusters[0]", "overrequest")


class FetchUsagePeaksTest(unittest.TestCase):
    def fetch(self, session, **kwargs):
        return fw.fetch_usage_peaks("acme", "prod-usc1", session=session, now=NOW, **kwargs)

    def test_cpu_and_memory_merge_into_one_peak_per_pod(self):
        session = FakeSession(
            cpu=[series_of("default", "api-1", 0.15)],
            mem=[series_of("default", "api-1", 256 * MIB)],
        )
        peaks, ok, result = self.fetch(session)
        self.assertTrue(ok)
        self.assertEqual(result.rc, 0)
        # Cores and MiB -- the units `check_overrequest` compares against
        # parsed `resources.requests`, not the API's cores and raw bytes.
        self.assertEqual(peaks[("default", "api-1")], (0.15, 256.0))

    def test_the_peak_is_the_max_not_the_last_or_the_mean(self):
        session = FakeSession(
            cpu=[series_of("default", "api-1", 0.1, 4.0, 0.2)],
            mem=[series_of("default", "api-1", MIB, 8 * MIB, 2 * MIB)],
        )
        peaks, _, _ = self.fetch(session)
        self.assertEqual(peaks[("default", "api-1")], (4.0, 8.0))

    def test_int64_values_are_read_as_well_as_double(self):
        # Monitoring returns memory as an integer type; a reader that only
        # understood doubleValue would see every pod using zero bytes.
        session = FakeSession(
            cpu=[series_of("default", "api-1", 0.5)],
            mem=[series_of("default", "api-1", str(512 * MIB), key="int64Value")],
        )
        peaks, _, _ = self.fetch(session)
        self.assertEqual(peaks[("default", "api-1")], (0.5, 512.0))

    def test_a_pod_in_one_metric_only_is_unmeasured_on_the_other(self):
        # Present, so its CPU is judged; `None` on memory, because a missing
        # series is no reading at all and zero would read as maximally idle.
        session = FakeSession(cpu=[series_of("default", "api-1", 0.3)], mem=[])
        peaks, ok, _ = self.fetch(session)
        self.assertTrue(ok)
        self.assertEqual(peaks[("default", "api-1")], (0.3, None))

    def test_an_empty_answer_is_unavailable_rather_than_zero_usage(self):
        # The one failure mode that turns this check into a fleet-wide false
        # positive. An empty result read as "every pod used nothing" flags
        # every workload on the cluster as pure waste, with a plausible-looking
        # peak of 0.00 vCPU behind it.
        peaks, ok, result = self.fetch(FakeSession(cpu=[], mem=[]))
        self.assertEqual(peaks, {})
        self.assertFalse(ok)
        self.assertIn("no time series", result.stderr)

    def test_an_api_error_is_unavailable_and_keeps_the_status(self):
        peaks, ok, result = self.fetch(FakeSession(status=403, text="caller lacks monitoring.timeSeries.list"))
        self.assertFalse(ok)
        self.assertEqual(peaks, {})
        self.assertEqual(result.rc, 403)
        self.assertIn("monitoring.timeSeries.list", result.stderr)

    def test_a_transport_exception_is_unavailable_not_a_crash(self):
        peaks, ok, result = self.fetch(FakeSession(raises=OSError("connection reset")))
        self.assertFalse(ok)
        self.assertEqual(result.rc, -1)
        self.assertIn("connection reset", result.stderr)

    def test_a_non_json_200_is_unavailable_not_a_crash(self):
        """A 200 whose body is not JSON raised out of `collect_cluster` and cost
        the cluster every object-state check, not only the metrics."""

        class HtmlResponse(FakeResponse):
            def json(self):
                raise json.JSONDecodeError("Expecting value", "<html>", 0)

        class HtmlSession(FakeSession):
            def get(self, url, params=None, timeout=None):
                return HtmlResponse(200, text="<html>")

        peaks, ok, result = self.fetch(HtmlSession())
        self.assertFalse(ok)
        self.assertEqual(peaks, {})
        self.assertEqual(result.rc, -1)
        self.assertIn(fw.NON_JSON_BODY, result.stderr)

    def test_no_session_degrades_instead_of_raising(self):
        # `collect_fleet` passes None when ADC could not be resolved. Every
        # object-state check still has to run.
        peaks, ok, result = self.fetch(None)
        self.assertFalse(ok)
        self.assertEqual(peaks, {})
        self.assertIn("ADC", result.stderr)

    def test_pagination_follows_the_next_page_token(self):
        pages = {
            "cpu": [
                {"timeSeries": [series_of("default", "api-1", 0.1)], "nextPageToken": "more"},
                {"timeSeries": [series_of("default", "api-2", 0.2)]},
            ],
            "mem": [{"timeSeries": [series_of("default", "api-1", MIB)]}],
        }
        seen = []

        class Paged:
            def get(self, url, params=None, timeout=None):
                seen.append(params.get("pageToken"))
                key = "cpu" if "cpu/core_usage_time" in params["filter"] else "mem"
                queue = pages[key]
                return FakeResponse(200, queue.pop(0))

        peaks, ok, _ = self.fetch(Paged())
        self.assertTrue(ok)
        self.assertEqual(sorted(peaks), [("default", "api-1"), ("default", "api-2")])
        self.assertEqual(seen, [None, "more", None])

    def test_the_query_asks_for_a_peak_per_pod_over_the_whole_window(self):
        # These four parameters are the whole method. Without the secondary
        # ALIGN_MAX the response is one point per alignment period and the
        # caller would have to reduce it itself; without REDUCE_SUM grouped by
        # namespace and pod the figures stay per-container and compare against
        # a pod's summed requests as if each container were the whole pod.
        session = FakeSession(cpu=[series_of("d", "p", 1.0)], mem=[series_of("d", "p", MIB)])
        self.fetch(session, window_hours=24)
        params = session.calls[0]
        self.assertEqual(params["secondaryAggregation.perSeriesAligner"], "ALIGN_MAX")
        self.assertEqual(params["secondaryAggregation.alignmentPeriod"], "86400s")
        self.assertEqual(params["aggregation.crossSeriesReducer"], "REDUCE_SUM")
        self.assertEqual(
            params["aggregation.groupByFields"],
            ["resource.labels.namespace_name", "resource.labels.pod_name"],
        )
        self.assertIn('resource.labels.cluster_name="prod-usc1"', params["filter"])
        self.assertEqual(params["interval.startTime"], "2026-07-31T00:00:00Z")
        self.assertEqual(params["interval.endTime"], "2026-08-01T00:00:00Z")

    def test_cpu_is_a_rate_and_memory_is_not(self):
        # `core_usage_time` is a cumulative counter in core-seconds: aligned
        # any way but ALIGN_RATE it reports seconds of CPU consumed since the
        # container started, which is not cores and grows without bound.
        session = FakeSession(cpu=[series_of("d", "p", 1.0)], mem=[series_of("d", "p", MIB)])
        self.fetch(session)
        aligners = {c["filter"].split('"')[1]: c["aggregation.perSeriesAligner"] for c in session.calls}
        self.assertEqual(aligners[fw.CPU_METRIC], "ALIGN_RATE")
        self.assertEqual(aligners[fw.MEM_METRIC], "ALIGN_MAX")

    def test_both_reads_are_filtered_to_the_clusters_location(self):
        # Two clusters named `prod-usc1` in one project, at different
        # locations, would otherwise have their pods' series summed together.
        session = FakeSession(cpu=[series_of("d", "p", 1.0)], mem=[series_of("d", "p", MIB)])
        _, _, result = self.fetch(session, location="us-central1")
        for params in session.calls:
            self.assertIn('resource.labels.location="us-central1"', params["filter"])
        self.assertIn('resource.labels.location="us-central1"', result.argv[0])

    def test_memory_excludes_page_cache_and_cpu_is_unfiltered(self):
        session = FakeSession(cpu=[series_of("d", "p", 1.0)], mem=[series_of("d", "p", MIB)])
        self.fetch(session)
        filters = {c["filter"].split('"')[1]: c["filter"] for c in session.calls}
        self.assertIn('memory_type="non-evictable"', filters[fw.MEM_METRIC])
        self.assertNotIn("memory_type", filters[fw.CPU_METRIC])


class FetchMemoryMeansTest(unittest.TestCase):
    def fetch(self, session, **kwargs):
        return fw.fetch_memory_means("acme", "prod-usc1", session=session, now=NOW, **kwargs)

    def test_the_secondary_aligner_is_the_mean_and_the_primary_is_not(self):
        """The one line that separates this from `fetch_usage_peaks`.

        Both collapse a week to one number per pod. The primary aligner buckets
        five-minute samples and must stay ALIGN_MAX so a pod's number is its
        real occupancy within each bucket; the secondary collapses those
        buckets across the window, and ALIGN_MEAN there is what makes the
        result sustained use rather than the high-water mark 3.1 reads.
        """
        session = FakeSession(mem=[series_of("default", "api-1", 512 * MIB)])
        self.fetch(session)
        self.assertEqual(len(session.calls), 1)
        params = session.calls[0]
        self.assertEqual(params["aggregation.perSeriesAligner"], "ALIGN_MAX")
        self.assertEqual(params["secondaryAggregation.perSeriesAligner"], "ALIGN_MEAN")
        self.assertEqual(params["filter"].split('"')[1], fw.MEM_METRIC)

    def test_cpu_is_never_queried(self):
        # Memory only, by design: one extra round trip per cluster, not two.
        # CPU is compressible, so a request under actual usage throttles rather
        # than evicts and there is no `underrequest` finding to raise.
        session = FakeSession(mem=[series_of("default", "api-1", MIB)])
        self.fetch(session)
        self.assertNotIn(fw.CPU_METRIC, " ".join(c["filter"] for c in session.calls))

    def test_bytes_become_mib_keyed_like_the_peaks(self):
        session = FakeSession(mem=[series_of("default", "api-1", 1536 * MIB)])
        means, ok, result = self.fetch(session)
        self.assertTrue(ok)
        self.assertEqual(result.rc, 0)
        self.assertEqual(means[("default", "api-1")], 1536.0)

    def test_an_empty_answer_is_unavailable_rather_than_zero_usage(self):
        # Same failure mode `fetch_usage_peaks` guards, inverted: read as "this
        # pod averaged nothing", an empty answer suppresses every finding
        # instead of inventing them, so the check goes quiet and looks healthy.
        means, ok, result = self.fetch(FakeSession(mem=[]))
        self.assertEqual(means, {})
        self.assertFalse(ok)
        self.assertIn("no time series", result.stderr)

    def test_an_api_error_is_unavailable_and_keeps_the_status(self):
        means, ok, result = self.fetch(FakeSession(status=403, text="caller lacks monitoring.timeSeries.list"))
        self.assertFalse(ok)
        self.assertEqual(means, {})
        self.assertEqual(result.rc, 403)

    def test_no_session_degrades_instead_of_raising(self):
        means, ok, result = self.fetch(None)
        self.assertFalse(ok)
        self.assertEqual(means, {})
        self.assertIn("ADC", result.stderr)

    def test_the_label_names_the_aligner_that_produced_the_number(self):
        """`adopt_collector_evidence` overwrites the model's command with this
        label, so it is the only description of the query a reviewer ever sees.
        ALIGN_MEAN in it is what distinguishes the finding's evidence from
        3.1's, which is otherwise the same metric over the same window."""
        _, _, result = self.fetch(FakeSession(mem=[series_of("d", "p", MIB)]))
        self.assertIn("ALIGN_MEAN", result.argv[0])
        self.assertIn(f"secondaryAggregation.alignmentPeriod={fw.USAGE_WINDOW_HOURS * 3600}s", result.argv[0])


def lb_series(rule, *values, resource="loadbalancing.googleapis.com/ExternalNetworkLoadBalancerRule", region="us-central1"):
    return {
        "resource": {"type": resource, "labels": {"project_id": "acme", "forwarding_rule_name": rule, "region": region}},
        "points": [{"value": {"int64Value": str(v)}} for v in values],
    }


class FakeLbSession:
    """Answers the three queries `fetch_lb_traffic` issues, by metric."""

    def __init__(self, *, ingress=(), egress_packets=(), egress_bytes=(), status=200, text="", raises=None):
        self.by_metric = {
            fw.LB_INGRESS_PACKETS_METRIC: list(ingress),
            fw.LB_EGRESS_PACKETS_METRIC: list(egress_packets),
            fw.LB_EGRESS_BYTES_METRIC: list(egress_bytes),
        }
        self.status, self.text, self.raises, self.calls = status, text, raises, []

    def get(self, url, params=None, timeout=None):
        self.calls.append(dict(params or {}))
        if self.raises:
            raise self.raises
        if self.status != 200:
            return FakeResponse(self.status, text=self.text)
        metric = params["filter"].split('"')[1]
        return FakeResponse(200, {"timeSeries": self.by_metric[metric]})


class FetchLbTrafficTest(unittest.TestCase):
    """§3.13's traffic read -- what the rule in front of an idle workload met."""

    RULES = [
        {"name": "rule-a", "IPAddress": "34.186.100.26", "loadBalancingScheme": "EXTERNAL", "region": "us-central1"},
        {"name": "rule-b", "IPAddress": "35.245.254.69", "loadBalancingScheme": "EXTERNAL", "region": "us-central1"},
    ]

    def fetch(self, session, rules=None, **kwargs):
        return fw.fetch_lb_traffic("acme", self.RULES if rules is None else rules, session=session, now=NOW, **kwargs)

    def test_a_rule_list_that_is_not_a_list_of_objects_reads_no_traffic(self):
        """The rule list gates §3.6 as a failed read; the traffic sentence
        drawn from the rules that did parse would be the only claim left
        standing on it, so it goes unwritten instead."""
        clusters = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])
        for stdout in ("{}", json.dumps([self.RULES[0], "rule-b"])):
            def run(argv, **kwargs):
                if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                    return run_of(0, clusters)
                if argv[:4] == ["gcloud", "compute", "forwarding-rules", "list"]:
                    return run_of(0, stdout)
                return run_of(0, "")

            with self.subTest(stdout=stdout[:20]), patch.object(fw, "fetch_lb_traffic") as fetch:
                *_, traffic = fw._read_project("acme", run=run, session=FakeLbSession(), now=NOW)
                self.assertIsNone(traffic)
                fetch.assert_not_called()

    def test_the_answer_is_keyed_by_address_not_by_rule_name(self):
        # The join on the other end is a Service's
        # `status.loadBalancer.ingress[].ip`, and nothing in the Monitoring
        # answer carries an address.
        traffic, result = self.fetch(FakeLbSession(ingress=[lb_series("rule-a", 100000)]))
        self.assertEqual(result.rc, 0)
        self.assertEqual(traffic["34.186.100.26"]["rule"], "rule-a")
        self.assertEqual(traffic["34.186.100.26"]["ingress_packets"], 100000)

    def test_points_within_a_series_are_summed(self):
        # DELTA counters aligned to a day: seven buckets that add up to the
        # window, not seven candidates for a maximum.
        traffic, _ = self.fetch(FakeLbSession(ingress=[lb_series("rule-a", 10, 20, 30)]))
        self.assertEqual(traffic["34.186.100.26"]["ingress_packets"], 60)

    def test_the_duplicate_resource_type_is_not_double_counted(self):
        """The trap that would have published 745,404 for a rule that saw 374,158.

        Monitoring reports the same L4 traffic under the legacy `tcp_lb_rule`
        resource and again under `ExternalNetworkLoadBalancerRule`, and
        `crossSeriesReducer` does not merge across resource types -- so
        grouping by the rule name still returns two near-equal series. The
        figures below are the live pair read off adamparco-kage.
        """
        traffic, _ = self.fetch(
            FakeLbSession(
                ingress=[
                    lb_series("rule-a", 374158),
                    lb_series("rule-a", 371246, resource="tcp_lb_rule"),
                ]
            )
        )
        self.assertEqual(traffic["34.186.100.26"]["ingress_packets"], 374158)

    def test_a_rule_with_no_series_is_present_with_no_figures(self):
        # Present, so the caller can say "unmeasured"; `None`, so it cannot
        # say "zero".
        traffic, _ = self.fetch(FakeLbSession(ingress=[lb_series("rule-a", 1)]))
        self.assertIn("35.245.254.69", traffic)
        self.assertIsNone(traffic["35.245.254.69"]["ingress_packets"])

    def test_a_series_with_no_rule_label_is_dropped(self):
        """The mis-grouped answer, which carries the project's whole traffic.

        `metric.labels.forwarding_rule_name` is a valid thing to ask for and
        returns exactly this: one unlabelled series holding every rule's
        packets. Attributing it to a rule would be a fabricated number an
        order of magnitude too large.
        """
        unlabelled = {"resource": {"type": "tcp_lb_rule", "labels": {"project_id": "acme"}}, "points": [{"value": {"int64Value": "743426"}}]}
        traffic, _ = self.fetch(FakeLbSession(ingress=[unlabelled, lb_series("rule-a", 374158)]))
        self.assertEqual(traffic["34.186.100.26"]["ingress_packets"], 374158)

    def test_same_named_rules_in_two_regions_keep_their_own_traffic(self):
        # A rule name is unique per region, and the list spans every region.
        rules = [
            {"name": "web", "IPAddress": "34.1.1.1", "loadBalancingScheme": "EXTERNAL", "region": "https://www.googleapis.com/compute/v1/projects/acme/regions/us-central1"},
            {"name": "web", "IPAddress": "34.2.2.2", "loadBalancingScheme": "EXTERNAL", "region": "https://www.googleapis.com/compute/v1/projects/acme/regions/europe-west1"},
        ]
        session = FakeLbSession(ingress=[lb_series("web", 900000, region="us-central1"), lb_series("web", 5, region="europe-west1")])
        traffic, _ = self.fetch(session, rules=rules)
        self.assertEqual(traffic["34.1.1.1"]["ingress_packets"], 900000)
        self.assertEqual(traffic["34.2.2.2"]["ingress_packets"], 5)
        self.assertIn(fw.LB_REGION_LABEL, session.calls[0]["aggregation.groupByFields"])

    def test_a_series_with_no_region_label_is_dropped(self):
        unregioned = {"resource": {"type": "tcp_lb_rule", "labels": {"project_id": "acme", "forwarding_rule_name": "rule-a"}}, "points": [{"value": {"int64Value": "5"}}]}
        traffic, _ = self.fetch(FakeLbSession(ingress=[unregioned]))
        self.assertIsNone(traffic["34.186.100.26"]["ingress_packets"])

    def test_only_external_rules_are_measured(self):
        rules = [
            {"name": "internal", "IPAddress": "10.150.0.78", "loadBalancingScheme": "INTERNAL"},
            {"name": "psc", "IPAddress": "10.150.0.60"},
            {"name": "rule-a", "IPAddress": "34.186.100.26", "loadBalancingScheme": "EXTERNAL", "region": "us-central1"},
        ]
        traffic, _ = self.fetch(FakeLbSession(ingress=[lb_series("rule-a", 5)]), rules=rules)
        self.assertEqual(sorted(traffic), ["34.186.100.26"])

    def test_rules_sharing_an_address_are_summed_not_overwritten(self):
        """A TCP and a UDP Service on one static IP: the quiet rule must not
        stand in for the busy one."""
        rules = [
            {"name": "rule-tcp", "IPAddress": "34.186.100.26", "loadBalancingScheme": "EXTERNAL", "region": "us-central1"},
            {"name": "rule-udp", "IPAddress": "34.186.100.26", "loadBalancingScheme": "EXTERNAL", "region": "us-central1"},
        ]
        traffic, _ = self.fetch(FakeLbSession(ingress=[lb_series("rule-tcp", 900000), lb_series("rule-udp", 5)]), rules=rules)
        self.assertEqual(traffic["34.186.100.26"]["ingress_packets"], 900005)
        self.assertEqual(traffic["34.186.100.26"]["rule"], "rule-tcp,rule-udp")

    def test_one_unmeasured_rule_leaves_the_shared_address_unknown(self):
        rules = [
            {"name": "rule-tcp", "IPAddress": "34.186.100.26", "loadBalancingScheme": "EXTERNAL", "region": "us-central1"},
            {"name": "rule-udp", "IPAddress": "34.186.100.26", "loadBalancingScheme": "EXTERNAL", "region": "us-central1"},
        ]
        traffic, _ = self.fetch(FakeLbSession(ingress=[lb_series("rule-udp", 5)]), rules=rules)
        self.assertIsNone(traffic["34.186.100.26"]["ingress_packets"])

    def test_a_project_with_no_external_rule_sends_nothing_and_returns_none(self):
        """No request is made, so there is no `Run` to record: one would reach
        the manifest as an rc-0 Monitoring read that never happened."""
        session = FakeLbSession()
        self.assertIsNone(self.fetch(session, rules=[]))
        self.assertEqual(session.calls, [])

    def test_no_external_rule_is_none_even_without_a_session(self):
        """The no-rule answer comes before the session check: without it a
        project with nothing to measure recorded an rc -1 traffic read on
        every cluster whenever the session could not be built."""
        self.assertIsNone(fw.fetch_lb_traffic("acme", [], session=None, now=NOW))

    def test_a_non_json_200_fails_the_read_rather_than_raising(self):
        """`ApiSession.get` returns a bare `requests.Response`; a 200 carrying
        an HTML page raised out of `_read_project` and cost the project every
        cluster, for a read whose only consumer is one excerpt sentence."""

        class HtmlResponse(FakeResponse):
            def json(self):
                raise json.JSONDecodeError("Expecting value", "<html>", 0)

        class HtmlSession(FakeLbSession):
            def get(self, url, params=None, timeout=None):
                return HtmlResponse(200, text="<html>")

        traffic, result = self.fetch(HtmlSession())
        self.assertEqual(traffic, {})
        self.assertEqual(result.rc, -1)
        self.assertIn(fw.NON_JSON_BODY, result.stderr)

    def test_an_api_error_keeps_its_status_and_measures_nothing(self):
        traffic, result = self.fetch(FakeLbSession(status=403, text="caller lacks monitoring.timeSeries.list"))
        self.assertEqual(traffic, {})
        self.assertEqual(result.rc, 403)
        self.assertIn("monitoring.timeSeries.list", result.stderr)

    def test_a_transport_exception_is_a_failed_read_not_a_crash(self):
        traffic, result = self.fetch(FakeLbSession(raises=OSError("connection reset")))
        self.assertEqual(traffic, {})
        self.assertEqual(result.rc, -1)

    def test_no_session_degrades_the_way_every_other_read_here_does(self):
        traffic, result = self.fetch(None)
        self.assertEqual(traffic, {})
        self.assertEqual(result.rc, -1)
        self.assertIn("ADC", result.stderr)

    def test_the_query_groups_by_the_resource_label(self):
        session = FakeLbSession(ingress=[lb_series("rule-a", 1)])
        self.fetch(session, window_hours=24)
        params = session.calls[0]
        self.assertEqual(params["aggregation.groupByFields"], ["resource.labels.forwarding_rule_name", "resource.labels.region"])
        self.assertEqual(params["aggregation.crossSeriesReducer"], "REDUCE_SUM")
        self.assertEqual(params["aggregation.perSeriesAligner"], "ALIGN_SUM")
        self.assertEqual(params["aggregation.alignmentPeriod"], "86400s")
        self.assertEqual(params["interval.startTime"], "2026-07-31T00:00:00Z")

    def test_all_three_metrics_are_read(self):
        session = FakeLbSession(ingress=[lb_series("rule-a", 1)])
        self.fetch(session)
        self.assertEqual(
            [call["filter"].split('"')[1] for call in session.calls],
            [fw.LB_INGRESS_PACKETS_METRIC, fw.LB_EGRESS_PACKETS_METRIC, fw.LB_EGRESS_BYTES_METRIC],
        )

    def test_pagination_follows_the_next_page_token(self):
        pages = [
            {"timeSeries": [lb_series("rule-a", 10)], "nextPageToken": "more"},
            {"timeSeries": [lb_series("rule-b", 20)]},
        ]
        seen = []

        class Paged:
            def get(self, url, params=None, timeout=None):
                seen.append(params.get("pageToken"))
                if fw.LB_INGRESS_PACKETS_METRIC not in params["filter"]:
                    return FakeResponse(200, {"timeSeries": []})
                return FakeResponse(200, pages.pop(0) if pages else {"timeSeries": []})

        traffic, _ = self.fetch(Paged())
        self.assertEqual(traffic["34.186.100.26"]["ingress_packets"], 10)
        self.assertEqual(traffic["35.245.254.69"]["ingress_packets"], 20)
        self.assertEqual(seen[:2], [None, "more"])

    def test_the_recorded_label_names_what_a_reader_would_re_run(self):
        _, result = self.fetch(FakeLbSession(ingress=[lb_series("rule-a", 1)]))
        label = result.argv[0]
        self.assertIn("projects/acme/timeSeries", label)
        self.assertIn(fw.LB_RULE_LABEL, label)
        self.assertIn(fw.LB_INGRESS_PACKETS_METRIC, label)
        start = NOW - timedelta(hours=fw.USAGE_WINDOW_HOURS)
        self.assertIn(f"interval.startTime={start.strftime(fw.MONITORING_TIME_FORMAT)}", label)
        self.assertIn(f"interval.endTime={NOW.strftime(fw.MONITORING_TIME_FORMAT)}", label)

    def test_the_digest_survives_an_unmeasured_rule(self):
        # The rendered stand-in holds `None` in three columns for `rule-b`. A
        # tuple sort that ever compared one against a number would raise, and
        # the whole project's traffic read would come back as a crash.
        _, result = self.fetch(FakeLbSession(ingress=[lb_series("rule-a", 1)]))
        rows = json.loads(result.stdout)
        self.assertEqual([row[0] for row in rows], ["34.186.100.26", "35.245.254.69"])
        self.assertIsNone(rows[1][2])


class OrphanPvTest(unittest.TestCase):
    def pv(self, phase, reclaim="Retain", **overrides):
        defaults = {"spec.persistentVolumeReclaimPolicy": reclaim, "status.phase": phase, "spec.capacity": {"storage": "10Gi"}}
        return obj("PersistentVolume", "pv-1", **{**defaults, **overrides})

    def context(self, pvs, pvcs=None, sts=None):
        return {"pvs": pvs, "pvcs": pvcs or [], "statefulsets": sts or []}

    def test_flags_released_over_7_days(self):
        pv = self.pv("Released", **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z"})
        hits = fw.check_orphan_pv(self.context([pv]), now=NOW)
        self.assertEqual(len(hits), 1)

    def test_does_not_flag_released_under_7_days(self):
        pv = self.pv("Released", **{"status.lastPhaseTransitionTime": "2026-07-30T00:00:00Z"})
        self.assertEqual(fw.check_orphan_pv(self.context([pv]), now=NOW), [])

    def test_an_addon_manager_label_excludes_the_pv(self):
        """addon-manager stamps its mode as a label; the guard read only
        annotations, so an addon's Retain volume was flagged."""
        released = {"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z"}
        for where in ("labels", "annotations"):
            with self.subTest(where=where):
                pv = self.pv("Released", **released, **{f"metadata.{where}": {fw.ADDON_MANAGER_KEY: "Reconcile"}})
                self.assertEqual(fw.check_orphan_pv(self.context([pv]), now=NOW), [])

    def test_delete_policy_is_never_flagged(self):
        pv = self.pv("Released", reclaim="Delete", **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z"})
        self.assertEqual(fw.check_orphan_pv(self.context([pv]), now=NOW), [])

    def test_falls_back_to_object_age_when_transition_time_absent(self):
        pv = self.pv("Failed")  # creationTimestamp is 2026-01-01, > 7 days before NOW
        hits = fw.check_orphan_pv(self.context([pv]), now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertIn("lastPhaseTransitionTime absent", hits[0]["excerpt"])

    def test_available_unclaimed_over_30_days_is_flagged(self):
        pv = self.pv("Available")  # created 2026-01-01, unclaimed
        self.assertEqual(len(fw.check_orphan_pv(self.context([pv]), now=NOW)), 1)

    def test_available_with_a_pending_claim_for_its_class_is_pre_staged(self):
        pv = self.pv("Available", **{"spec.storageClassName": "manual"})
        waiting = obj("PersistentVolumeClaim", "data", ns="default", **{"spec.storageClassName": "manual", "status.phase": "Pending"})
        bound = obj("PersistentVolumeClaim", "other", ns="default", **{"spec.storageClassName": "manual", "status.phase": "Bound"})
        self.assertEqual(fw.check_orphan_pv(self.context([pv], pvcs=[waiting]), now=NOW), [])
        self.assertEqual(len(fw.check_orphan_pv(self.context([pv], pvcs=[bound]), now=NOW)), 1)

    def test_a_pending_claim_for_another_class_does_not_excuse_it(self):
        pv = self.pv("Available", **{"spec.storageClassName": "manual"})
        waiting = obj("PersistentVolumeClaim", "data", ns="default", **{"spec.storageClassName": "standard-rwo", "status.phase": "Pending"})
        self.assertEqual(len(fw.check_orphan_pv(self.context([pv], pvcs=[waiting]), now=NOW)), 1)

    def test_available_with_claim_ref_is_not_flagged(self):
        pv = self.pv("Available", **{"spec.claimRef": {"namespace": "default", "name": "x"}})
        self.assertEqual(fw.check_orphan_pv(self.context([pv]), now=NOW), [])

    def test_claim_ref_naming_a_live_pvc_is_suppressed(self):
        pv = self.pv("Released", **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z", "spec.claimRef": {"namespace": "default", "name": "data"}})
        pvc = obj("PersistentVolumeClaim", "data", ns="default")
        self.assertEqual(fw.check_orphan_pv(self.context([pv], pvcs=[pvc]), now=NOW), [])

    def test_a_claim_recreated_under_the_same_name_leaves_the_old_pv_orphaned(self):
        pv = self.pv("Released", **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z", "spec.claimRef": {"namespace": "default", "name": "data-mydb-0", "uid": "old"}})
        pvc = obj("PersistentVolumeClaim", "data-mydb-0", ns="default", **{"metadata.uid": "new"})
        sts = obj("StatefulSet", "mydb", ns="default")
        self.assertEqual(len(fw.check_orphan_pv(self.context([pv], pvcs=[pvc], sts=[sts]), now=NOW)), 1)
        pvc["metadata"]["uid"] = "old"
        self.assertEqual(fw.check_orphan_pv(self.context([pv], pvcs=[pvc], sts=[sts]), now=NOW), [])

    def test_the_age_fallback_takes_the_longer_floor(self):
        pv = self.pv("Released", **{"metadata.creationTimestamp": "2026-07-20T00:00:00Z"})
        self.assertEqual(fw.check_orphan_pv(self.context([pv]), now=NOW), [])

    def test_scaled_to_zero_statefulset_claim_is_suppressed(self):
        pv = self.pv(
            "Released",
            **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z", "spec.claimRef": {"namespace": "default", "name": "data-mydb-0"}},
        )
        sts = obj("StatefulSet", "mydb", ns="default")
        self.assertEqual(fw.check_orphan_pv(self.context([pv], sts=[sts]), now=NOW), [])

    def test_a_same_named_statefulset_in_another_namespace_does_not_suppress_it(self):
        pv = self.pv(
            "Released",
            **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z", "spec.claimRef": {"namespace": "default", "name": "data-mydb-0"}},
        )
        sts = obj("StatefulSet", "mydb", ns="other")
        self.assertEqual(len(fw.check_orphan_pv(self.context([pv], sts=[sts]), now=NOW)), 1)

    def test_backup_annotated_pv_is_suppressed(self):
        pv = self.pv("Released", **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z"})
        pv["metadata"]["annotations"]["velero.io/backup-name"] = "nightly"
        self.assertEqual(fw.check_orphan_pv(self.context([pv]), now=NOW), [])

    def test_large_disk_is_major(self):
        pv = self.pv("Released", **{"status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z", "spec.capacity": {"storage": "500Gi"}})
        self.assertEqual(fw.check_orphan_pv(self.context([pv]), now=NOW)[0]["severity"], "major")

    def test_a_part_day_over_the_gate_reports_the_gate_not_the_day_after(self):
        # 30d 14h. The gate is 30, so this is the shape that matters: rounding
        # to nearest printed "31d" and handed the model a number a day past a
        # threshold it is entitled to quote back in a title.
        pv = self.pv("Available", **{"metadata.creationTimestamp": "2026-07-01T10:00:00Z"})
        self.assertIn("AGE=30d", fw.check_orphan_pv(self.context([pv]), now=NOW)[0]["excerpt"])


class UnconsumedPvcTest(unittest.TestCase):
    def pvc(self, name="data", ns="default", phase="Bound", created="2026-01-01T00:00:00Z", capacity="10Gi"):
        return obj("PersistentVolumeClaim", name, ns=ns, **{"status.phase": phase, "status.capacity": {"storage": capacity}}, **{"metadata.creationTimestamp": created})

    def test_flags_bound_unreferenced_over_14_days(self):
        context = {"pods": [], "pvcs": [self.pvc()], "statefulsets": []}
        self.assertEqual(len(fw.check_unconsumed_pvc(context, now=NOW)), 1)

    def test_does_not_flag_referenced_by_a_pod(self):
        pod = obj("Pod", "p", ns="default", **{"spec.volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]})
        context = {"pods": [pod], "pvcs": [self.pvc()], "statefulsets": []}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_does_not_flag_a_claim_a_suspended_cronjob_will_mount(self):
        cronjob = obj("CronJob", "nightly", ns="default", **{"spec.suspend": True, "spec.jobTemplate": {"spec": {"template": {"spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]}}}}})
        context = {"pods": [], "pvcs": [self.pvc()], "statefulsets": [], "cronjobs": [cronjob]}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_does_not_flag_a_claim_an_unstarted_job_will_mount(self):
        job = obj("Job", "backfill", ns="default", **{"spec.template": {"spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]}}})
        context = {"pods": [], "pvcs": [self.pvc()], "statefulsets": [], "jobs": [job]}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_does_not_flag_a_generic_ephemeral_volume_claim(self):
        """An `ephemeral` volume names no claim; its PVC is `<pod>-<volume>`."""
        pod = obj("Pod", "web-0", ns="default", **{"spec.volumes": [{"name": "scratch", "ephemeral": {"volumeClaimTemplate": {}}}]})
        context = {"pods": [pod], "pvcs": [self.pvc(name="web-0-scratch")], "statefulsets": []}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_an_ephemeral_volume_on_another_pod_does_not_spare_the_claim(self):
        pod = obj("Pod", "web-1", ns="default", **{"spec.volumes": [{"name": "scratch", "ephemeral": {"volumeClaimTemplate": {}}}]})
        context = {"pods": [pod], "pvcs": [self.pvc(name="web-0-scratch")], "statefulsets": []}
        self.assertEqual(len(fw.check_unconsumed_pvc(context, now=NOW)), 1)

    def test_does_not_flag_a_claim_a_pod_owns(self):
        """Garbage collection deletes it with the pod, whether or not the pod
        list caught the pod."""
        pvc = self.pvc(name="web-0-scratch")
        pvc["metadata"]["ownerReferences"] = [{"kind": "Pod", "name": "web-0"}]
        context = {"pods": [], "pvcs": [pvc], "statefulsets": []}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_a_job_in_another_namespace_does_not_spare_the_claim(self):
        job = obj("Job", "backfill", ns="other", **{"spec.template": {"spec": {"volumes": [{"persistentVolumeClaim": {"claimName": "data"}}]}}})
        context = {"pods": [], "pvcs": [self.pvc()], "statefulsets": [], "jobs": [job]}
        self.assertEqual(len(fw.check_unconsumed_pvc(context, now=NOW)), 1)

    def test_does_not_flag_under_14_days(self):
        context = {"pods": [], "pvcs": [self.pvc(created="2026-07-25T00:00:00Z")], "statefulsets": []}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_does_not_flag_scaled_to_zero_statefulset_claim(self):
        sts = obj("StatefulSet", "mydb", ns="default")
        context = {"pods": [], "pvcs": [self.pvc(name="data-mydb-0")], "statefulsets": [sts]}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_does_not_flag_system_namespace(self):
        context = {"pods": [], "pvcs": [self.pvc(ns="kube-system")], "statefulsets": []}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_does_not_flag_unbound(self):
        context = {"pods": [], "pvcs": [self.pvc(phase="Pending")], "statefulsets": []}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW), [])

    def test_premium_rwo_is_an_ssd_class(self):
        """GKE's SSD-backed class is `premium-rwo`, which names no `ssd`."""
        pvc = self.pvc()
        pvc["spec"]["storageClassName"] = "premium-rwo"
        context = {"pods": [], "pvcs": [pvc], "statefulsets": []}
        self.assertEqual(fw.check_unconsumed_pvc(context, now=NOW)[0]["severity"], "major")

    def test_a_part_day_over_the_gate_reports_the_gate_not_the_day_after(self):
        # 14d 14h, against a 14-day gate. See the matching case in OrphanPvTest.
        context = {"pods": [], "pvcs": [self.pvc(created="2026-07-17T10:00:00Z")], "statefulsets": []}
        self.assertIn("AGE=14d", fw.check_unconsumed_pvc(context, now=NOW)[0]["excerpt"])


class IdleNodepoolTest(unittest.TestCase):
    def node(self, name, pool, cpu_alloc="4", mem_alloc="8Gi", unschedulable=False):
        return obj(
            "Node", name,
            **{
                "metadata.labels": {"cloud.google.com/gke-nodepool": pool},
                "status.allocatable": {"cpu": cpu_alloc, "memory": mem_alloc},
                "spec.unschedulable": unschedulable,
                "metadata.creationTimestamp": "2026-01-01T00:00:00Z",
            },
        )

    def pod_on(self, node, cpu_req="0", mem_req="0Mi", daemonset=False, phase="Running",
               ns="default", name=None):
        owners = [{"kind": "DaemonSet", "name": "ds"}] if daemonset else []
        return obj(
            "Pod", name or f"p-{node}", ns=ns,
            **{
                "spec.nodeName": node,
                "spec.containers": [{"resources": {"requests": {"cpu": cpu_req, "memory": mem_req}}}],
                "metadata.ownerReferences": owners,
                "status.phase": phase,
            },
        )

    def pool(self, name, min_nodes=1, autoscaling_enabled=True, machine_type="e2-standard-8",
             accelerators=None, taints=None):
        return {
            "name": name,
            "autoscaling": {"enabled": autoscaling_enabled, "minNodeCount": min_nodes},
            "config": {"machineType": machine_type, "accelerators": accelerators or [],
                       "taints": taints or []},
        }

    @staticmethod
    def controlled(pod):
        """A ReplicaSet owner, as a real add-on carries: `_drain_blockers`
        lists a bare pod in a system namespace as a blocker of its own."""
        pod["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "rs"}]
        return pod

    def small_node_with_addons(self, node="n1", pool="default-pool"):
        """The `spot-capacity-test` shape measured on the fleet 2026-09-05: a
        lone e2-small whose GKE add-ons alone book 61% CPU / 39% memory."""
        return (
            self.node(node, pool, cpu_alloc="940m", mem_alloc="1372Mi"),
            [
                self.controlled(self.pod_on(node, cpu_req="270m", mem_req="155Mi", ns="kube-system", name="kube-dns")),
                self.controlled(self.pod_on(node, cpu_req="105m", mem_req="130Mi", ns="gke-managed-cim", name="ksm")),
                self.controlled(self.pod_on(node, cpu_req="202m", mem_req="247Mi", ns="kube-system", name="rest")),
            ],
        )

    def test_flags_idle_pool_with_nonzero_floor(self):
        nodes = [self.node("n1", "idle-pool")]
        pods = [self.pod_on("n1", cpu_req="200m", mem_req="200Mi")]  # well under 15% of 4 vCPU/8Gi
        context = {"nodes": nodes, "pods": pods}
        pools = [self.pool("idle-pool"), self.pool("other-pool")]
        hits = fw.check_idle_nodepool(context, pools, now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "NodePool/idle-pool")

    def test_does_not_flag_the_only_pool_in_the_cluster(self):
        nodes = [self.node("n1", "only-pool")]
        context = {"nodes": nodes, "pods": []}
        pools = [self.pool("only-pool")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])

    def test_does_not_flag_pool_at_min_zero(self):
        nodes = [self.node("n1", "burst")]
        context = {"nodes": nodes, "pods": []}
        pools = [self.pool("burst", min_nodes=0), self.pool("other")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])

    def test_daemonset_pods_are_excluded_from_the_allocation_math(self):
        nodes = [self.node("n1", "pool")]
        pods = [self.pod_on("n1", cpu_req="3", mem_req="6Gi", daemonset=True)]
        context = {"nodes": nodes, "pods": pods}
        pools = [self.pool("pool"), self.pool("other")]
        # DaemonSet-only allocation still reads as idle -- 90% of a node
        # would look "used" if the filter did not exclude it.
        self.assertEqual(len(fw.check_idle_nodepool(context, pools, now=NOW)), 1)

    def test_does_not_flag_a_well_utilized_pool(self):
        nodes = [self.node("n1", "pool")]
        pods = [self.pod_on("n1", cpu_req="3", mem_req="6Gi")]
        context = {"nodes": nodes, "pods": pods}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])

    def test_accelerator_pool_is_major_even_with_few_nodes(self):
        nodes = [self.node("n1", "gpu-pool")]
        context = {"nodes": nodes, "pods": []}
        pools = [self.pool("gpu-pool", machine_type="a2-highgpu-1g", accelerators=[{"acceleratorType": "nvidia-tesla-a100"}]), self.pool("other")]
        hits = fw.check_idle_nodepool(context, pools, now=NOW)
        self.assertEqual(hits[0]["severity"], "major")

    def test_an_extended_memory_custom_pool_is_major(self):
        """`n2-custom-8-65536-ext` is an 8-vCPU machine; an unparsed `-ext`
        suffix used to leave it unsized and grade the idle pool `minor`."""
        nodes = [self.node("n1", "pool")]
        context = {"nodes": nodes, "pods": []}
        pools = [self.pool("pool", machine_type="n2-custom-8-65536-ext"), self.pool("other")]
        hits = fw.check_idle_nodepool(context, pools, now=NOW)
        self.assertEqual(hits[0]["severity"], "major")

    def test_small_machine_few_nodes_is_minor(self):
        nodes = [self.node("n1", "pool", cpu_alloc="2", mem_alloc="4Gi")]
        context = {"nodes": nodes, "pods": []}
        pools = [self.pool("pool", machine_type="e2-small"), self.pool("other")]
        hits = fw.check_idle_nodepool(context, pools, now=NOW)
        self.assertEqual(hits[0]["severity"], "minor")

    def test_a_tpu_pool_is_major_without_an_accelerators_list(self):
        """A TPU node pool carries its topology, not `config.accelerators`, so
        `has_accelerator` is False there and the machine type is the only
        signal. §3.7: an idle accelerator pool with a non-zero floor is the
        largest reclaimable item this audit can find."""
        nodes = [self.node("n1", "tpu-pool")]
        context = {"nodes": nodes, "pods": []}
        for machine_type in ("ct5lp-hightpu-4t", "ct6e-standard-4t", "tpu7x-standard-4t"):
            with self.subTest(machine_type=machine_type):
                pools = [self.pool("tpu-pool", machine_type=machine_type), self.pool("other")]
                hits = fw.check_idle_nodepool(context, pools, now=NOW)
                self.assertEqual(hits[0]["severity"], "major")

    def test_one_busy_node_stops_the_pool_being_called_idle(self):
        """§3.7 flags when *every* node in the pool is under 15%, not when the
        pool averages under 15%. Nine empty nodes and one full one average 10%,
        and the full one is exactly what stops the pool shrinking."""
        nodes = [self.node(f"n{i}", "pool") for i in range(10)]
        pods = [self.pod_on("n0", cpu_req="3800m", mem_req="7Gi")]
        context = {"nodes": nodes, "pods": pods}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])

    def test_a_pool_where_every_node_is_idle_is_still_flagged(self):
        nodes = [self.node(f"n{i}", "pool") for i in range(10)]
        pods = [self.pod_on(f"n{i}", cpu_req="100m", mem_req="100Mi") for i in range(10)]
        context = {"nodes": nodes, "pods": pods}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertEqual(len(fw.check_idle_nodepool(context, pools, now=NOW)), 1)

    def test_a_node_reporting_no_allocatable_is_not_evidence_of_idleness(self):
        nodes = [self.node("n1", "pool"), self.node("n2", "pool", cpu_alloc="0", mem_alloc="0")]
        context = {"nodes": nodes, "pods": []}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])

    def test_pool_age_comes_from_the_oldest_node_not_the_first_listed(self):
        """`kubectl get nodes` comes back name-sorted, so `nodes[0]` was the
        alphabetically first node. A months-old pool that autoscaled up a node
        named `a-...` yesterday exempted itself from the whole check."""
        old = self.node("z-old", "pool")  # creationTimestamp 2026-01-01
        new = self.node("a-new", "pool")
        new["metadata"]["creationTimestamp"] = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        context = {"nodes": [new, old], "pods": []}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertEqual(len(fw.check_idle_nodepool(context, pools, now=NOW)), 1)

    def test_a_genuinely_new_pool_is_still_skipped(self):
        new = self.node("n1", "pool")
        new["metadata"]["creationTimestamp"] = (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        context = {"nodes": [new], "pods": []}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])

    def upgraded_pool_context(self):
        """A pool whose every node a surge upgrade recreated two days ago."""
        n = self.node("n1", "pool")
        n["metadata"]["creationTimestamp"] = (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"nodes": [n], "pods": []}, [self.pool("pool"), self.pool("other")]

    def test_a_months_old_pool_is_flagged_after_a_node_upgrade(self):
        """Node age is not pool age. Every auto-upgrade recreates the nodes, so
        read off the nodes a months-old idle pool went unflagged for a week
        after each one."""
        context, pools = self.upgraded_pool_context()
        hits = fw.check_idle_nodepool(context, pools, now=NOW, pool_ages={})
        self.assertEqual([h["object"] for h in hits], ["NodePool/pool"])

    def test_a_young_cluster_s_first_pool_is_dated_from_the_cluster(self):
        """A cluster's first pool arrives in `CREATE_CLUSTER`, so it has no
        `CREATE_NODE_POOL` operation; on a three-day-old cluster it is three days
        old, not older than the threshold."""
        context, pools = self.upgraded_pool_context()
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW, pool_ages={}, cluster_age=3.0), [])

    def test_an_old_cluster_s_first_pool_is_judged(self):
        context, pools = self.upgraded_pool_context()
        hits = fw.check_idle_nodepool(context, pools, now=NOW, pool_ages={}, cluster_age=200.0)
        self.assertEqual([h["object"] for h in hits], ["NodePool/pool"])

    def test_a_pool_created_under_a_week_ago_is_skipped_on_its_operation(self):
        context, pools = self.upgraded_pool_context()
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW, pool_ages={"pool": 2.0}), [])

    def test_a_failed_operations_read_falls_back_to_node_age_and_says_so(self):
        context, pools = self.upgraded_pool_context()
        limitations: list[str] = []
        hits = fw.check_idle_nodepool(context, pools, now=NOW, pool_ages=None, limitations=limitations)
        self.assertEqual(hits, [])
        self.assertEqual(len(limitations), 1)
        # The pool is named `pool`, so a bare "pool" matched the sentence's own
        # wording; this pins the name in the slot it fills.
        self.assertIn(f"skipped pool pool as under {fw.IDLE_NODEPOOL_MIN_AGE_DAYS} days", limitations[0])
        self.assertIn("operations read failed", limitations[0])

    def test_creation_ages_take_the_newest_create_of_this_cluster_s_pools(self):
        def op(pool, days, cluster="c1", kind="CREATE_NODE_POOL"):
            start = (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")
            return {
                "operationType": kind,
                "targetLink": f"https://container.googleapis.com/v1/projects/p/locations/l/clusters/{cluster}/nodePools/{pool}",
                "startTime": start,
            }

        ops = [op("pool", 90), op("pool", 3), op("other", 1, cluster="c2"), op("gone", 1, kind="DELETE_NODE_POOL")]
        ages = fw.node_pool_creation_ages(ops, "c1", now=NOW)
        self.assertEqual(set(ages), {"pool"})
        self.assertAlmostEqual(ages["pool"], 3.0, places=3)

    def test_system_addons_alone_do_not_make_a_pool_look_busy(self):
        """§3.7 puts the 15% bar "below the point where DaemonSet and system
        overhead (typically 10-25% of a small node) dominates". Measured, GKE's
        non-DaemonSet add-ons come to ~0.57 vCPU per node, so on an e2-small
        they are 61% on their own and the bar is unreachable. On the fleet of
        2026-09-05 that silenced the check on every one of twelve Standard
        pools -- including this shape, a lone untainted e2-small holding no
        workload at all next to a pool with 1.5 vCPU free."""
        node, addons = self.small_node_with_addons()
        context = {"nodes": [node], "pods": addons}
        pools = [self.pool("default-pool", machine_type="e2-small"), self.pool("other")]
        hits = fw.check_idle_nodepool(context, pools, now=NOW)
        self.assertEqual([h["object"] for h in hits], ["NodePool/default-pool"])
        self.assertIn("no workload pods at all", hits[0]["excerpt"])

    def test_one_real_workload_still_keeps_the_pool(self):
        """The system-pod exclusion must not swallow the busy case: a pool
        carrying an actual workload above the bar is in use, whatever the
        add-ons around it add up to."""
        node, addons = self.small_node_with_addons()
        pods = addons + [self.pod_on("n1", cpu_req="500m", mem_req="100Mi", name="app")]
        context = {"nodes": [node], "pods": pods}
        pools = [self.pool("default-pool", machine_type="e2-small"), self.pool("other")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])

    def test_a_workload_under_the_bar_is_flagged_and_quantified(self):
        """Under 15% of allocatable is still an under-allocated pool, and the
        excerpt has to say so rather than claiming the pool is empty."""
        node, addons = self.small_node_with_addons()
        pods = addons + [self.pod_on("n1", cpu_req="50m", mem_req="10Mi", name="tiny")]
        context = {"nodes": [node], "pods": pods}
        pools = [self.pool("default-pool", machine_type="e2-small"), self.pool("other")]
        hits = fw.check_idle_nodepool(context, pools, now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertIn("1 workload pod(s) requesting 5% CPU / 1% memory", hits[0]["excerpt"])

    def test_a_native_sidecar_counts_toward_the_node_request(self):
        """A `restartPolicy: Always` init container runs beside the app for the
        pod's life and the scheduler reserves its request. Summing app
        containers alone read this node as 3% requested and idle."""
        node = self.node("n1", "default-pool")
        pod = self.pod_on("n1", cpu_req="100m", mem_req="100Mi", name="app")
        pod["spec"]["initContainers"] = [
            {"restartPolicy": "Always", "resources": {"requests": {"cpu": "1500m", "memory": "2Gi"}}},
        ]
        context = {"nodes": [node], "pods": [pod]}
        pools = [self.pool("default-pool"), self.pool("other")]
        self.assertEqual(fw.check_idle_nodepool(context, pools, now=NOW), [])

    def test_the_effective_request_follows_the_scheduler_formula(self):
        """Per resource: app containers plus every sidecar, or a plain init
        container plus the sidecars started before it, whichever is larger. A
        plain init container runs to completion first, so it is not added."""
        pod = self.pod_on("n1", cpu_req="100m", mem_req="100Mi")
        pod["spec"]["initContainers"] = [
            {"resources": {"requests": {"cpu": "200m", "memory": "50Mi"}}},
            {"restartPolicy": "Always", "resources": {"requests": {"cpu": "300m", "memory": "10Mi"}}},
            {"resources": {"requests": {"cpu": "2", "memory": "20Mi"}}},
            {"restartPolicy": "Always", "resources": {"requests": {"cpu": "400m", "memory": "30Mi"}}},
        ]
        cpu, mem = fw._sum_requests([pod])
        # cpu: the second plain init (2) plus the sidecar before it (0.3)
        # outweighs app + both sidecars (0.8).
        self.assertAlmostEqual(cpu, 2.3)
        # memory: app + both sidecars (140Mi) outweighs either plain init.
        self.assertAlmostEqual(mem, 140.0)

    def test_the_excerpt_keeps_the_autoscaler_facing_figure(self):
        """The add-ons are excluded from the *gate*, not from the reader: the
        cluster autoscaler weighs them when it decides whether to drain, so the
        all-non-DaemonSet number stays in the evidence."""
        node, addons = self.small_node_with_addons()
        context = {"nodes": [node], "pods": addons}
        pools = [self.pool("default-pool", machine_type="e2-small"), self.pool("other")]
        excerpt = fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"]
        self.assertIn("non-DS CPU is 61% / mem 39% of allocatable (0.58 vCPU / 0.5 GiB)", excerpt)

    def test_the_excerpt_reports_headroom_on_the_other_pools(self):
        """Everything on the pool has to land somewhere before a node drains,
        add-ons included. Without the headroom figure the reader has to go and
        work out for themselves whether kube-dns has anywhere to go."""
        node, addons = self.small_node_with_addons()
        busy = self.node("n2", "other", cpu_alloc="4", mem_alloc="8Gi")
        pods = addons + [self.pod_on("n2", cpu_req="2", mem_req="4Gi", name="busy")]
        context = {"nodes": [node, busy], "pods": pods}
        pools = [self.pool("default-pool", machine_type="e2-small"), self.pool("other")]
        hits = fw.check_idle_nodepool(context, pools, now=NOW)
        self.assertEqual([h["object"] for h in hits], ["NodePool/default-pool"])
        self.assertIn("other pools have 2.00 vCPU / 4.0 GiB unrequested on uncordoned nodes to absorb it",
                      hits[0]["excerpt"])
        self.assertIn("before taints, selectors and zonal spread", hits[0]["excerpt"])

    def test_the_headroom_subtracts_daemonsets_already_on_the_absorbing_nodes(self):
        """The logging and metrics agents on `n2` have booked their share of it
        already; a drain will not find that room. DaemonSets leave the pool's
        own 15% test, not the other nodes' arithmetic."""
        node, addons = self.small_node_with_addons()
        busy = self.node("n2", "other", cpu_alloc="4", mem_alloc="8Gi")
        pods = addons + [
            self.pod_on("n2", cpu_req="2", mem_req="4Gi", name="busy"),
            self.pod_on("n2", cpu_req="500m", mem_req="1Gi", daemonset=True, name="fluentbit"),
        ]
        context = {"nodes": [node, busy], "pods": pods}
        pools = [self.pool("default-pool", machine_type="e2-small"), self.pool("other")]
        hits = fw.check_idle_nodepool(context, pools, now=NOW)
        self.assertIn("other pools have 1.50 vCPU / 3.0 GiB unrequested on uncordoned nodes to absorb it",
                      hits[0]["excerpt"])

    def test_two_idle_pools_do_not_count_each_other_as_room(self):
        """Each excerpt counted the other idle pool's free capacity, so a
        reader acting on both deleted the room each was told would absorb it."""
        node_a, addons = self.small_node_with_addons("na", "pool-a")
        node_b = self.node("nb", "pool-b", cpu_alloc="4", mem_alloc="8Gi")
        busy = self.node("n2", "other", cpu_alloc="4", mem_alloc="8Gi")
        pods = addons + [self.pod_on("n2", cpu_req="2", mem_req="4Gi", name="busy")]
        context = {"nodes": [node_a, node_b, busy], "pods": pods}
        pools = [self.pool("pool-a", machine_type="e2-small"), self.pool("pool-b"), self.pool("other")]
        hits = {h["object"]: h["excerpt"] for h in fw.check_idle_nodepool(context, pools, now=NOW)}
        self.assertEqual(set(hits), {"NodePool/pool-a", "NodePool/pool-b"})
        for this, other in (("NodePool/pool-a", "NodePool/pool-b"), ("NodePool/pool-b", "NodePool/pool-a")):
            with self.subTest(pool=this):
                self.assertIn(
                    f"other pools have 2.00 vCPU / 4.0 GiB unrequested on uncordoned nodes and not on {other}, "
                    f"which this run also finds idle, to absorb it",
                    hits[this],
                )

    def test_the_headroom_leaves_out_cordoned_nodes(self):
        """A cordoned node accepts no pod, so its free capacity is not room
        for the drain: a surge upgrade that cordons `n3` must not count it."""
        node, addons = self.small_node_with_addons()
        busy = self.node("n2", "other", cpu_alloc="4", mem_alloc="8Gi")
        cordoned = self.node("n3", "other", cpu_alloc="4", mem_alloc="8Gi")
        cordoned["spec"] = {**(cordoned.get("spec") or {}), "unschedulable": True}
        pods = addons + [self.pod_on("n2", cpu_req="2", mem_req="4Gi", name="busy")]
        context = {"nodes": [node, busy, cordoned], "pods": pods}
        pools = [self.pool("default-pool", machine_type="e2-small"), self.pool("other")]
        hits = fw.check_idle_nodepool(context, pools, now=NOW)
        self.assertIn("other pools have 2.00 vCPU / 4.0 GiB unrequested on uncordoned nodes to absorb it",
                      hits[0]["excerpt"])

    def test_taints_are_surfaced_so_a_dedicated_pool_can_be_dismissed(self):
        """A pool tainted for add-ons is empty of workloads by design. That is
        a judgement for triage, not a suppression here -- §3.7 refuses to
        suppress tainted pools outright because a stranded accelerator pool is
        the largest item this audit can find."""
        node, addons = self.small_node_with_addons()
        context = {"nodes": [node], "pods": addons}
        taints = [{"key": "dedicated", "value": "system", "effect": "NO_SCHEDULE"}]
        pools = [self.pool("default-pool", machine_type="e2-small", taints=taints),
                 self.pool("other")]
        excerpt = fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"]
        self.assertIn("Nodes are tainted dedicated=system:NO_SCHEDULE", excerpt)
        self.assertIn("dedicated on purpose", excerpt)

    def test_drain_blockers_are_named_because_lowering_the_floor_would_do_nothing(self):
        """The autoscaler skips a node holding a PDB-less `kube-system` pod or
        an unannotated local-storage pod, and GKE does not let you turn either
        rule off. Without this the finding reads as actionable while
        `--min-nodes=0` sits there reclaiming nothing."""
        node, addons = self.small_node_with_addons()
        context = {"nodes": [node], "pods": addons, "pdbs": []}
        pools = [self.pool("default-pool", machine_type="e2-small"), self.pool("other")]
        excerpt = fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"]
        self.assertIn("Draining will not happen on its own: 2 pod(s)", excerpt)
        self.assertIn("kube-system/kube-dns (kube-system, no PDB)", excerpt)
        self.assertIn("deleting the pool is the remediation that works", excerpt)

    def test_a_pdb_backed_kube_system_pod_is_not_a_blocker(self):
        node, addons = self.small_node_with_addons()
        for pod in addons:
            pod["metadata"]["labels"] = {"k8s-app": "kube-dns"}
        context = {"nodes": [node], "pods": addons,
                   "pdbs": [{"metadata": {"namespace": "kube-system"}, "spec": {"selector": {"matchLabels": {"k8s-app": "kube-dns"}}}}]}
        pools = [self.pool("default-pool", machine_type="e2-small"), self.pool("other")]
        excerpt = fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"]
        self.assertNotIn("Draining will not happen", excerpt)

    def test_system_namespace_pods_the_autoscaler_will_not_evict_are_blockers(self):
        """§3.8 skips `SYSTEM_NS`, so a bare pod or a `safe-to-evict: "false"`
        one there reaches no finding; the note is the only place it is named,
        and a PDB clears neither rule. Outside `SYSTEM_NS` §3.8 reports both,
        so the note leaves them out rather than count them twice."""
        node, addons = self.small_node_with_addons()
        dns, ksm, rest = addons
        dns["metadata"]["labels"] = {"k8s-app": "kube-dns"}
        dns["metadata"]["annotations"] = {fw.SAFE_TO_EVICT_ANNOTATION: fw.SAFE_TO_EVICT_FALSE}
        ksm["metadata"]["ownerReferences"] = []
        rest["metadata"]["annotations"] = {fw.SAFE_TO_EVICT_ANNOTATION: fw.SAFE_TO_EVICT_TRUE}
        workload = self.pod_on("n1", ns="default", name="bare-app")
        workload["metadata"]["annotations"] = {fw.SAFE_TO_EVICT_ANNOTATION: fw.SAFE_TO_EVICT_FALSE}
        context = {"nodes": [node], "pods": addons + [workload],
                   "pdbs": [{"metadata": {"namespace": "kube-system"}, "spec": {"selector": {"matchLabels": {"k8s-app": "kube-dns"}}}}]}
        pools = [self.pool("default-pool", machine_type="e2-small"), self.pool("other")]
        excerpt = fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"]
        self.assertIn("Draining will not happen on its own: 2 pod(s)", excerpt)
        self.assertIn('kube-system/kube-dns (system namespace, safe-to-evict "false")', excerpt)
        self.assertIn("gke-managed-cim/ksm (system namespace, no controller)", excerpt)
        self.assertNotIn("bare-app", excerpt)

    def test_a_pdb_with_no_selector_covers_no_pod(self):
        """In `policy/v1` an omitted selector selects nothing and `{}` selects
        everything; read as `{}`, a selector-less PDB hid every blocker."""
        node, addons = self.small_node_with_addons()
        context = {"nodes": [node], "pods": addons, "pdbs": [{"metadata": {"namespace": "kube-system"}, "spec": {}}]}
        pools = [self.pool("default-pool", machine_type="e2-small"), self.pool("other")]
        excerpt = fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"]
        self.assertIn("Draining will not happen on its own: 2 pod(s)", excerpt)
        self.assertEqual(fw._pdb_selectors({"pdbs": [{"metadata": {"namespace": "a"}, "spec": {"selector": {}}}]}), [("a", {})])

    def test_a_pdb_in_another_namespace_covers_nothing_here(self):
        pdb = ("default", {"matchLabels": {"k8s-app": "kube-dns"}})
        self.assertFalse(fw._selector_matches(pdb, "kube-system", {"k8s-app": "kube-dns"}))
        self.assertTrue(fw._selector_matches(pdb, "default", {"k8s-app": "kube-dns"}))

    def test_match_expressions_are_honoured(self):
        pdb = ("app", {"matchExpressions": [{"key": "tier", "operator": "In", "values": ["db"]}]})
        self.assertTrue(fw._selector_matches(pdb, "app", {"tier": "db"}))
        self.assertFalse(fw._selector_matches(pdb, "app", {"tier": "web"}))
        self.assertFalse(fw._selector_matches(pdb, "app", {}))

    def test_a_mirror_pod_is_not_a_blocker(self):
        """kube-proxy is owned by the Node, not a DaemonSet, so the DaemonSet
        filter does not reach it -- but it is static and goes with the node."""
        node = self.node("n1", "pool")
        mirror = self.pod_on("n1", ns="kube-system", name="kube-proxy-n1")
        mirror["metadata"]["ownerReferences"] = [{"kind": "Node", "name": "n1"}]
        mirror["spec"]["volumes"] = [{"hostPath": {"path": "/var/lib"}}]
        context = {"nodes": [node], "pods": [mirror], "pdbs": []}
        pools = [self.pool("pool"), self.pool("other")]
        excerpt = fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"]
        self.assertNotIn("Draining will not happen", excerpt)

    def test_local_storage_outside_kube_system_still_blocks(self):
        node = self.node("n1", "pool")
        pod = self.controlled(self.pod_on("n1", cpu_req="10m", ns="gmp-system", name="gmp-op"))
        pod["spec"]["volumes"] = [{"emptyDir": {}}]
        context = {"nodes": [node], "pods": [pod], "pdbs": []}
        pools = [self.pool("pool"), self.pool("other")]
        excerpt = fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"]
        self.assertIn("gmp-system/gmp-op (local storage, not safe-to-evict)", excerpt)

    def test_a_safe_to_evict_local_storage_pod_is_not_a_blocker(self):
        node = self.node("n1", "pool")
        pod = self.controlled(self.pod_on("n1", cpu_req="10m", ns="gmp-system", name="gmp-op"))
        pod["spec"]["volumes"] = [{"emptyDir": {}}]
        pod["metadata"]["annotations"] = {fw.SAFE_TO_EVICT_ANNOTATION: "true"}
        context = {"nodes": [node], "pods": [pod], "pdbs": []}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertNotIn("Draining will not happen",
                         fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"])

    def test_a_safe_to_evict_kube_system_pod_without_a_pdb_is_not_a_blocker(self):
        node = self.node("n1", "pool")
        pod = self.pod_on("n1", cpu_req="10m", ns="kube-system", name="metrics-server")
        pod["metadata"]["annotations"] = {fw.SAFE_TO_EVICT_ANNOTATION: "true"}
        context = {"nodes": [node], "pods": [pod], "pdbs": []}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertNotIn("Draining will not happen",
                         fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"])

    def test_local_volumes_all_listed_as_safe_are_not_a_blocker(self):
        node = self.node("n1", "pool")
        pod = self.controlled(self.pod_on("n1", cpu_req="10m", ns="gmp-system", name="gmp-op"))
        pod["spec"]["volumes"] = [{"name": "cache", "emptyDir": {}}, {"name": "logs", "hostPath": {"path": "/l"}}]
        context = {"nodes": [node], "pods": [pod], "pdbs": []}
        pools = [self.pool("pool"), self.pool("other")]
        # Untrimmed, as the autoscaler compares: " logs" is not `logs`.
        for listed, blocks in (("cache,logs", False), ("cache, logs", True), ("cache", True)):
            with self.subTest(listed=listed):
                pod["metadata"]["annotations"] = {fw.SAFE_TO_EVICT_LOCAL_VOLUMES_ANNOTATION: listed}
                excerpt = fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"]
                self.assertEqual("Draining will not happen" in excerpt, blocks)

    def test_a_memory_backed_empty_dir_is_not_local_storage(self):
        node = self.node("n1", "pool")
        pod = self.controlled(self.pod_on("n1", cpu_req="10m", ns="gmp-system", name="gmp-op"))
        pod["spec"]["volumes"] = [{"name": "dshm", "emptyDir": {"medium": "Memory"}}]
        context = {"nodes": [node], "pods": [pod], "pdbs": []}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertNotIn("Draining will not happen",
                         fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"])

    def test_an_empty_pool_gets_no_blocker_note(self):
        context = {"nodes": [self.node("n1", "pool")], "pods": [], "pdbs": []}
        pools = [self.pool("pool"), self.pool("other")]
        self.assertNotIn("Draining will not happen",
                         fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"])

    def test_a_context_without_pdbs_does_not_crash_the_check(self):
        """Every existing caller builds the full dump, but the check reads
        `pdbs` only for this note and must not start requiring it."""
        node, addons = self.small_node_with_addons()
        context = {"nodes": [node], "pods": addons}
        pools = [self.pool("default-pool", machine_type="e2-small"), self.pool("other")]
        self.assertEqual(len(fw.check_idle_nodepool(context, pools, now=NOW)), 1)

    def test_a_fixed_size_pool_says_so_rather_than_min_none(self):
        """`min=None` reads as missing data; it means the pool has no
        autoscaler, and §3.7's remediation then has to create one rather than
        lower a floor. The live `default-pool` on `spot-capacity-test` is
        exactly this shape."""
        node, addons = self.small_node_with_addons()
        context = {"nodes": [node], "pods": addons}
        pools = [self.pool("default-pool", machine_type="e2-small", autoscaling_enabled=False),
                 self.pool("other")]
        excerpt = fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"]
        self.assertIn("autoscaling disabled, so the node count is a fixed floor", excerpt)
        self.assertNotIn("min=None", excerpt)

    def test_an_autoscaled_pool_still_reports_its_floor(self):
        node, addons = self.small_node_with_addons()
        context = {"nodes": [node], "pods": addons}
        pools = [self.pool("default-pool", machine_type="e2-small", min_nodes=2), self.pool("other")]
        self.assertIn("min=2", fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"])

    def test_an_untainted_pool_gets_no_taint_note(self):
        node, addons = self.small_node_with_addons()
        context = {"nodes": [node], "pods": addons}
        pools = [self.pool("default-pool", machine_type="e2-small"), self.pool("other")]
        self.assertNotIn("tainted", fw.check_idle_nodepool(context, pools, now=NOW)[0]["excerpt"])


class MachineTypeVcpusTest(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(fw._machine_type_vcpus("e2-standard-8"), 8)

    def test_highmem(self):
        self.assertEqual(fw._machine_type_vcpus("n2-highmem-16"), 16)

    def test_custom(self):
        self.assertEqual(fw._machine_type_vcpus("custom-4-16384"), 4)

    def test_extended_memory_custom(self):
        self.assertEqual(fw._machine_type_vcpus("n2-custom-8-65536-ext"), 8)

    def test_shared_core_custom_names_no_vcpu_count(self):
        self.assertIsNone(fw._machine_type_vcpus("e2-custom-medium-4096"))

    def test_hypermem(self):
        # M2's hypermem shapes are the largest non-accelerator machines, so
        # an idle one-node pool of them is §3.7's `major`, not `minor`.
        self.assertEqual(fw._machine_type_vcpus("m2-hypermem-416"), 416)
        self.assertTrue(fw._is_big_machine("m2-hypermem-208"))

    def test_small_is_unmatched(self):
        self.assertIsNone(fw._machine_type_vcpus("e2-small"))

    def test_a_local_ssd_variant_still_parses(self):
        self.assertEqual(fw._machine_type_vcpus("c3-standard-8-lssd"), 8)

    def test_an_accelerator_type_is_not_read_as_a_vcpu_count(self):
        """`a3-highgpu-8g`'s `8` counts GPUs, not vCPUs -- it is a 208-vCPU
        machine. Answering 8 would be luck; answering None and letting
        `_is_big_machine` decide is the honest split."""
        for machine_type in ("a2-highgpu-1g", "a2-ultragpu-8g", "a3-megagpu-8g", "ct5lp-hightpu-4t"):
            with self.subTest(machine_type=machine_type):
                self.assertIsNone(fw._machine_type_vcpus(machine_type))


class IsBigMachineTest(unittest.TestCase):
    def test_eight_vcpus_is_the_line(self):
        self.assertTrue(fw._is_big_machine("e2-standard-8"))
        self.assertFalse(fw._is_big_machine("e2-standard-4"))

    def test_every_accelerator_family_is_big(self):
        for machine_type in ("a2-highgpu-1g", "a2-ultragpu-8g", "a3-megagpu-8g", "a3-highgpu-8g", "ct5lp-hightpu-4t", "ct6e-standard-1t", "ct6e-standard-4t", "tpu7x-standard-4t"):
            with self.subTest(machine_type=machine_type):
                self.assertTrue(fw._is_big_machine(machine_type))

    def test_an_unparseable_type_is_not_assumed_big(self):
        self.assertFalse(fw._is_big_machine("e2-micro"))
        self.assertFalse(fw._is_big_machine("e2-standard-4"))
        self.assertFalse(fw._is_big_machine(""))


class ScaledownBlockedTest(unittest.TestCase):
    def test_bare_pod_with_local_storage_is_critical(self):
        pod = obj("Pod", "debug", ns="ci", **{"spec.nodeName": "n1", "spec.volumes": [{"emptyDir": {}}], "metadata.ownerReferences": []})
        context = {"pods": [pod], "pdbs": []}
        hits = fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}])
        self.assertEqual(hits[0]["severity"], "critical")

    def test_safe_to_evict_false_on_owned_pod_is_major(self):
        """§3.8 keeps `critical` for a pod nothing will ever reschedule. A
        controller recreates this one once it is deleted, so it is `major`."""
        pod = obj(
            "Pod", "app", ns="default",
            **{"spec.nodeName": "n1", "metadata.ownerReferences": [{"kind": "ReplicaSet", "name": "x"}], "metadata.annotations": {"cluster-autoscaler.kubernetes.io/safe-to-evict": "false"}},
        )
        context = {"pods": [pod], "pdbs": []}
        hits = fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}])
        self.assertEqual(hits[0]["severity"], "major")

    def test_safe_to_evict_false_on_bare_pod_is_critical(self):
        pod = obj(
            "Pod", "app", ns="default",
            **{"spec.nodeName": "n1", "metadata.ownerReferences": [], "metadata.annotations": {fw.SAFE_TO_EVICT_ANNOTATION: "false"}},
        )
        hits = fw.check_scaledown_blocked({"pods": [pod], "pdbs": []}, [{"_node_names": {"n1"}}])
        self.assertEqual(hits[0]["severity"], "critical")

    def test_bare_pod_without_local_storage_is_a_major_blocker(self):
        """§3.8 names a bare pod as a blocker on its own: the autoscaler will
        not evict a pod no controller would recreate."""
        pod = obj("Pod", "debug", ns="ci", **{"spec.nodeName": "n1", "metadata.ownerReferences": []})
        hits = fw.check_scaledown_blocked({"pods": [pod], "pdbs": []}, [{"_node_names": {"n1"}}])
        self.assertEqual([h["severity"] for h in hits], ["major"])

    def test_a_bare_pod_marked_safe_to_evict_is_not_a_blocker(self):
        pod = obj("Pod", "debug", ns="ci", **{"spec.nodeName": "n1", "metadata.ownerReferences": [], "metadata.annotations": {fw.SAFE_TO_EVICT_ANNOTATION: "true"}})
        self.assertEqual(fw.check_scaledown_blocked({"pods": [pod], "pdbs": []}, [{"_node_names": {"n1"}}]), [])

    def test_local_volumes_listed_as_safe_do_not_pin_an_owned_pod(self):
        """The autoscaler's per-volume form, as the §3.7 drain note reads it:
        an owned pod whose every local volume is listed is evictable."""
        for listed, expected in (("cache,logs", []), ("cache", ["major"])):
            with self.subTest(listed=listed):
                pod = obj(
                    "Pod", "app", ns="default",
                    **{"spec.nodeName": "n1", "metadata.ownerReferences": [{"kind": "ReplicaSet", "name": "x"}],
                       "metadata.annotations": {fw.SAFE_TO_EVICT_LOCAL_VOLUMES_ANNOTATION: listed},
                       "spec.volumes": [{"name": "cache", "emptyDir": {}}, {"name": "logs", "hostPath": {"path": "/l"}}]},
                )
                hits = fw.check_scaledown_blocked({"pods": [pod], "pdbs": []}, [{"_node_names": {"n1"}}])
                self.assertEqual([h["severity"] for h in hits], expected)

    def test_a_memory_backed_empty_dir_does_not_pin_an_owned_pod(self):
        pod = obj(
            "Pod", "trainer", ns="default",
            **{"spec.nodeName": "n1", "metadata.ownerReferences": [{"kind": "ReplicaSet", "name": "x"}],
               "spec.volumes": [{"name": "dshm", "emptyDir": {"medium": "Memory"}}]},
        )
        self.assertEqual(fw.check_scaledown_blocked({"pods": [pod], "pdbs": []}, [{"_node_names": {"n1"}}]), [])

    def test_the_excerpt_reports_the_raw_local_storage_evidence(self):
        """`local-storage=` is the shape on the object, as it always was, and
        the per-volume annotation sits beside it verbatim."""
        pod = obj(
            "Pod", "debug", ns="ci",
            **{"spec.nodeName": "n1", "metadata.ownerReferences": [],
               "metadata.annotations": {fw.SAFE_TO_EVICT_LOCAL_VOLUMES_ANNOTATION: "cache"},
               "spec.volumes": [{"name": "cache", "emptyDir": {}}]},
        )
        excerpt = fw.check_scaledown_blocked({"pods": [pod], "pdbs": []}, [{"_node_names": {"n1"}}])[0]["excerpt"]
        self.assertIn("local-storage=True", excerpt)
        self.assertIn("safe-to-evict-local-volumes=cache", excerpt)

    def test_a_finished_bare_pod_is_not_a_blocker(self):
        """The autoscaler ignores a pod that has already exited."""
        for phase in ("Succeeded", "Failed"):
            with self.subTest(phase=phase):
                pod = obj("Pod", "debug", ns="ci", **{"spec.nodeName": "n1", "metadata.ownerReferences": [], "status.phase": phase, "spec.volumes": [{"emptyDir": {}}]})
                self.assertEqual(fw.check_scaledown_blocked({"pods": [pod], "pdbs": []}, [{"_node_names": {"n1"}}]), [])

    def test_daemonset_and_mirror_pods_go_with_the_node(self):
        for kind in ("DaemonSet", "Node"):
            with self.subTest(kind=kind):
                pod = obj("Pod", "agent", ns="monitoring", **{"spec.nodeName": "n1", "metadata.ownerReferences": [{"kind": kind, "name": "x"}], "spec.volumes": [{"hostPath": {"path": "/var/log"}}]})
                self.assertEqual(fw.check_scaledown_blocked({"pods": [pod], "pdbs": []}, [{"_node_names": {"n1"}}]), [])

    def test_daemonset_and_mirror_pods_marked_not_safe_to_evict_still_go_with_the_node(self):
        # The autoscaler skips both before it reads the annotation.
        for kind in ("DaemonSet", "Node"):
            with self.subTest(kind=kind):
                pod = obj("Pod", "agent", ns="monitoring", **{"spec.nodeName": "n1", "metadata.ownerReferences": [{"kind": kind, "name": "x"}], "metadata.annotations": {fw.SAFE_TO_EVICT_ANNOTATION: "false"}})
                self.assertEqual(fw.check_scaledown_blocked({"pods": [pod], "pdbs": []}, [{"_node_names": {"n1"}}]), [])

    def test_the_node_carries_its_worst_blocker_whatever_the_listing_order(self):
        owned = obj("Pod", "app", ns="default", **{"spec.nodeName": "n1", "metadata.ownerReferences": [{"kind": "ReplicaSet", "name": "x"}], "metadata.annotations": {fw.SAFE_TO_EVICT_ANNOTATION: "false"}})
        bare = obj("Pod", "debug", ns="ci", **{"spec.nodeName": "n1", "metadata.ownerReferences": [], "spec.volumes": [{"emptyDir": {}}]})
        for pods in ([owned, bare], [bare, owned]):
            with self.subTest(first=pods[0]["metadata"]["name"]):
                hits = fw.check_scaledown_blocked({"pods": pods, "pdbs": []}, [{"_node_names": {"n1"}}])
                self.assertEqual([(h["severity"], "ci/debug" in h["excerpt"]) for h in hits], [("critical", True)])

    def test_a_pdb_alone_is_not_flagged_here(self):
        # obtainability-audit's 3.3/3.4 own the PDB; an owned pod with no
        # other blocker is evictable as far as §3.8 is concerned.
        pod = obj("Pod", "app", ns="default", **{"spec.nodeName": "n1", "metadata.labels": {"app": "web"}, "metadata.ownerReferences": [{"kind": "ReplicaSet", "name": "x"}]})
        pdb = obj("PodDisruptionBudget", "pdb1", ns="default", **{"spec.selector": {"matchLabels": {"app": "web"}}})
        context = {"pods": [pod], "pdbs": [pdb]}
        self.assertEqual(fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}]), [])

    def test_a_pdb_does_not_hide_a_blocker_that_is_not_the_pdb(self):
        """§3.8 withholds the finding only where the PDB is the only blocker.
        A bare PDB-selected pod with local storage still pins the node for good
        once the PDB is fixed. The check reads no PDB at all, so which selector
        the PDB carries cannot matter and is not varied here."""
        pod = obj("Pod", "app", ns="default", **{"spec.nodeName": "n1", "metadata.labels": {"app": "web"}, "metadata.ownerReferences": [], "spec.volumes": [{"emptyDir": {}}]})
        pdb = obj("PodDisruptionBudget", "pdb1", ns="default", **{"spec.selector": {"matchLabels": {"app": "web"}}})
        [hit] = fw.check_scaledown_blocked({"pods": [pod], "pdbs": [pdb]}, [{"_node_names": {"n1"}}])
        self.assertEqual(hit["severity"], "critical")

    def test_no_idle_pool_hits_means_nothing_to_check(self):
        self.assertEqual(fw.check_scaledown_blocked({"pods": [], "pdbs": []}, []), [])

    def test_ordinary_evictable_pod_is_not_flagged(self):
        pod = obj("Pod", "app", ns="default", **{"spec.nodeName": "n1", "metadata.ownerReferences": [{"kind": "ReplicaSet", "name": "x"}]})
        context = {"pods": [pod], "pdbs": []}
        self.assertEqual(fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}]), [])

    def evict_pod(self, value, node="n1"):
        return obj(
            "Pod", "app", ns="default",
            **{
                "spec.nodeName": node,
                "metadata.ownerReferences": [{"kind": "ReplicaSet", "name": "x"}],
                "metadata.annotations": {fw.SAFE_TO_EVICT_ANNOTATION: value},
            },
        )

    def test_only_the_exact_false_pins_the_node(self):
        """The autoscaler compares the annotation against `"false"` exactly, so
        any other spelling leaves the pod evictable and the node drainable.
        Reporting `"False"` as a pin published a blocker the autoscaler
        ignores."""
        context = {"pods": [self.evict_pod("false")], "pdbs": []}
        hits = fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")
        for value in ("False", "FALSE", "0", "f", " false "):
            with self.subTest(value=value):
                context = {"pods": [self.evict_pod(value)], "pdbs": []}
                self.assertEqual(fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}]), [])

    def test_only_the_exact_true_clears_the_local_storage_pin(self):
        """The mirror image: `"True"` is not `"true"` to the autoscaler, so a
        local-storage pod annotated that way still pins its node."""
        for value, expected in (("true", 0), ("True", 1), ("TRUE", 1), ("1", 1)):
            with self.subTest(value=value):
                pod = self.evict_pod(value)
                pod["spec"]["volumes"] = [{"emptyDir": {}}]
                context = {"pods": [pod], "pdbs": []}
                self.assertEqual(len(fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}])), expected)

    def test_an_unparseable_value_is_treated_as_unset(self):
        pod = self.evict_pod("maybe")
        context = {"pods": [pod], "pdbs": []}
        self.assertEqual(fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}]), [])

    def test_the_excerpt_quotes_the_raw_annotation(self):
        context = {"pods": [self.evict_pod("false")], "pdbs": []}
        hits = fw.check_scaledown_blocked(context, [{"_node_names": {"n1"}}])
        self.assertIn("safe-to-evict=false", hits[0]["excerpt"])


class OwnerKeyTest(unittest.TestCase):
    def test_no_owners_is_none(self):
        self.assertIsNone(fw._owner_key([]))

    def test_the_controller_wins_wherever_it_sits_in_the_list(self):
        owners = [
            {"kind": "ThingBinding", "name": "zzz"},
            {"kind": "ReplicaSet", "name": "web-abc", "controller": True},
        ]
        self.assertEqual(fw._owner_key(owners), ("ReplicaSet", "web-abc"))
        self.assertEqual(fw._owner_key(list(reversed(owners))), ("ReplicaSet", "web-abc"))

    def test_with_no_controller_the_answer_is_still_order_independent(self):
        owners = [{"kind": "B", "name": "b"}, {"kind": "A", "name": "a"}]
        self.assertEqual(fw._owner_key(owners), fw._owner_key(list(reversed(owners))))


class SizingOwnerTest(unittest.TestCase):
    @staticmethod
    def meta(kind="ReplicaSet", name="web-74d7c4f678", hash_label="74d7c4f678"):
        labels = {"pod-template-hash": hash_label} if hash_label else {}
        return {
            "name": "web-74d7c4f678-abcde",
            "labels": labels,
            "ownerReferences": [{"kind": kind, "name": name, "controller": True}],
        }

    def test_a_replicaset_resolves_to_the_deployment_that_declares_it(self):
        self.assertEqual(fw._sizing_owner(self.meta()), ("Deployment", "web"))

    def test_a_bare_replicaset_keeps_its_own_name(self):
        # No `pod-template-hash` means no Deployment above it, so the
        # ReplicaSet really is the object the repo declares.
        self.assertEqual(
            fw._sizing_owner(self.meta(name="standalone", hash_label=None)),
            ("ReplicaSet", "standalone"),
        )

    def test_a_hash_that_is_not_the_name_suffix_is_not_stripped(self):
        self.assertEqual(
            fw._sizing_owner(self.meta(name="web-abc", hash_label="74d7c4f678")),
            ("ReplicaSet", "web-abc"),
        )

    def test_other_kinds_pass_through(self):
        self.assertEqual(
            fw._sizing_owner(self.meta(kind="StatefulSet", name="db", hash_label=None)),
            ("StatefulSet", "db"),
        )

    def test_an_unowned_pod_is_its_own_object(self):
        self.assertEqual(
            fw._sizing_owner({"name": "debug", "ownerReferences": []}), ("Pod", "debug")
        )


class JobFinishedAtTest(unittest.TestCase):
    def test_picks_the_terminal_condition_not_the_first_one(self):
        status = {
            "conditions": [
                {"type": "Suspended", "status": "False", "lastTransitionTime": "2026-01-01T00:00:00Z"},
                {"type": "Failed", "status": "True", "lastTransitionTime": "2026-07-01T00:00:00Z"},
            ]
        }
        self.assertEqual(fw._job_finished_at(status), "2026-07-01T00:00:00Z")

    def test_a_condition_that_is_not_true_does_not_count(self):
        status = {"conditions": [{"type": "Failed", "status": "False", "lastTransitionTime": "2026-01-01T00:00:00Z"}]}
        self.assertEqual(fw._job_finished_at(status), "")

    def test_success_criteria_met_counts(self):
        status = {"conditions": [{"type": "SuccessCriteriaMet", "status": "True", "lastTransitionTime": "2026-02-02T00:00:00Z"}]}
        self.assertEqual(fw._job_finished_at(status), "2026-02-02T00:00:00Z")

    def test_no_conditions_is_empty(self):
        self.assertEqual(fw._job_finished_at({}), "")


class TerminalPodsTest(unittest.TestCase):
    def terminal_pod(self, ns="default", name="p", phase="Succeeded", created="2026-01-01T00:00:00Z"):
        return obj("Pod", name, ns=ns, **{"status.phase": phase, "metadata.creationTimestamp": created})

    def test_flags_a_namespace_with_50_or_more(self):
        # Two days old: past the one-day grace, short of the seven-day arm, so
        # only the count can flag it.
        created = (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        pods = [self.terminal_pod(name=f"p{i}", created=created) for i in range(50)]
        hits = fw.check_terminal_pods({"pods": pods, "jobs": [], "cronjobs": []}, now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertIn("50 terminal pods", hits[0]["excerpt"])
        self.assertEqual(fw.check_terminal_pods({"pods": pods[:49], "jobs": [], "cronjobs": []}, now=NOW), [])

    def test_flags_a_single_old_pod(self):
        pods = [self.terminal_pod(created="2026-01-01T00:00:00Z")]
        context = {"pods": pods, "jobs": [], "cronjobs": []}
        self.assertEqual(len(fw.check_terminal_pods(context, now=NOW)), 1)

    def test_does_not_flag_recent_small_backlog(self):
        pods = [self.terminal_pod(created="2026-07-30T00:00:00Z") for _ in range(3)]
        context = {"pods": pods, "jobs": [], "cronjobs": []}
        self.assertEqual(fw.check_terminal_pods(context, now=NOW), [])

    def test_flags_standalone_job_without_ttl(self):
        job = obj("Job", "batch", ns="default", **{"status.succeeded": 1, "status.completionTime": "2026-01-01T00:00:00Z"})
        context = {"pods": [], "jobs": [job], "cronjobs": []}
        hits = fw.check_terminal_pods(context, now=NOW)
        self.assertTrue(any(h["object"] == "Job/batch" for h in hits))

    def test_does_not_flag_a_job_whose_controller_collects_it(self):
        job = obj("Job", "step", ns="default", **{"status.succeeded": 1, "status.completionTime": "2026-01-01T00:00:00Z", "metadata.labels": {"workflows.argoproj.io/workflow": "w"}})
        context = {"pods": [], "jobs": [job], "cronjobs": []}
        self.assertEqual(fw.check_terminal_pods(context, now=NOW), [])

    def test_does_not_flag_job_with_ttl_set(self):
        job = obj("Job", "batch", ns="default", **{"status.succeeded": 1, "status.completionTime": "2026-01-01T00:00:00Z", "spec.ttlSecondsAfterFinished": 3600})
        context = {"pods": [], "jobs": [job], "cronjobs": []}
        self.assertEqual(fw.check_terminal_pods(context, now=NOW), [])

    def test_does_not_flag_cronjob_owned_job(self):
        job = obj(
            "Job", "cron-123", ns="default",
            **{"status.succeeded": 1, "status.completionTime": "2026-01-01T00:00:00Z", "metadata.ownerReferences": [{"kind": "CronJob", "name": "cron"}]},
        )
        context = {"pods": [], "jobs": [job], "cronjobs": []}
        self.assertEqual(fw.check_terminal_pods(context, now=NOW), [])

    def test_flags_cronjob_with_excessive_history_limit(self):
        cj = obj("CronJob", "chatty", ns="default", **{"spec.successfulJobsHistoryLimit": 20})
        context = {"pods": [], "jobs": [], "cronjobs": [cj]}
        hits = fw.check_terminal_pods(context, now=NOW)
        self.assertTrue(any(h["object"] == "CronJob/chatty" for h in hits))


class IdleNamespaceTest(unittest.TestCase):
    def ns(self, name, created="2026-01-01T00:00:00Z"):
        return obj("Namespace", name, **{"metadata.creationTimestamp": created})

    def test_flags_idle_ns_with_loadbalancer(self):
        svc = obj("Service", "lb", ns="demo", **{"spec.type": "LoadBalancer"})
        context = {"pods": [], "pvcs": [], "services": [svc], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        hits = fw.check_idle_namespace(context, now=NOW)
        self.assertEqual(hits[0]["severity"], "major")

    def test_flags_idle_ns_with_pvc(self):
        pvc = obj("PersistentVolumeClaim", "d", ns="demo", **{"status.capacity": {"storage": "10Gi"}})
        context = {"pods": [], "pvcs": [pvc], "services": [], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        self.assertEqual(len(fw.check_idle_namespace(context, now=NOW)), 1)

    def test_does_not_flag_active_namespace(self):
        pod = obj("Pod", "p", ns="demo", **{"status.phase": "Running"})
        svc = obj("Service", "lb", ns="demo", **{"spec.type": "LoadBalancer"})
        context = {"pods": [pod], "pvcs": [], "services": [svc], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        self.assertEqual(fw.check_idle_namespace(context, now=NOW), [])

    def test_does_not_flag_namespace_with_nothing_billable(self):
        context = {"pods": [], "pvcs": [], "services": [], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        self.assertEqual(fw.check_idle_namespace(context, now=NOW), [])

    def test_does_not_flag_gitops_synced_namespace(self):
        svc = obj("Service", "lb", ns="demo", **{"spec.type": "LoadBalancer"})
        ns_doc = self.ns("demo")
        ns_doc["metadata"]["annotations"]["configsync.gke.io/sync-name"] = "x"
        context = {"pods": [], "pvcs": [], "services": [svc], "resourcequotas": [], "namespaces": [ns_doc]}
        self.assertEqual(fw.check_idle_namespace(context, now=NOW), [])

    def test_does_not_flag_a_namespace_annotated_with_an_owner_or_retention(self):
        """§3.10: someone has said who keeps it, or for how long."""
        svc = obj("Service", "lb", ns="demo", **{"spec.type": "LoadBalancer"})
        for key in ("owner", "example.com/team-owner", "backup.example.com/retention-days", "retain"):
            ns_doc = self.ns("demo")
            ns_doc["metadata"]["annotations"][key] = "x"
            context = {"pods": [], "pvcs": [], "services": [svc], "resourcequotas": [], "namespaces": [ns_doc]}
            self.assertEqual(fw.check_idle_namespace(context, now=NOW), [], key)

    def test_an_unrelated_annotation_does_not_suppress(self):
        svc = obj("Service", "lb", ns="demo", **{"spec.type": "LoadBalancer"})
        ns_doc = self.ns("demo")
        ns_doc["metadata"]["annotations"]["kubectl.kubernetes.io/last-applied-configuration"] = "{}"
        context = {"pods": [], "pvcs": [], "services": [svc], "resourcequotas": [], "namespaces": [ns_doc]}
        self.assertEqual(len(fw.check_idle_namespace(context, now=NOW)), 1)

    def test_does_not_flag_a_namespace_a_cronjob_runs_in(self):
        """§3.10: idle between fires by design, suspended or not."""
        svc = obj("Service", "lb", ns="demo", **{"spec.type": "LoadBalancer"})
        cj = obj("CronJob", "nightly", ns="demo", **{"spec.suspend": True})
        context = {"pods": [], "pvcs": [], "services": [svc], "resourcequotas": [],
                   "namespaces": [self.ns("demo")], "cronjobs": [cj]}
        self.assertEqual(fw.check_idle_namespace(context, now=NOW), [])

    def test_does_not_flag_a_flux_labelled_namespace(self):
        """Flux's kustomize-controller marks what it applies with labels, not
        annotations."""
        svc = obj("Service", "lb", ns="demo", **{"spec.type": "LoadBalancer"})
        ns_doc = self.ns("demo")
        ns_doc["metadata"].setdefault("labels", {})["kustomize.toolkit.fluxcd.io/name"] = "apps"
        context = {"pods": [], "pvcs": [], "services": [svc], "resourcequotas": [], "namespaces": [ns_doc]}
        self.assertEqual(fw.check_idle_namespace(context, now=NOW), [])

    def test_a_part_day_over_the_gate_reports_the_gate_not_the_day_after(self):
        # 30d 14h, against a 30-day gate. See the matching case in OrphanPvTest.
        pvc = obj("PersistentVolumeClaim", "d", ns="demo", **{"status.capacity": {"storage": "10Gi"}})
        context = {"pods": [], "pvcs": [pvc], "services": [], "resourcequotas": [], "namespaces": [self.ns("demo", created="2026-07-01T10:00:00Z")]}
        self.assertIn("created 30d ago;", fw.check_idle_namespace(context, now=NOW)[0]["excerpt"])

    def test_the_excerpt_claims_no_pod_free_duration(self):
        # The pod dump is a snapshot and the age is the namespace's; the live
        # `default` namespace read "no Running/Pending pods for 69d" when all
        # anyone knew was that it had none now and was 69 days old.
        pvc = obj("PersistentVolumeClaim", "d", ns="demo", **{"status.capacity": {"storage": "10Gi"}})
        context = {"pods": [], "pvcs": [pvc], "services": [], "resourcequotas": [], "namespaces": [self.ns("demo", created="2026-07-01T10:00:00Z")]}
        excerpt = fw.check_idle_namespace(context, now=NOW)[0]["excerpt"]
        self.assertTrue(excerpt.startswith("no Running/Pending pods now; namespace created 30d ago;"), excerpt)
        self.assertNotIn("pods for", excerpt)

    def test_capacity_just_under_the_severity_gate_is_not_printed_as_the_gate(self):
        # 102000Mi is 99.6 GiB. Rounding to nearest printed "100 GiB" while the
        # gate read the raw value and graded `minor`, so one finding said the
        # threshold was met and denied it in the same breath.
        pvc = obj("PersistentVolumeClaim", "d", ns="demo", **{"status.capacity": {"storage": "102000Mi"}})
        context = {"pods": [], "pvcs": [pvc], "services": [], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        hit = fw.check_idle_namespace(context, now=NOW)[0]
        self.assertIn("99 GiB of PVCs", hit["excerpt"])
        self.assertEqual(hit["severity"], "minor")

    def test_a_sub_gibibyte_claim_is_not_floored_away_to_zero(self):
        # Flooring must not print "0 GiB of PVCs" about the very PVC that made
        # the namespace billable; only a namespace billable through something
        # other than storage gets a literal 0.
        pvc = obj("PersistentVolumeClaim", "d", ns="demo", **{"status.capacity": {"storage": "500Mi"}})
        context = {"pods": [], "pvcs": [pvc], "services": [], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        self.assertIn("<1 GiB of PVCs", fw.check_idle_namespace(context, now=NOW)[0]["excerpt"])

    def test_a_namespace_billable_only_by_a_loadbalancer_holds_zero(self):
        svc = obj("Service", "lb", ns="demo", **{"spec.type": "LoadBalancer"})
        context = {"pods": [], "pvcs": [], "services": [svc], "resourcequotas": [], "namespaces": [self.ns("demo")]}
        self.assertIn("a LoadBalancer Service, 0 GiB of PVCs", fw.check_idle_namespace(context, now=NOW)[0]["excerpt"])

    def test_a_resourcequota_does_not_make_an_empty_namespace_billable(self):
        # A quota reserves nothing and bills nothing -- it gates admission on
        # the requests of the pods in its own namespace, of which there are
        # none. This namespace is the live `gitops-managed`: no pods, no PVCs,
        # no Services, one quota with `used` all zeroes, costing zero. Flagged,
        # it drew the impact "reserving 10 vCPU / 20 GiB of request headroom
        # ... that no other namespace on this Autopilot cluster can use", which
        # is false in all three of its claims.
        rq = obj("ResourceQuota", "platform-baseline-quota", ns="demo", **{"status.hard": {"requests.cpu": "10"}})
        context = {"pods": [], "pvcs": [], "services": [], "resourcequotas": [rq], "namespaces": [self.ns("demo")]}
        self.assertEqual(fw.check_idle_namespace(context, now=NOW), [])

    def test_a_resourcequota_beside_a_real_billable_object_still_flags(self):
        # Dropping the quota arm must not suppress a namespace that a PVC or a
        # LoadBalancer would have flagged on its own.
        rq = obj("ResourceQuota", "q", ns="demo", **{"status.hard": {"requests.cpu": "10"}})
        pvc = obj("PersistentVolumeClaim", "d", ns="demo", **{"status.capacity": {"storage": "10Gi"}})
        context = {"pods": [], "pvcs": [pvc], "services": [], "resourcequotas": [rq], "namespaces": [self.ns("demo")]}
        hits = fw.check_idle_namespace(context, now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertNotIn("uota", hits[0]["excerpt"])


class OverrequestTest(unittest.TestCase):
    def deployment_pod(self, ns="default", name="api-1", cpu_req="12", mem_req="48Gi", cpu_lim=None, mem_lim=None, started="2026-01-01T00:00:00Z", owner_kind="ReplicaSet", owner_name="api"):
        resources = {"requests": {"cpu": cpu_req, "memory": mem_req}}
        if cpu_lim or mem_lim:
            resources["limits"] = {"cpu": cpu_lim or cpu_req, "memory": mem_lim or mem_req}
        return obj(
            "Pod", name, ns=ns,
            **{
                "spec.containers": [{"resources": resources}],
                "status.startTime": started,
                "status.phase": "Running",
                "metadata.ownerReferences": [{"kind": owner_kind, "name": owner_name}],
            },
        )

    IDLE = {("default", "api-1"): (0.0, 0.0)}

    def test_a_native_sidecars_request_is_sized_and_the_target_is_a_pod_total(self):
        """The app sits on the floor; the sidecar requests 4 vCPU / 8Gi and
        uses none of it. Its usage is in the peak, so its request is in the
        sum, and the Resize-to figure says it is the pod's to split."""
        pod = self.deployment_pod(cpu_req="50m", mem_req="64Mi")
        pod["spec"]["initContainers"] = [
            {"name": "proxy", "restartPolicy": "Always", "resources": {"requests": {"cpu": "4", "memory": "8Gi"}}},
        ]
        [hit] = fw.check_overrequest({"pods": [pod]}, self.IDLE, now=NOW, autopilot=False)
        self.assertIn("That is the pod's total, native sidecar `proxy` included", hit["excerpt"])

    def hashed_pod(self):
        pod = self.deployment_pod(owner_name="api-5d8f7")
        pod["metadata"]["labels"]["pod-template-hash"] = "5d8f7"
        return pod

    def test_an_hpa_target_is_not_resized(self):
        pod = self.hashed_pod()
        hpa = obj("HorizontalPodAutoscaler", "api", ns="default", **{"spec.scaleTargetRef": {"kind": "Deployment", "name": "api"}})
        self.assertEqual(fw.check_overrequest({"pods": [pod], "hpas": [hpa]}, self.IDLE, now=NOW, autopilot=False), [])
        # Control: the same controller with no HPA is reported.
        self.assertEqual(len(fw.check_overrequest({"pods": [pod]}, self.IDLE, now=NOW, autopilot=False)), 1)

    def test_a_request_the_limitrange_filled_in_is_not_resized(self):
        pod = self.deployment_pod()
        lr = obj("LimitRange", "defaults", ns="default", **{"spec.limits": [{"type": "Container", "defaultRequest": {"cpu": "12", "memory": "48Gi"}}]})
        self.assertEqual(fw.check_overrequest({"pods": [pod], "limitranges": [lr]}, self.IDLE, now=NOW, autopilot=False), [])
        other = obj("LimitRange", "defaults", ns="default", **{"spec.limits": [{"type": "Container", "defaultRequest": {"cpu": "1", "memory": "1Gi"}}]})
        self.assertEqual(len(fw.check_overrequest({"pods": [pod], "limitranges": [other]}, self.IDLE, now=NOW, autopilot=False)), 1)

    def test_a_cpu_only_default_does_not_hide_a_hand_written_memory_request(self):
        """GKE's stock LimitRange defaults CPU only; the memory request is the workload's own."""
        pod = self.deployment_pod()
        cpu_only = obj("LimitRange", "limits", ns="default", **{"spec.limits": [{"type": "Container", "defaultRequest": {"cpu": "12"}}]})
        hits = fw.check_overrequest({"pods": [pod], "limitranges": [cpu_only]}, self.IDLE, now=NOW, autopilot=False)
        self.assertEqual(len(hits), 1)
        self.assertIn("memory only", hits[0]["excerpt"])
        # Idle as it is, the CPU is the LimitRange's, and "in use" would be
        # false evidence in front of the reviewer.
        self.assertIn("cpu is the namespace LimitRange default", hits[0]["excerpt"])
        self.assertNotIn("in use", hits[0]["excerpt"])

    def test_the_defaulted_dimension_alone_is_never_the_finding(self):
        """Memory written by hand and in use, CPU filled in by the LimitRange and
        idle: the only resize on offer is of a value nobody declared, which §3.1
        sends to the LimitRange."""
        pod = self.deployment_pod()
        cpu_only = obj("LimitRange", "limits", ns="default", **{"spec.limits": [{"type": "Container", "defaultRequest": {"cpu": "12"}}]})
        memory_in_use = {("default", "api-1"): (0.01, 45000.0)}
        self.assertEqual(fw.check_overrequest({"pods": [pod], "limitranges": [cpu_only]}, memory_in_use, now=NOW, autopilot=False), [])

    def test_a_defaulted_limit_counts_as_the_filled_in_request(self):
        """A LimitRange read before object defaulting (a manifest, a dump) may
        carry only `default`; the API server would have filled `defaultRequest`
        from it, so it counts as the filled-in request."""
        pod = self.deployment_pod()
        lr = obj("LimitRange", "defaults", ns="default", **{"spec.limits": [{"type": "Container", "default": {"cpu": "12", "memory": "48Gi"}}]})
        self.assertEqual(fw.check_overrequest({"pods": [pod], "limitranges": [lr]}, self.IDLE, now=NOW, autopilot=False), [])

    def test_a_deleting_or_failed_pod_is_not_a_replica(self):
        deleting = self.deployment_pod(name="api-2")
        deleting["metadata"]["deletionTimestamp"] = "2026-07-31T00:00:00Z"
        failed = self.deployment_pod(name="api-3")
        failed["status"]["phase"] = "Failed"
        by_owner = fw._eligible_pods_by_owner({"pods": [self.deployment_pod(), deleting, failed]}, now=NOW)
        self.assertEqual([len(e["pods"]) for e in by_owner.values()], [1])

    def test_guaranteed_is_decided_per_container(self):
        def entry(*containers):
            return {"pods": [{"requests": [c[0] for c in containers], "limits": [c[1] for c in containers]}]}
        full = ({"cpu": "1", "memory": "1Gi"}, {"cpu": "1", "memory": "1Gi"})
        self.assertTrue(fw._is_guaranteed(entry(full)))
        # A request left unset defaults to the limit.
        self.assertTrue(fw._is_guaranteed(entry(({}, {"cpu": "1", "memory": "1Gi"}))))
        self.assertFalse(fw._is_guaranteed(entry(({"cpu": "1"}, {"cpu": "1"}))))
        self.assertFalse(fw._is_guaranteed(entry(full, ({}, {}))))

    def test_flags_gross_overrequest(self):
        pod = self.deployment_pod()
        peaks = {("default", "api-1"): (0.9, 3072.0)}  # 0.9 vCPU / 3 GiB peak vs 12/48 requested
        hits = fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=False)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_does_not_flag_when_the_peak_clears_the_bar(self):
        # The window used to be three ten-minute samples and this rule used to
        # be "every one of them agrees". A week-long peak is the same rule
        # without the sampling error: a workload that reached 11 vCPU once is
        # not over-requesting at 12, however idle it looked when we last
        # happened to run `kubectl top`.
        pod = self.deployment_pod()
        peaks = {("default", "api-1"): (11.0, 40000.0)}
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=False), [])

    def test_an_unmeasured_controller_is_not_reported(self):
        """Absent is not idle, and here absent reads as *maximally* idle.

        Summing pods missing from the Monitoring answer as zero puts the
        controller at 0% of its request on both dimensions at once, which
        clears both ratio tests by the widest possible margin -- so the failure
        mode is not a missed finding but a confident one, proposing to cut the
        request of a workload nobody observed down to the 50m/64Mi floor. The
        `if not usage_peaks` guard at the top of the check only catches a
        cluster that answered nothing at all; a single namespace missing, or a
        metrics agent down on one node, arrives here.
        """
        pod = self.deployment_pod()
        populated = {("default", "somebody-else"): (4.0, 8192.0)}
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, populated, now=NOW, autopilot=False), [])
        # Control: the same controller, same zero usage, but *present* in the
        # answer is reported -- so it is the guard doing the silencing above
        # and not the materiality floor or an eligibility exclusion.
        self.assertEqual(len(fw.check_overrequest({"pods": [pod]}, self.IDLE, now=NOW, autopilot=False)), 1)

    def test_a_dimension_no_series_was_read_for_is_not_resized(self):
        """CPU in use, memory never read. Reading the missing memory series as
        zero published "Over-requested on memory only (0% of request); resize
        to memory 64Mi" on the strength of a series nobody read."""
        pod = self.deployment_pod()
        peaks = {("default", "api-1"): (11.0, None)}
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=False), [])

    def test_one_measured_pod_is_enough_to_judge_a_controller(self):
        """The guard drops the unmeasured, not the partially measured.

        A controller mid-rollout has a pod the window has not seen yet.
        Dropping it wholesale would make every over-request invisible for as
        long as one replica stayed unmeasured, which is the opposite failure.
        """
        pods = [self.deployment_pod(name="api-1"), self.deployment_pod(name="api-2")]
        peaks = {("default", "api-1"): (0.5, 1024.0)}  # api-2 absent
        hits = fw.check_overrequest({"pods": pods}, peaks, now=NOW, autopilot=False)
        self.assertEqual([h["object"] for h in hits], ["ReplicaSet/api"])

    def test_does_not_flag_below_the_absolute_floor(self):
        pod = self.deployment_pod(cpu_req="50m", mem_req="64Mi")
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, self.IDLE, now=NOW, autopilot=False), [])

    def test_the_floor_sits_where_the_constants_say_it_does(self):
        """A 90m request is the sidecar the floor excludes; 110m is not.

        The old floor was 2 vCPU / 4 GiB -- a node's worth of headroom inside a
        single controller -- and on the sixteen-cluster fleet nothing ever
        reached it, so the check published nothing while seven controllers sat
        under 20% of their requests on both dimensions.
        """
        under = self.deployment_pod(name="small-1", owner_name="small", cpu_req="90m", mem_req="64Mi")
        over = self.deployment_pod(name="big-1", owner_name="big", cpu_req="110m", mem_req="64Mi")
        peaks = {("default", "small-1"): (0.0, 0.0), ("default", "big-1"): (0.0, 0.0)}
        hits = fw.check_overrequest({"pods": [under, over]}, peaks, now=NOW, autopilot=False)
        self.assertEqual([h["object"] for h in hits], ["ReplicaSet/big"])
        self.assertEqual(fw.OVERREQUEST_FLOOR_VCPU, 0.1)
        self.assertEqual(fw.OVERREQUEST_FLOOR_GIB, 0.125)

    def test_the_floor_tests_the_request_and_not_the_reclaimable_delta(self):
        """The two differ by the usage, and that gap silenced real findings.

        `github-token-minter` on the live fleet: 200m requested, peak 0.001
        vCPU over a week -- half a percent -- and a 199m delta that failed a
        250m test whose stated justification ("about the smallest request a
        first-class service is given") describes a request. Comparing the
        request is what makes the justification true of the code.

        The values make the two tests disagree: a 100m request sits on the
        100m floor, while its 81m delta (peak 19m, idle at 19%) is under it.
        Memory is on its 64Mi resize floor, so CPU alone decides.
        """
        pod = self.deployment_pod(cpu_req="100m", mem_req="64Mi")
        peak_cpu = 0.019
        self.assertGreaterEqual(0.1, fw.OVERREQUEST_FLOOR_VCPU)
        self.assertLess(0.1 - peak_cpu, fw.OVERREQUEST_FLOOR_VCPU)
        hits = fw.check_overrequest(
            {"pods": [pod]}, {("default", "api-1"): (peak_cpu, 24.0)}, now=NOW, autopilot=False
        )
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "minor")

    def test_a_request_exactly_on_the_floor_reports(self):
        # The boundary is inclusive, so `hello-world`'s 100m -- the commonest
        # request on the fleet -- is inside the check rather than one
        # rounding-error away from it.
        pod = self.deployment_pod(cpu_req="100m", mem_req="64Mi")
        self.assertEqual(len(fw.check_overrequest({"pods": [pod]}, self.IDLE, now=NOW, autopilot=False)), 1)

    def test_a_ten_milli_sidecar_is_still_excluded(self):
        """What the resize floors exist for, and the reason not to simply
        delete them.

        `cert-manager` and its webhook on the live fleet: 10m and 32Mi each,
        both under 20% of both requests all week. Both requests already sit at
        or under `OVERREQUEST_RESIZE_FLOOR_VCPU`/`_MIB`, so no resize shrinks
        either, and the resize floor excludes them before the materiality
        floor is consulted.
        """
        for label, cpu, mem in (("cert-manager", "10m", "32Mi"), ("bad-app", "50m", "64Mi")):
            with self.subTest(label):
                pod = self.deployment_pod(cpu_req=cpu, mem_req=mem)
                self.assertEqual(
                    fw.check_overrequest({"pods": [pod]}, self.IDLE, now=NOW, autopilot=False), []
                )

    def test_flags_an_idle_deployment_the_old_floor_dropped(self):
        """`ai-inference-hardened` on the live fleet: half a vCPU and 2 GiB
        reserved, 0.001 vCPU and 0.15 GiB ever used in a week. The percentage
        rule caught it and the 2 vCPU / 4 GiB floor threw it away."""
        pod = self.deployment_pod(cpu_req="500m", mem_req="2Gi")
        hits = fw.check_overrequest({"pods": [pod]}, {("default", "api-1"): (0.001, 150.0)}, now=NOW, autopilot=False)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "minor")

    def test_flags_a_cpu_only_overrequest(self):
        """The commonest real shape: sized for its memory, handed a
        copy-pasted CPU request. Requiring both dimensions to be idle at once
        let the memory ratio veto a 6.6x CPU over-request -- which is
        `platform-agent-gateway` on the live fleet, at 15% of its CPU request
        and 86% of its memory one."""
        pod = self.deployment_pod(cpu_req="1100m", mem_req="1Gi")
        hits = fw.check_overrequest({"pods": [pod]}, {("default", "api-1"): (0.167, 880.0)}, now=NOW, autopilot=False)
        self.assertEqual(len(hits), 1)
        self.assertIn("Over-requested on cpu only", hits[0]["excerpt"])
        self.assertIn("must not be resized", hits[0]["excerpt"])

    def test_flags_a_memory_only_overrequest(self):
        pod = self.deployment_pod(cpu_req="1", mem_req="8Gi")
        hits = fw.check_overrequest({"pods": [pod]}, {("default", "api-1"): (0.9, 400.0)}, now=NOW, autopilot=False)
        self.assertEqual(len(hits), 1)
        self.assertIn("Over-requested on memory only", hits[0]["excerpt"])

    def test_a_memory_only_request_does_not_call_cpu_in_use(self):
        # No CPU request and no LimitRange default: "in use" would be a verdict
        # about a dimension the workload never declared.
        pod = self.deployment_pod(mem_req="8Gi")
        del pod["spec"]["containers"][0]["resources"]["requests"]["cpu"]
        hits = fw.check_overrequest({"pods": [pod]}, {("default", "api-1"): (0.9, 400.0)}, now=NOW, autopilot=False)
        self.assertEqual(len(hits), 1)
        self.assertIn("Over-requested on memory only", hits[0]["excerpt"])
        self.assertIn("cpu is not requested", hits[0]["excerpt"])
        self.assertNotIn("in use", hits[0]["excerpt"])

    def test_a_both_idle_finding_does_not_claim_one_dimension_is_in_use(self):
        pod = self.deployment_pod()
        hits = fw.check_overrequest({"pods": [pod]}, {("default", "api-1"): (0.9, 3072.0)}, now=NOW, autopilot=False)
        self.assertNotIn("only", hits[0]["excerpt"])

    def test_the_dimension_in_use_contributes_nothing_to_the_reclaimable_delta(self):
        """§3.1's remediation resizes a request to 2x the observed peak, so a
        dimension running at 94% of its request must not be part of the finding
        at all -- neither reaching the materiality floor for it nor raising its
        severity. 8 GiB of nominal slack under a 128 GiB request that is in use
        is not 8 GiB anyone can reclaim.

        Memory here runs at 50% of 128 GiB, so its 64 GiB of nominal slack is
        twice `NODE_WORTH_GIB`: counted, it would grade the finding `major`,
        where the idle CPU's 0.95 vCPU alone grades `minor`."""
        pod = self.deployment_pod(cpu_req="1", mem_req="128Gi")
        peaks = {("default", "api-1"): (0.05, 64 * 1024.0)}
        self.assertGreater(64, fw.NODE_WORTH_GIB)
        hits = fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=False)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "minor")
        self.assertIn("Over-requested on cpu only", hits[0]["excerpt"])

    def test_an_idle_dimension_under_the_materiality_floor_is_left_out(self):
        """CPU idle on a full vCPU carries the finding; memory is idle and
        shrinkable too, but its 100Mi request is under the 128Mi materiality
        floor, so the finding neither calls it over-requested nor asks to
        resize it."""
        pod = self.deployment_pod(cpu_req="1", mem_req="100Mi")
        hits = fw.check_overrequest({"pods": [pod]}, {("default", "api-1"): (0.01, 10.0)}, now=NOW, autopilot=False)
        self.assertEqual(len(hits), 1)
        excerpt = hits[0]["excerpt"]
        self.assertIn("Over-requested on cpu only", excerpt)
        self.assertIn("memory is idle, but its request is under the 100m/128Mi materiality floor", excerpt)
        self.assertNotIn("both dimensions", excerpt)
        self.assertIn("Resize to cpu 50m.", excerpt)
        self.assertNotIn("memory 64Mi", excerpt)

    def test_an_idle_dimension_under_the_floor_is_not_rescued_by_one_in_use(self):
        # 64 GiB is far over the memory floor, but memory is the dimension in
        # use. Only the idle dimension may satisfy the floor, and 80m -- above
        # the 50m resize floor, so it is idle and shrinkable -- does not.
        pod = self.deployment_pod(cpu_req="80m", mem_req="64Gi")
        peaks = {("default", "api-1"): (0.001, 60 * 1024.0)}
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=False), [])

    def test_does_not_flag_daemonset(self):
        pod = self.deployment_pod(owner_kind="DaemonSet", owner_name="ds")
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, self.IDLE, now=NOW, autopilot=False), [])

    def test_does_not_flag_job_owned_pod(self):
        pod = self.deployment_pod(owner_kind="Job", owner_name="batch")
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, self.IDLE, now=NOW, autopilot=False), [])

    def test_does_not_flag_a_pod_with_no_requests_at_all(self):
        pod = self.deployment_pod(cpu_req="0", mem_req="0")
        pod["spec"]["containers"][0]["resources"] = {}
        # Peaks for some *other* pod, so the no-requests skip is what makes
        # this pass rather than the empty-usage guard at the top.
        peaks = {("default", "unrelated"): (0.0, 0.0)}
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=False), [])

    def test_no_usage_data_flags_nothing_rather_than_everything(self):
        # `fetch_usage_peaks` returns `{}` for a cluster it could not read.
        # Reading that as "this Deployment used no CPU and no memory" would
        # flag every workload in the fleet as reclaimable waste.
        pod = self.deployment_pod()
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, {}, now=NOW, autopilot=False), [])

    def test_guaranteed_qos_is_marked_for_manual_remediation(self):
        pod = self.deployment_pod(cpu_lim="12", mem_lim="48Gi")
        peaks = {("default", "api-1"): (0.9, 3072.0)}
        hits = fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=False)
        self.assertTrue(hits[0]["_guaranteed"])

    def test_autopilot_bumps_minor_to_major(self):
        pod = self.deployment_pod(cpu_req="3", mem_req="6Gi")
        peaks = {("default", "api-1"): (0.1, 100.0)}
        hits = fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=True)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")
        self.assertTrue(hits[0]["_autopilot_bumped"])

    def test_a_major_the_delta_earned_is_not_marked_as_bumped(self):
        """Only a grade the bump supplied is held out of the sweep. A delta
        worth a node is `major` on any cluster and promotes on its own."""
        pod = self.deployment_pod()
        peaks = {("default", "api-1"): (0.9, 3072.0)}
        for autopilot in (False, True):
            with self.subTest(autopilot=autopilot):
                [hit] = fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=autopilot)
                self.assertEqual(hit["severity"], "major")
                self.assertFalse(hit["_autopilot_bumped"])

    def test_a_minor_off_autopilot_is_not_marked_as_bumped(self):
        pod = self.deployment_pod(cpu_req="3", mem_req="6Gi")
        peaks = {("default", "api-1"): (0.1, 100.0)}
        [hit] = fw.check_overrequest({"pods": [pod]}, peaks, now=NOW, autopilot=False)
        self.assertEqual(hit["severity"], "minor")
        self.assertFalse(hit["_autopilot_bumped"])

    def test_the_excerpt_names_the_window_it_rests_on(self):
        pod = self.deployment_pod()
        hits = fw.check_overrequest({"pods": [pod]}, {("default", "api-1"): (0.9, 3072.0)}, now=NOW, autopilot=False)
        self.assertIn(f"trailing {fw.USAGE_WINDOW_HOURS}h", hits[0]["excerpt"])

    def test_the_window_shrinks_to_a_controller_younger_than_it(self):
        # Monitoring holds a week of history; a Deployment rolled six hours ago
        # has six hours of it. Reporting "over the trailing 168h" there claims
        # to have watched something that did not exist for 162 of them.
        pod = self.deployment_pod(started="2026-07-31T18:00:00Z")
        hits = fw.check_overrequest({"pods": [pod]}, {("default", "api-1"): (0.9, 3072.0)}, now=NOW, autopilot=False)
        self.assertIn("trailing 6h", hits[0]["excerpt"])

    def test_a_short_window_says_why_it_disagrees_with_the_command(self):
        # `evidence.command` records the Monitoring read verbatim, so it says
        # `window=168h` however young the controller is. Three findings of the
        # 2026-09-06 cost report shipped that number beside an excerpt reading
        # "trailing 12h", with nothing to reconcile them.
        pod = self.deployment_pod(started="2026-07-31T18:00:00Z")
        excerpt = fw.check_overrequest(
            {"pods": [pod]}, {("default", "api-1"): (0.9, 3072.0)}, now=NOW, autopilot=False
        )[0]["excerpt"]
        self.assertIn(f"the read covers {fw.USAGE_WINDOW_HOURS}h", excerpt)
        self.assertIn("oldest pod started 6h ago", excerpt)

    def test_a_window_under_a_day_says_it_has_not_seen_one(self):
        # Six hours cannot distinguish an idle workload from one whose traffic
        # arrives overnight, and what follows the excerpt is an instruction to
        # shrink a request.
        pod = self.deployment_pod(started="2026-07-31T18:00:00Z")
        excerpt = fw.check_overrequest(
            {"pods": [pod]}, {("default", "api-1"): (0.9, 3072.0)}, now=NOW, autopilot=False
        )[0]["excerpt"]
        self.assertIn("under a full daily cycle", excerpt)

    def test_a_window_over_a_day_but_under_the_read_omits_the_cycle_clause(self):
        pod = self.deployment_pod(started="2026-07-29T00:00:00Z")
        excerpt = fw.check_overrequest(
            {"pods": [pod]}, {("default", "api-1"): (0.9, 3072.0)}, now=NOW, autopilot=False
        )[0]["excerpt"]
        self.assertIn("trailing 72h", excerpt)
        self.assertIn(f"the read covers {fw.USAGE_WINDOW_HOURS}h", excerpt)
        self.assertNotIn("daily cycle", excerpt)

    def test_a_full_window_says_nothing_about_the_read(self):
        # The two numbers agree, so there is nothing to reconcile and the
        # sentence stays the short one it has always been.
        pod = self.deployment_pod()
        excerpt = fw.check_overrequest(
            {"pods": [pod]}, {("default", "api-1"): (0.9, 3072.0)}, now=NOW, autopilot=False
        )[0]["excerpt"]
        self.assertIn(f"trailing {fw.USAGE_WINDOW_HOURS}h (Cloud Monitoring)", excerpt)
        self.assertNotIn("the read covers", excerpt)

    def test_the_window_follows_the_longest_lived_pod_of_the_controller(self):
        old = self.deployment_pod(name="api-1", started="2026-07-01T00:00:00Z")
        fresh = self.deployment_pod(name="api-2", started="2026-07-31T18:00:00Z")
        peaks = {("default", "api-1"): (0.4, 1536.0), ("default", "api-2"): (0.5, 1536.0)}
        hits = fw.check_overrequest({"pods": [old, fresh]}, peaks, now=NOW, autopilot=False)
        self.assertIn(f"trailing {fw.USAGE_WINDOW_HOURS}h", hits[0]["excerpt"])

    def test_an_unknown_start_time_falls_back_to_the_full_window(self):
        pod = self.deployment_pod(started="")
        hits = fw.check_overrequest({"pods": [pod]}, {("default", "api-1"): (0.9, 3072.0)}, now=NOW, autopilot=False)
        self.assertIn(f"trailing {fw.USAGE_WINDOW_HOURS}h", hits[0]["excerpt"])

    def test_pending_pod_is_never_flagged(self):
        pod = self.deployment_pod()
        pod["status"]["phase"] = "Pending"
        self.assertEqual(fw.check_overrequest({"pods": [pod]}, self.IDLE, now=NOW, autopilot=False), [])

    def test_a_multi_replica_excerpt_gives_the_per_replica_arithmetic(self):
        """Every total is summed across the controller's pods, but §3.1 sizes
        one container's request at 2x the peak -- so a reader handed only the
        total triples a three-replica Deployment's request. The excerpt is the
        only channel that survives `adopt_collector_evidence`."""
        pods = [self.deployment_pod(name=f"api-{i}", cpu_req="1", mem_req="2Gi") for i in (1, 2, 3)]
        peaks = {("default", f"api-{i}"): (0.05, 100.0) for i in (1, 2, 3)}
        excerpt = fw.check_overrequest({"pods": pods}, peaks, now=NOW, autopilot=False)[0]["excerpt"]
        self.assertIn("requests 3.00 vCPU / 6.0 GiB", excerpt)
        self.assertIn("Totals span 3 replicas", excerpt)
        self.assertIn("1.000 vCPU / 2.00 GiB requested", excerpt)
        self.assertIn("0.050 vCPU / 0.10 GiB peak", excerpt)
        self.assertIn("per replica", excerpt)

    def test_a_single_replica_excerpt_says_nothing_about_replicas(self):
        # One pod means the total *is* the per-replica figure, and a sentence
        # restating it is noise in an excerpt a human reads on every finding.
        excerpt = fw.check_overrequest(
            {"pods": [self.deployment_pod()]}, {("default", "api-1"): (0.9, 3072.0)},
            now=NOW, autopilot=False,
        )[0]["excerpt"]
        self.assertNotIn("replica", excerpt)
        self.assertTrue(excerpt.endswith("."), excerpt)

    def test_the_single_dimension_clause_and_the_replica_clause_compose(self):
        pods = [self.deployment_pod(name=f"api-{i}", cpu_req="1100m", mem_req="1Gi") for i in (1, 2)]
        peaks = {("default", f"api-{i}"): (0.167, 880.0) for i in (1, 2)}
        excerpt = fw.check_overrequest({"pods": pods}, peaks, now=NOW, autopilot=False)[0]["excerpt"]
        self.assertIn("Over-requested on cpu only", excerpt)
        self.assertIn("must not be resized.", excerpt)
        self.assertIn("Totals span 2 replicas", excerpt)


class OverrequestPrescribesTheResizeTest(unittest.TestCase):
    """The excerpt names the request each idle dimension must end up at.

    Left to infer it, the model works the number out of the peaks the excerpt
    quotes -- which are rounded to two decimals of a vCPU and one of a GiB, so
    a small workload reads as `0.00 vCPU / 0.0 GiB` -- and it has to recall
    the `50m`/`64Mi` clamp the SOP states beside `ceil(peak x 2)` to get the
    number right. The 2026-09-06 cost report asked for `about 10m` on a
    dimension whose floor is `50m`.
    """

    pod = OverrequestTest().deployment_pod

    def excerpt(self, pods, peaks):
        hits = fw.check_overrequest({"pods": pods}, peaks, now=NOW, autopilot=False)
        self.assertEqual(len(hits), 1, hits)
        return hits[0]["excerpt"]

    def minter(self, cpu_req, mem_req):
        """`github-token-minter` as the live fleet runs it: two replicas, and a
        7-day peak of 0.4 millicores and 11.2 MiB per replica."""
        pods = [
            self.pod(name=f"gtm-{i}", owner_name="github-token-minter", cpu_req=cpu_req, mem_req=mem_req)
            for i in (1, 2)
        ]
        return pods, {("default", f"gtm-{i}"): (0.0004, 11.2) for i in (1, 2)}

    def test_names_both_dimensions_when_both_are_over_requested(self):
        # The branch that names dimensions used to fire only at exactly one, so
        # the finding that most needs the instruction went without it.
        excerpt = self.excerpt(*self.minter("100m", "128Mi"))
        self.assertIn("Over-requested on both dimensions", excerpt)
        self.assertIn("resizing one alone does not clear this finding", excerpt)

    def test_prescribes_the_floor_rather_than_twice_the_peak(self):
        # 2 x 0.4m is 1m on CPU and 23Mi on memory; both clamp up.
        excerpt = self.excerpt(*self.minter("100m", "128Mi"))
        self.assertIn(
            f"Resize to cpu {fw.OVERREQUEST_RESIZE_FLOOR_VCPU * 1000:.0f}m "
            f"and memory {fw.OVERREQUEST_RESIZE_FLOOR_MIB:.0f}Mi per replica",
            excerpt,
        )

    def test_the_prescribed_resize_actually_clears_the_finding(self):
        # The property the wording exists to guarantee, asserted against the
        # check rather than against the sentence: applying what the excerpt
        # prescribes must leave nothing to report. A remediation that resizes
        # CPU alone -- what the 2026-09-06 report proposed -- must not.
        floor_cpu = f"{fw.OVERREQUEST_RESIZE_FLOOR_VCPU * 1000:.0f}m"
        floor_mem = f"{fw.OVERREQUEST_RESIZE_FLOOR_MIB:.0f}Mi"
        self.assertEqual(fw.check_overrequest({"pods": self.minter(floor_cpu, floor_mem)[0]},
                                              self.minter(floor_cpu, floor_mem)[1],
                                              now=NOW, autopilot=False), [])
        still_open = fw.check_overrequest({"pods": self.minter(floor_cpu, "128Mi")[0]},
                                          self.minter(floor_cpu, "128Mi")[1],
                                          now=NOW, autopilot=False)
        self.assertEqual(len(still_open), 1)
        self.assertIn("Over-requested on memory only", still_open[0]["excerpt"])

    def test_twice_the_peak_wins_where_it_clears_the_floor(self):
        # The clamp is a floor, not the answer: a controller peaking well above
        # it gets `ceil(peak x 2)`.
        pods = [self.pod(cpu_req="12", mem_req="48Gi")]
        excerpt = self.excerpt(pods, {("default", "api-1"): (0.9, 3072.0)})
        self.assertIn("Resize to cpu 1800m and memory 6144Mi", excerpt)

    def test_names_only_the_idle_dimension_where_the_other_is_in_use(self):
        pods = [self.pod(name=f"api-{i}", cpu_req="1100m", mem_req="1Gi") for i in (1, 2)]
        excerpt = self.excerpt(pods, {("default", f"api-{i}"): (0.167, 880.0) for i in (1, 2)})
        self.assertIn("Resize to cpu ", excerpt)
        self.assertNotIn("and memory", excerpt)

    def test_a_single_replica_is_not_told_the_figure_is_per_replica(self):
        excerpt = self.excerpt([self.pod()], {("default", "api-1"): (0.9, 3072.0)})
        self.assertIn("Resize to cpu 1800m and memory 6144Mi.", excerpt)


class OverrequestResizeIsANoOpTest(unittest.TestCase):
    """A finding whose own remediation changes nothing is not reported.

    §3.1 resizes an idle request to `ceil(peak x 2)` per replica, floored at
    `50m` / `64Mi`. A controller already sitting on that floor on every
    dimension the finding calls idle has a recommendation identical to the
    request it already declares -- so no manifest edit closes it and the model,
    following the rule correctly, answers "already at the sizing floor; no
    resize is possible". The 2026-09-05 cost report carried three of them
    (`hello-world` on `adam-new-cluster`, `adamparco-gitops` and
    `ap-ap-deploy-test`), each graded `major` by the Autopilot bump.
    """

    pod = OverrequestTest().deployment_pod

    def hits(self, pods, peaks, autopilot=False):
        return fw.check_overrequest({"pods": pods}, peaks, now=NOW, autopilot=autopilot)

    def test_a_controller_already_on_the_floor_is_not_reported(self):
        # `hello-world` verbatim: two replicas at 50m/64Mi each, peaking at
        # 0.010 vCPU / 19 MiB across both over the week. Idle on both
        # dimensions, material on CPU at exactly 100m -- and unshrinkable,
        # because 2x the per-replica peak clamps to the floor it is already on.
        pods = [self.pod(name=f"hw-{i}", owner_name="hello-world", cpu_req="50m", mem_req="64Mi") for i in (1, 2)]
        peaks = {("default", "hw-1"): (0.005, 9.5), ("default", "hw-2"): (0.005, 9.5)}
        self.assertEqual(self.hits(pods, peaks), [])
        # And the Autopilot bump does not resurrect it: the drop happens before
        # severity is decided, so it is not merely graded down to `minor`.
        self.assertEqual(self.hits(pods, peaks, autopilot=True), [])

    def test_one_millicore_above_the_floor_is_still_reported(self):
        """The control that says it is this gate and not the materiality floor.

        51m per replica is over the resize floor by the smallest amount a
        manifest can express, and 102m of request still clears the 100m
        materiality floor -- so the only thing separating it from the case
        above is whether a resize would change the number.
        """
        pods = [self.pod(name=f"hw-{i}", owner_name="hello-world", cpu_req="51m", mem_req="64Mi") for i in (1, 2)]
        peaks = {("default", "hw-1"): (0.005, 9.5), ("default", "hw-2"): (0.005, 9.5)}
        self.assertEqual([h["object"] for h in self.hits(pods, peaks)], ["ReplicaSet/hello-world"])

    def test_a_three_replica_controller_on_the_floor_is_not_reported(self):
        """The per-replica quotient is a float, and an exact `>` fails here.

        Three pods of `50m` sum to 0.15000000000000002, so a third of the total
        is a hair above the 50m the controller declares -- which an exact
        comparison reads as a reclaimable delta and reports. Every
        three-replica controller on the floor would come back.
        """
        pods = [self.pod(name=f"hw-{i}", owner_name="hello-world", cpu_req="50m", mem_req="64Mi") for i in (1, 2, 3)]
        peaks = {("default", f"hw-{i}"): (0.005, 9.5) for i in (1, 2, 3)}
        self.assertEqual(self.hits(pods, peaks), [])

    def test_a_floor_bound_dimension_does_not_veto_a_shrinkable_one(self):
        """CPU is on the floor; memory is idle at 8 GiB with 7-and-change to
        give back. The finding is about the memory, and it says so."""
        pod = self.pod(cpu_req="50m", mem_req="8Gi")
        hits = self.hits([pod], {("default", "api-1"): (0.001, 100.0)})
        self.assertEqual(len(hits), 1)
        self.assertIn("Over-requested on memory only", hits[0]["excerpt"])

    def test_a_floor_bound_dimension_is_not_described_as_in_use(self):
        """CPU at 2% of a 50m request is idle, just not reclaimable.

        The excerpt's single-dimension clause exists to stop a reader resizing
        a dimension the workload is consuming. Reusing that wording for a
        floor-bound dimension would tell the reviewer something false about the
        workload -- and this excerpt is what `adopt_collector_evidence`
        substitutes for whatever the model wrote, so it is the version that
        reaches the ledger.
        """
        pod = self.pod(cpu_req="50m", mem_req="8Gi")
        excerpt = self.hits([pod], {("default", "api-1"): (0.001, 100.0)})[0]["excerpt"]
        self.assertIn("cpu is already at the 50m/64Mi sizing floor", excerpt)
        self.assertNotIn("in use", excerpt)

    def test_a_dimension_below_the_floor_is_not_described_as_on_it(self):
        # Idle and not reclaimable also covers a 10m request, and "at the
        # 50m/64Mi floor" would tell the reviewer it declares 50m.
        pod = self.pod(cpu_req="10m", mem_req="8Gi")
        excerpt = self.hits([pod], {("default", "api-1"): (0.001, 100.0)})[0]["excerpt"]
        self.assertIn("cpu is already below the 50m/64Mi sizing floor", excerpt)
        self.assertNotIn("at the 50m/64Mi", excerpt)

    def test_a_dimension_genuinely_in_use_still_says_so(self):
        # The control for the branch above: memory at 86% of its request is
        # excluded for the original reason, and the original wording holds.
        pod = self.pod(cpu_req="1100m", mem_req="1Gi")
        excerpt = self.hits([pod], {("default", "api-1"): (0.167, 880.0)})[0]["excerpt"]
        self.assertIn("memory is in use and must not be resized", excerpt)

    def test_a_shrinkable_dimension_in_use_does_not_rescue_the_finding(self):
        """Only the dimensions the finding calls idle are consulted.

        Memory here is at 39% of its request -- not idle, so the excerpt never
        proposes touching it -- and `2x peak` would nonetheless come out below
        what it declares. Letting that count would publish a finding whose only
        reclaimable dimension is one §3.1 forbids resizing.
        """
        pods = [self.pod(name=f"api-{i}", cpu_req="50m", mem_req="1Gi") for i in (1, 2, 3, 4)]
        peaks = {("default", f"api-{i}"): (0.0005, 400.0) for i in (1, 2, 3, 4)}
        # Material on CPU: 4 x 50m is 200m, twice the 100m floor.
        self.assertEqual(self.hits(pods, peaks), [])

    def test_memory_on_the_floor_is_dropped_when_cpu_is_in_use(self):
        # The mirror of the case above: memory is the idle dimension, it clears
        # the 0.125 GiB materiality floor only because there are two replicas,
        # and 64Mi each is exactly the resize floor.
        pods = [self.pod(name=f"api-{i}", cpu_req="1", mem_req="64Mi") for i in (1, 2)]
        peaks = {("default", f"api-{i}"): (0.9, 10.0) for i in (1, 2)}
        self.assertEqual(self.hits(pods, peaks), [])

    def test_the_resize_floors_match_the_sop(self):
        self.assertEqual(fw.OVERREQUEST_RESIZE_FLOOR_VCPU, 0.05)
        self.assertEqual(fw.OVERREQUEST_RESIZE_FLOOR_MIB, 64.0)
        self.assertEqual(fw.OVERREQUEST_PEAK_MULTIPLIER, 2)

    def test_the_gate_is_evaluated_per_replica_and_not_on_the_totals(self):
        """Ten replicas on the floor total 500m, which shrinks; each does not.

        Reading the gate off the summed request would report the largest
        instance of exactly the shape it exists to drop -- and the
        recommendation the reader then gets is `50m`, which is what all ten
        pods already declare.
        """
        pods = [self.pod(name=f"hw-{i}", owner_name="hello-world", cpu_req="50m", mem_req="64Mi") for i in range(10)]
        peaks = {("default", f"hw-{i}"): (0.005, 9.5) for i in range(10)}
        self.assertEqual(self.hits(pods, peaks), [])
        self.assertFalse(
            fw._resize_shrinks_request(0.5, 0.05, 10, floor=fw.OVERREQUEST_RESIZE_FLOOR_VCPU, unit=0.001)
        )
        # The same totals read as a single replica do shrink, which is what a
        # totals-based gate would have computed.
        self.assertTrue(
            fw._resize_shrinks_request(0.5, 0.05, 1, floor=fw.OVERREQUEST_RESIZE_FLOOR_VCPU, unit=0.001)
        )

    def test_a_replica_count_of_zero_is_not_a_division(self):
        self.assertFalse(
            fw._resize_shrinks_request(1.0, 0.0, 0, floor=fw.OVERREQUEST_RESIZE_FLOOR_VCPU, unit=0.001)
        )

    def test_a_request_below_the_floor_is_a_raise_and_not_a_shrink(self):
        # 10m per replica is under the 50m the rule would resize it to, so the
        # answer is not "shrink by a little", it is "do not touch this".
        self.assertFalse(
            fw._resize_shrinks_request(0.01, 0.0, 1, floor=fw.OVERREQUEST_RESIZE_FLOOR_VCPU, unit=0.001)
        )


class IdleWorkloadTest(unittest.TestCase):
    """§3.13 -- the population §3.1 measures correctly and then will not act on.

    Every fixture here is a controller `check_overrequest` sees, agrees is
    idle, and proposes no resize for, on one of three arms: the floor-bound
    arm, where `ceil(peak x 2)` clamps up to the 50m/64Mi (or LimitRange
    default) it already declares; the materiality arm, where the only
    dimension it could shrink would save less than 100m/128Mi; and the
    `Guaranteed` arm, where §3.1 declines to turn a week of idleness into a
    limit. `test_the_partition_with_overrequest_is_exact` is the load-bearing
    one: the two checks must never both fire on an object, or the report asks
    a reader to shrink and delete the same Deployment.
    """

    NS = "hello-world"
    HASH = "7d9f8c6b45"
    POD = "hello-world-7d9f8c6b45-1"
    # 0.2% of 50m and 9% of 64Mi -- the live shape of all four Deployments this
    # check found on the 2026-09-06 fleet.
    IDLE = {(NS, POD): (0.0021, 6.0)}

    def pod(
        self,
        name=POD,
        *,
        ns=NS,
        cpu_req="50m",
        mem_req="64Mi",
        cpu_lim=None,
        mem_lim=None,
        started="2026-07-25T00:00:00Z",
        owner_kind="ReplicaSet",
        owner_name="hello-world-7d9f8c6b45",
        labels=None,
    ):
        requests = {}
        if cpu_req:
            requests["cpu"] = cpu_req
        if mem_req:
            requests["memory"] = mem_req
        # Omitted entirely rather than empty: a pod with no `limits` key is
        # Burstable, which is what every fixture predating the Guaranteed arm
        # is and must stay.
        resources = {"requests": requests}
        limits = {}
        if cpu_lim:
            limits["cpu"] = cpu_lim
        if mem_lim:
            limits["memory"] = mem_lim
        if limits:
            resources["limits"] = limits
        return obj(
            "Pod",
            name,
            ns=ns,
            **{
                "spec.containers": [{"resources": resources}],
                "status.startTime": started,
                "status.phase": "Running",
                "metadata.labels": {"app": "hello-world", "pod-template-hash": self.HASH} if labels is None else labels,
                "metadata.ownerReferences": [{"kind": owner_kind, "name": owner_name}],
            },
        )

    def guaranteed_pod(self, *, cpu="500m", mem="2Gi", **kw):
        """The live shape: `ai-inference-hardened`, requests == limits."""
        return self.pod(cpu_req=cpu, mem_req=mem, cpu_lim=cpu, mem_lim=mem, **kw)

    def controller(self, kind="Deployment", name="hello-world", *, ns=NS, created="2026-07-01T00:00:00Z"):
        return obj(kind, name, ns=ns, **{"metadata.creationTimestamp": created})

    def svc(self, name="hello-world", *, ns=NS, kind="LoadBalancer", selector=None, ip=None):
        fields = {
            "spec.type": kind,
            "spec.selector": {"app": "hello-world"} if selector is None else selector,
        }
        # Only when asked. A Service still waiting on an address has no
        # `status.loadBalancer.ingress` at all, and that is the shape every
        # fixture predating the traffic read carries.
        if ip:
            fields["status.loadBalancer.ingress"] = [{"ip": ip}]
        return obj("Service", name, ns=ns, **fields)

    def context(self, pods=None, controllers=None, services=()):
        pods = [self.pod()] if pods is None else pods
        controllers = [self.controller()] if controllers is None else controllers
        return {
            "pods": pods,
            "services": list(services),
            "deployments": [c for c in controllers if c["kind"] == "Deployment"],
            "statefulsets": [c for c in controllers if c["kind"] == "StatefulSet"],
        }

    def hits(self, peaks=None, *, lb_traffic=None, **kw):
        return fw.check_idle_workload(
            self.context(**kw),
            self.IDLE if peaks is None else peaks,
            now=NOW,
            lb_traffic=lb_traffic,
        )

    def test_flags_a_floor_bound_deployment_nothing_is_using(self):
        hits = self.hits()
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "Deployment/hello-world")
        self.assertEqual(hits[0]["namespace"], self.NS)
        self.assertEqual(hits[0]["severity"], "minor")
        # The three things a reader needs to act: what it holds, that nobody
        # used it, and that no resize is the answer.
        self.assertIn("requests 0.050 vCPU / 64 MiB", hits[0]["excerpt"])
        self.assertIn("Declared 31 days ago", hits[0]["excerpt"])
        self.assertIn("no resize can reclaim", hits[0]["excerpt"])

    def test_a_request_on_a_native_sidecar_alone_makes_the_controller_sized(self):
        """Sized on the sidecar, so §3.13 judges it and §3.12 does not."""
        pod = self.pod(cpu_req=None, mem_req=None)
        pod["spec"]["initContainers"] = [
            {"name": "proxy", "restartPolicy": "Always", "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}}},
        ]
        [hit] = self.hits(pods=[pod])
        self.assertIn("requests 0.050 vCPU / 64 MiB", hit["excerpt"])
        self.assertEqual(fw.check_unsized(self.context(pods=[pod]), self.IDLE, now=NOW, autopilot=False), [])

    def test_an_hpa_target_is_not_idle(self):
        """§3.13 applies §3.1's exclusions, the HPA one included: the HPA holds
        its target at `minReplicas`, so a merged `replicas: 0` is undone on
        the next sync."""
        hpa = obj("HorizontalPodAutoscaler", "hpa", ns=self.NS, **{"spec.scaleTargetRef": {"kind": "Deployment", "name": "hello-world"}})
        ctx = {**self.context(), "hpas": [hpa]}
        self.assertEqual(fw.check_idle_workload(ctx, self.IDLE, now=NOW), [])
        # Control: the same controller with no HPA is reported.
        self.assertEqual(len(self.hits()), 1)

    def test_a_sub_floor_controller_is_not_described_as_on_the_floor(self):
        hits = self.hits({(self.NS, self.POD): (0.001, 3.0)}, pods=[self.pod(cpu_req="10m", mem_req="32Mi")])
        self.assertEqual(len(hits), 1)
        self.assertIn("already at or below the 50m/64Mi floor", hits[0]["excerpt"])

    def test_the_partition_with_overrequest_is_exact(self):
        """Neither check may stay silent on a controller, nor both speak.

        The four Deployments that motivated this check were already reported
        by §3.1 until `bc437731` dropped them for having a no-op remediation.
        If the two predicates ever overlap, the same object comes back with
        one finding saying "resize it" and another saying "delete it".
        """
        floor_bound = self.context()
        # Same controller, same idleness, a request with somewhere to go: 2
        # vCPU / 4 GiB against a 100m / 100 MiB peak resizes to 200m / 200Mi.
        shrinkable = self.context(pods=[self.pod(cpu_req="2", mem_req="4Gi")])
        big_peak = {(self.NS, self.POD): (0.1, 100.0)}

        self.assertEqual(len(fw.check_idle_workload(floor_bound, self.IDLE, now=NOW)), 1)
        self.assertEqual(fw.check_overrequest(floor_bound, self.IDLE, now=NOW, autopilot=False), [])
        self.assertEqual(fw.check_idle_workload(shrinkable, big_peak, now=NOW), [])
        self.assertEqual(len(fw.check_overrequest(shrinkable, big_peak, now=NOW, autopilot=False)), 1)

    def test_the_partition_holds_between_the_resize_and_materiality_floors(self):
        """50m/100Mi is above §3.1's 64Mi resize floor and under its 128Mi
        materiality floor, so §3.1 drops it; this check deferred to §3.1 on the
        resize floor alone, and a month-idle controller got neither finding."""
        between = self.context(pods=[self.pod(mem_req="100Mi")])
        peak = {(self.NS, self.POD): (0.002, 6.0)}
        self.assertEqual(fw.check_overrequest(between, peak, now=NOW, autopilot=False), [])
        hits = fw.check_idle_workload(between, peak, now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertIn("under the 100m / 128Mi", hits[0]["excerpt"])

    def test_a_limitrange_defaulted_dimension_does_not_break_the_partition(self):
        """CPU filled in by the LimitRange, memory on the floor, both idle.

        §3.1 gives the defaulted CPU no verdict and the memory has nowhere to
        go, so it stays silent; this check must not then decline on the
        strength of a CPU resize §3.1 will never propose."""
        lr = obj("LimitRange", "limits", ns=self.NS, **{"spec.limits": [{"type": "Container", "defaultRequest": {"cpu": "2"}}]})
        ctx = {**self.context(pods=[self.pod(cpu_req="2")]), "limitranges": [lr]}
        self.assertEqual(fw.check_overrequest(ctx, self.IDLE, now=NOW, autopilot=False), [])
        hits = fw.check_idle_workload(ctx, self.IDLE, now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertIn("LimitRange default", hits[0]["excerpt"])

    def test_a_defaulted_cpu_beside_a_sub_material_memory_names_both_reasons(self):
        """CPU is the LimitRange's 250m, memory a hand-written 100Mi between
        the resize and materiality floors. "The request is under the 100m /
        128Mi" alone is false of the 250m CPU and hides the LimitRange."""
        lr = obj("LimitRange", "limits", ns=self.NS, **{"spec.limits": [{"type": "Container", "defaultRequest": {"cpu": "250m"}}]})
        ctx = {**self.context(pods=[self.pod(cpu_req="250m", mem_req="100Mi")]), "limitranges": [lr]}
        [hit] = fw.check_idle_workload(ctx, self.IDLE, now=NOW)
        self.assertIn(
            "The memory request is under the 100m / 128Mi a resize is worth proposing for, "
            "and the CPU request is the namespace LimitRange default",
            hit["excerpt"],
        )

    def test_a_defaulted_memory_beside_a_sub_material_cpu_names_both_reasons(self):
        lr = obj("LimitRange", "limits", ns=self.NS, **{"spec.limits": [{"type": "Container", "defaultRequest": {"memory": "1Gi"}}]})
        ctx = {**self.context(pods=[self.pod(cpu_req="80m", mem_req="1Gi")]), "limitranges": [lr]}
        [hit] = fw.check_idle_workload(ctx, self.IDLE, now=NOW)
        self.assertIn(
            "The CPU request is under the 100m / 128Mi a resize is worth proposing for, "
            "and the memory request is the namespace LimitRange default",
            hit["excerpt"],
        )

    def test_a_fully_idle_guaranteed_controller_stands_down_instead_of_resizing(self):
        """The `ai-inference` shape: §3.1 can resize it and refuses to.

        Requests equal limits at 0.50 vCPU / 2.0 GiB against a peak of nothing.
        `_resize_shrinks_request` is true on both dimensions -- 2 x nothing
        clamps to 50m/64Mi, well under what it declares -- so before this arm
        existed §3.1 took it and published `kind: manual`, and §3.13 stayed
        silent. Neither offered the pull request that `spec.replicas: 0` is.
        """
        ctx = self.context(pods=[self.guaranteed_pod()])
        peaks = {(self.NS, self.POD): (0.0, 0.0)}

        hits = fw.check_idle_workload(ctx, peaks, now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "Deployment/hello-world")
        # Graded on the reservation, not on the floor argument that grades the
        # other arm: half a vCPU idle for a month is not `minor`.
        self.assertEqual(hits[0]["severity"], "major")
        # And §3.1 must have let go of it, or the object reports twice.
        self.assertEqual(fw.check_overrequest(ctx, peaks, now=NOW, autopilot=False), [])

    def test_an_unlimited_init_container_makes_the_pod_burstable(self):
        # kubelet reads init containers, native sidecars among them, when it
        # sets QoS; reading `containers` alone called this pod Guaranteed and
        # took it from §3.1 for §3.13's stand-down.
        for restart in (None, "Always"):
            with self.subTest(restart_policy=restart):
                pod = self.guaranteed_pod()
                init = {"name": "proxy", "resources": {"requests": {"cpu": "10m"}}}
                if restart:
                    init["restartPolicy"] = restart
                pod["spec"]["initContainers"] = [init]
                ctx = self.context(pods=[pod])
                peaks = {(self.NS, self.POD): (0.0, 0.0)}
                self.assertEqual(fw.check_idle_workload(ctx, peaks, now=NOW), [])
                over = fw.check_overrequest(ctx, peaks, now=NOW, autopilot=False)
                self.assertEqual(len(over), 1)
                self.assertFalse(over[0].get("_guaranteed"))

    def test_a_limited_init_container_leaves_the_pod_guaranteed(self):
        pod = self.guaranteed_pod()
        pod["spec"]["initContainers"] = [{"name": "setup", "resources": {"limits": {"cpu": "100m", "memory": "64Mi"}}}]
        hits = fw.check_idle_workload(self.context(pods=[pod]), {(self.NS, self.POD): (0.0, 0.0)}, now=NOW)
        self.assertEqual(len(hits), 1)

    def test_the_guaranteed_excerpt_names_the_ceiling_not_the_floor(self):
        # Both arms answer "why is no resize offered", and they answer it
        # differently. Naming the 50m/64Mi floor on a controller declaring
        # 0.50 vCPU sends the reader to check something that is not true.
        hits = fw.check_idle_workload(
            self.context(pods=[self.guaranteed_pod()]), {(self.NS, self.POD): (0.0, 0.0)}, now=NOW
        )
        self.assertIn("enforcement ceiling", hits[0]["excerpt"])
        self.assertNotIn("50m/64Mi floor", hits[0]["excerpt"])

    def test_a_guaranteed_controller_idle_on_one_dimension_stays_overrequests(self):
        """The narrow case is idle on *every* dimension, and only that.

        A `Guaranteed` controller using all its memory and none of its CPU is
        not unused -- one number is wrong. §3.1's `manual` note is the right
        answer there and a stand-down would be destructive.
        """
        ctx = self.context(pods=[self.guaranteed_pod()])
        # 0% of the CPU, 95% of the 2 GiB.
        peaks = {(self.NS, self.POD): (0.0, 1945.6)}
        self.assertEqual(fw.check_idle_workload(ctx, peaks, now=NOW), [])
        over = fw.check_overrequest(ctx, peaks, now=NOW, autopilot=False)
        self.assertEqual(len(over), 1)
        self.assertTrue(over[0]["_guaranteed"])

    def test_a_guaranteed_controller_under_the_age_bar_stays_overrequests(self):
        """§3.13's fortnight is not waived by the Guaranteed arm.

        The two live findings were six days old when this was written. A
        stand-down proposed against a workload somebody deployed on Monday is
        the recommendation this age bar exists to refuse, so §3.1 keeps it and
        the `manual` note stands until it has been idle long enough.
        """
        ctx = self.context(
            pods=[self.guaranteed_pod()],
            controllers=[self.controller(created="2026-07-27T00:00:00Z")],
        )
        peaks = {(self.NS, self.POD): (0.0, 0.0)}
        self.assertEqual(fw.check_idle_workload(ctx, peaks, now=NOW), [])
        self.assertEqual(len(fw.check_overrequest(ctx, peaks, now=NOW, autopilot=False)), 1)

    def test_a_small_guaranteed_reservation_is_still_minor(self):
        # The severity split is on the size of what is held, not on the QoS
        # class: a Guaranteed controller sitting on 50m/64Mi has given up as
        # little as the floor-bound one beside it.
        ctx = self.context(pods=[self.guaranteed_pod(cpu="50m", mem="64Mi")])
        hits = fw.check_idle_workload(ctx, self.IDLE, now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "minor")

    def test_one_shrinkable_dimension_sends_the_whole_controller_to_overrequest(self):
        # Either dimension having somewhere to go is enough, so both halves of
        # the guard are checked: §3.1 reports the roomy one and says the other
        # is already floored, and this check must not also claim the object.
        for cpu_req, mem_req, peak in (("2", "64Mi", (0.1, 6.0)), ("50m", "4Gi", (0.0021, 100.0))):
            with self.subTest(cpu=cpu_req, mem=mem_req):
                pods = [self.pod(cpu_req=cpu_req, mem_req=mem_req)]
                peaks = {(self.NS, self.POD): peak}
                self.assertEqual(fw.check_idle_workload(self.context(pods=pods), peaks, now=NOW), [])
                self.assertEqual(
                    len(fw.check_overrequest(self.context(pods=pods), peaks, now=NOW, autopilot=False)), 1
                )

    def test_a_dimension_in_use_vetoes_the_finding(self):
        """`cert-manager`'s shape: 17% of its CPU, 95% of its memory.

        §3.1 is per-dimension because a copy-pasted CPU request beside a
        carefully sized memory one is the common waste. This check is not: the
        claim is "nothing uses this", and something using all of its memory
        refutes it whatever the CPU says.
        """
        busy = {(self.NS, self.POD): (0.0085, 60.8)}
        self.assertEqual(self.hits(peaks=busy), [])
        # Control: the same request and the same CPU, memory idle -> reported.
        self.assertEqual(len(self.hits(peaks={(self.NS, self.POD): (0.0085, 6.0)})), 1)

    def test_a_dimension_the_controller_does_not_declare_is_skipped_not_divided(self):
        # No CPU request at all. The ratio has no denominator, and treating the
        # absence as a failed test would silence every memory-only controller.
        pods = [self.pod(cpu_req=None)]
        self.assertEqual(len(fw.check_idle_workload(self.context(pods=pods), self.IDLE, now=NOW)), 1)

    def test_the_age_gate_reads_the_controller_and_not_its_pods(self):
        """The regression that made the first live run of this check return zero.

        GKE recreates a pod on every node upgrade, so the four Deployments
        here ran untouched for a month behind pods under eight days old. A
        fortnight gate on `status.startTime` excludes all four -- and goes on
        excluding them, since nothing on a managed platform keeps a pod that
        long.
        """
        young_pod = [self.pod(started="2026-07-30T00:00:00Z")]  # 2 days
        self.assertEqual(len(fw.check_idle_workload(self.context(pods=young_pod), self.IDLE, now=NOW)), 1)
        # And the converse: a month-old pod under a Deployment declared
        # yesterday is a new workload nobody has had a chance to use yet.
        new_controller = [self.controller(created="2026-07-31T00:00:00Z")]
        self.assertEqual(
            fw.check_idle_workload(self.context(controllers=new_controller), self.IDLE, now=NOW), []
        )

    def test_a_controller_the_dump_does_not_carry_is_skipped(self):
        # No age to test, and this check does not guess one from the pods. The
        # safe direction is silence: the cost of that is another month of a
        # 50m reservation, the cost of a wrong guess is "delete this".
        self.assertEqual(fw.check_idle_workload(self.context(controllers=[]), self.IDLE, now=NOW), [])

    def test_a_statefulset_is_judged_the_same_way(self):
        pods = [self.pod(owner_kind="StatefulSet", owner_name="hello-world", labels={"app": "hello-world"})]
        controllers = [self.controller(kind="StatefulSet")]
        hits = fw.check_idle_workload(self.context(pods=pods, controllers=controllers), self.IDLE, now=NOW)
        self.assertEqual([h["object"] for h in hits], ["StatefulSet/hello-world"])

    def test_an_unmeasured_controller_is_not_reported(self):
        # Absent reads as zero on every dimension, which is maximally idle --
        # so a metrics agent down on one node would otherwise produce a
        # recommendation to delete whatever was running there.
        self.assertEqual(self.hits(peaks={(self.NS, "somebody-else"): (4.0, 8192.0)}), [])
        self.assertEqual(len(self.hits()), 1)

    def test_a_controller_unmeasured_on_one_dimension_is_not_reported(self):
        # Idle on every dimension needs every dimension read.
        self.assertEqual(self.hits(peaks={(self.NS, self.POD): (0.0021, None)}), [])

    def test_no_usage_answer_at_all_reports_nothing(self):
        self.assertEqual(self.hits(peaks={}), [])

    def test_a_load_balancer_in_front_of_it_is_major(self):
        """The forwarding rule is the larger half of the bill.

        Three of these on the 2026-09-06 fleet held about $17/month of
        Autopilot pod charges between them while their three L4 rules ran
        about $66. No check joined the two, so the bigger number reached
        neither the finding nor its severity.
        """
        hits = self.hits(services=[self.svc()])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")
        self.assertIn("Service/hello-world still fronts it", hits[0]["excerpt"])
        self.assertIn("external IP", hits[0]["excerpt"])

    def test_an_internal_load_balancer_claims_no_external_ip(self):
        """An internal Service holds a forwarding rule and no external IP, by
        either annotation GKE accepts. Grading is unchanged: still `major`."""
        for key in fw.INTERNAL_LB_ANNOTATIONS:
            with self.subTest(annotation=key):
                svc = self.svc()
                svc["metadata"]["annotations"] = {key: fw.INTERNAL_LB_ANNOTATION_VALUE}
                hits = self.hits(services=[svc])
                self.assertEqual(hits[0]["severity"], "major")
                self.assertIn("so an internal forwarding rule bills for it too", hits[0]["excerpt"])
                self.assertNotIn("external IP", hits[0]["excerpt"])
        internal = self.svc(name="ilb")
        internal["metadata"]["annotations"] = {fw.INTERNAL_LB_ANNOTATIONS[0]: fw.INTERNAL_LB_ANNOTATION_VALUE}
        mixed = self.hits(services=[internal, self.svc()])[0]["excerpt"]
        self.assertIn("forwarding rules, and external IPs for the external ones, bill", mixed)

    def test_a_load_balancer_selecting_something_else_is_not_fronting(self):
        self.assertEqual(self.hits(services=[self.svc(selector={"app": "other"})])[0]["severity"], "minor")

    def test_a_selectorless_service_is_not_fronting(self):
        # Hand-written Endpoints feed it. Kubernetes matches nothing on an
        # empty selector, and `all()` over no pairs is True -- so without the
        # emptiness guard every idle workload in the namespace would inherit
        # somebody else's load balancer and its severity.
        self.assertEqual(self.hits(services=[self.svc(selector={})])[0]["severity"], "minor")

    def test_a_cluster_ip_service_holds_no_forwarding_rule(self):
        self.assertEqual(self.hits(services=[self.svc(kind="ClusterIP")])[0]["severity"], "minor")

    def test_a_load_balancer_in_another_namespace_is_not_fronting(self):
        self.assertEqual(self.hits(services=[self.svc(ns="elsewhere")])[0]["severity"], "minor")

    def test_a_partial_selector_still_matches(self):
        # Kubernetes' rule, not a set equality: every pair of the selector has
        # to be on the pod, and the pod carries `pod-template-hash` besides.
        self.assertEqual(self.hits(services=[self.svc(selector={"app": "hello-world"})])[0]["severity"], "major")

    def test_replicas_are_summed_and_the_excerpt_says_how_many(self):
        pods = [self.pod(name=f"hello-world-7d9f8c6b45-{i}") for i in range(3)]
        peaks = {(self.NS, f"hello-world-7d9f8c6b45-{i}"): (0.0021, 6.0) for i in range(3)}
        hits = fw.check_idle_workload(self.context(pods=pods), peaks, now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertIn("requests 0.150 vCPU / 192 MiB across 3 replicas", hits[0]["excerpt"])

    def test_the_excerpt_states_the_window_it_really_measured(self):
        """Age and window answer different questions and can disagree by weeks.

        A Deployment declared 31 days ago whose pod rolled 12 hours ago has 12
        hours of history, and the finding may not claim to have watched a
        month of idleness it never saw.
        """
        pods = [self.pod(started="2026-07-31T12:00:00Z")]
        hits = fw.check_idle_workload(self.context(pods=pods), self.IDLE, now=NOW)
        self.assertIn("Declared 31 days ago", hits[0]["excerpt"])
        self.assertIn("trailing 12h", hits[0]["excerpt"])
        self.assertIn("under a full daily cycle", hits[0]["excerpt"])

    def test_a_service_selecting_it_marks_the_fix_for_triage(self):
        """The gate the 2026-09-07 incident bought.

        Three findings of this shape auto-promoted and merged unattended,
        standing down three Deployments; two sat behind forwarding rules that
        had metered 837,460 and 785,748 inbound packets across the very week
        the finding quoted. Nothing in this check measures a call -- it reads
        CPU and memory -- so the marker is what stops the sweep deciding.
        """
        hits = self.hits(services=[self.svc()])
        self.assertEqual(hits[0]["_selected_by"], ["Service/hello-world"])

    def test_a_cluster_ip_service_marks_it_too_and_stays_minor(self):
        """The reason `_selecting_services` is not `_fronting_load_balancers`.

        A `ClusterIP` Service is free, so it must not lift the grade -- and it
        is still a stable name something in the cluster may be calling, so the
        stand-down must not open itself.
        """
        hits = self.hits(services=[self.svc(kind="ClusterIP")])
        self.assertEqual(hits[0]["severity"], "minor")
        self.assertEqual(hits[0]["_selected_by"], ["Service/hello-world"])
        self.assertIn("Service/hello-world also selects its pods", hits[0]["excerpt"])
        self.assertIn("no endpoints", hits[0]["excerpt"])
        self.assertIn("not whether anything still resolves that name", hits[0]["excerpt"])

    def test_a_load_balancer_is_not_named_twice(self):
        """Both clauses fire on a `LoadBalancer`, and they say different things
        -- one about the bill, one about the endpoints. Naming the Service in
        each reads as two findings about two objects."""
        excerpt = self.hits(services=[self.svc()])[0]["excerpt"]
        self.assertEqual(excerpt.count("Service/hello-world"), 1)
        self.assertIn("That Service selects its pods", excerpt)

    def test_nothing_selecting_it_leaves_the_fix_unmarked(self):
        hits = self.hits()
        self.assertEqual(hits[0]["_selected_by"], [])
        self.assertNotIn("no endpoints", hits[0]["excerpt"])

    def test_a_service_selecting_something_else_does_not_mark_it(self):
        self.assertEqual(
            self.hits(services=[self.svc(selector={"app": "other"})])[0]["_selected_by"], []
        )

    def test_a_selectorless_service_does_not_mark_it(self):
        # `all()` over no pairs is True, so without the emptiness guard a
        # hand-fed Service would freeze every stand-down in its namespace.
        self.assertEqual(self.hits(services=[self.svc(selector={})])[0]["_selected_by"], [])

    def test_a_service_in_another_namespace_does_not_mark_it(self):
        self.assertEqual(self.hits(services=[self.svc(ns="elsewhere")])[0]["_selected_by"], [])

    def test_the_impact_no_longer_claims_nobody_is_calling_it(self):
        """The sentence itself. It read "a workload nobody is calling" for
        eleven days over a check that has never measured a call."""
        impact = fw.IMPACT["idle-workload"]
        self.assertNotIn("nobody is calling", impact)
        self.assertIn("packets are not sessions", impact)

    def test_the_impact_claims_no_idle_span_longer_than_it_read(self):
        """The read covers at most `USAGE_WINDOW_HOURS`, often less; the
        controller's 14-day age is not observed idleness."""
        impact = fw.IMPACT["idle-workload"]
        self.assertNotIn("weeks", impact)
        self.assertIn("measured window", impact)

    def test_the_impact_states_the_peak_rule_rather_than_no_use(self):
        """The check passes a controller peaking at up to 20% of its
        requests; "nothing has used" it is not what was measured."""
        impact = fw.IMPACT["idle-workload"]
        self.assertNotIn("Nothing has used", impact)
        self.assertIn(f"at or below {fw.IDLE_WORKLOAD_UTILISATION:.0%} of its requests on every dimension", impact)

    # ----------------------------------------------------------------- #
    # The traffic clause. Every one of these turns on the same question:
    # what the excerpt is entitled to say about a forwarding rule it has,
    # has not, or cannot measure.
    # ----------------------------------------------------------------- #

    IP = "34.186.100.26"

    def traffic(self, *, ingress=None, egress_packets=None, egress_bytes=None, ip=None, rule="a-rule"):
        return {
            ip or self.IP: {
                "rule": rule,
                "ingress_packets": ingress,
                "egress_packets": egress_packets,
                "egress_bytes": egress_bytes,
            }
        }

    def test_a_busy_rule_serving_no_payload_says_so(self):
        """The live shape of the two findings that merged a stand-down.

        374,158 inbound packets answered at 63.9 bytes each -- busy enough to
        look like use and too small to be a response. Before this the excerpt
        said nothing at all and the impact said nobody was calling it.
        """
        hits = self.hits(
            services=[self.svc(ip=self.IP)],
            lb_traffic=self.traffic(ingress=374158, egress_packets=273430, egress_bytes=17459228),
        )
        excerpt = hits[0]["excerpt"]
        self.assertIn("metered 374,158 inbound packets over 168h", excerpt)
        self.assertIn("17,459,228 bytes across 273,430 outbound packets, 64 bytes each", excerpt)
        self.assertIn("without ever sending a payload", excerpt)
        self.assertIn("unsolicited connection attempts", excerpt)

    def test_a_rule_serving_real_payload_warns_against_the_stand_down(self):
        hits = self.hits(
            services=[self.svc(ip=self.IP)],
            lb_traffic=self.traffic(ingress=200000, egress_packets=100000, egress_bytes=120000000),
        )
        excerpt = hits[0]["excerpt"]
        self.assertIn("1,200 bytes each", excerpt)
        self.assertIn("something is being served", excerpt)
        self.assertIn("measured a forwarding rule, not a caller", excerpt)

    def test_traffic_under_the_floor_reads_as_background(self):
        hits = self.hits(
            services=[self.svc(ip=self.IP)],
            lb_traffic=self.traffic(ingress=812, egress_packets=400, egress_bytes=25000),
        )
        excerpt = hits[0]["excerpt"]
        self.assertIn("metered 812 inbound packets over 168h", excerpt)
        self.assertIn("under the 10,000-packet floor", excerpt)
        self.assertIn("nothing measurable reached it", excerpt)
        # The payload ratio is 62.5 bytes here too, and saying so would be an
        # observation about 400 packets. Below the floor there is nothing to
        # characterise.
        self.assertNotIn("bytes each", excerpt)

    def test_a_rule_that_answered_nothing_says_that(self):
        hits = self.hits(
            services=[self.svc(ip=self.IP)],
            lb_traffic=self.traffic(ingress=50000, egress_packets=0, egress_bytes=0),
        )
        self.assertIn("answered none of them", hits[0]["excerpt"])

    def test_an_absent_egress_series_is_unmeasured_not_unanswered(self):
        hits = self.hits(services=[self.svc(ip=self.IP)], lb_traffic=self.traffic(ingress=50000))
        excerpt = hits[0]["excerpt"]
        self.assertIn("metered 50,000 inbound packets", excerpt)
        self.assertIn("what it answered is unmeasured", excerpt)
        self.assertNotIn("answered none", excerpt)

    def test_an_absent_bytes_series_is_not_read_as_zero_bytes_each(self):
        hits = self.hits(services=[self.svc(ip=self.IP)], lb_traffic=self.traffic(ingress=50000, egress_packets=3000))
        excerpt = hits[0]["excerpt"]
        self.assertIn("answered with 3,000 outbound packets; their payload is unmeasured", excerpt)
        self.assertNotIn("bytes each", excerpt)

    def test_an_unmeasured_rule_is_not_reported_as_quiet(self):
        """The distinction the whole read exists for.

        Monitoring holding no series for a rule is the answer §3.13 used to
        treat as silence -- implicitly, by never asking. A zero here would be
        a fabricated measurement.
        """
        hits = self.hits(services=[self.svc(ip=self.IP)], lb_traffic=self.traffic())
        excerpt = hits[0]["excerpt"]
        self.assertIn("no Cloud Monitoring traffic series", excerpt)
        self.assertIn("unmeasured rather than zero", excerpt)
        self.assertNotIn("metered", excerpt)

    def test_an_unmeasured_rule_beside_a_quiet_one_is_not_counted_as_quiet(self):
        """Two Services in front of one controller, one rule with no series.

        The 812 packets belong to the measured rule alone, and "nothing
        measurable reached it" would claim the unmeasured one carried nothing.
        """
        other = "35.245.254.69"
        traffic = {**self.traffic(ingress=812, egress_packets=400, egress_bytes=25000), **self.traffic(ip=other, rule="b-rule")}
        hits = self.hits(services=[self.svc(ip=self.IP), self.svc("second", ip=other)], lb_traffic=traffic)
        excerpt = hits[0]["excerpt"]
        self.assertIn("1 of the 2 forwarding rules in front of it metered 812 inbound packets", excerpt)
        self.assertIn("the other 1 carries no Cloud Monitoring traffic series", excerpt)
        self.assertIn("unmeasured rather than zero", excerpt)
        self.assertNotIn("nothing measurable reached it", excerpt)

    def test_every_measured_shape_names_the_unmeasured_rule(self):
        """The mixed clause follows each of the measured rule's answers, not
        only the under-the-floor one above."""
        other = "35.245.254.69"
        unmeasured = "; the other 1 carries no Cloud Monitoring traffic series, so what reached it is unmeasured rather than zero"
        shapes = {
            "outbound unmeasured": ({"egress_packets": None}, "because Cloud Monitoring holds no outbound series for the rule" + unmeasured),
            "answered none": ({"egress_packets": 0}, "and answered none of them" + unmeasured),
            "payload unmeasured": ({"egress_packets": 3000, "egress_bytes": None}, "their payload is unmeasured, because Cloud Monitoring holds no outbound series for the rule" + unmeasured),
            "no payload": ({"egress_packets": 3000, "egress_bytes": 3000 * 60}, "rather than of sessions" + unmeasured),
            "payload served": ({"egress_packets": 3000, "egress_bytes": 3000 * 900}, "something is being served" + unmeasured + ". Find out what"),
        }
        for label, (egress, expected) in shapes.items():
            with self.subTest(label):
                traffic = {**self.traffic(ingress=50000, **egress), **self.traffic(ip=other, rule="b-rule")}
                excerpt = self.hits(services=[self.svc(ip=self.IP), self.svc("second", ip=other)], lb_traffic=traffic)[0]["excerpt"]
                self.assertIn("1 of the 2 forwarding rules in front of it metered 50,000 inbound packets", excerpt)
                self.assertIn(expected, excerpt)

    def test_no_traffic_read_at_all_writes_no_clause(self):
        # `lb_traffic=None` is a run with no Monitoring session, or the
        # collector invoked by hand. Silence, not a zero and not a caveat.
        excerpt = self.hits(services=[self.svc(ip=self.IP)])[0]["excerpt"]
        self.assertNotIn("forwarding rule metered", excerpt)
        self.assertNotIn("unmeasured", excerpt)

    def test_an_address_the_read_does_not_cover_writes_no_clause(self):
        hits = self.hits(
            services=[self.svc(ip="35.245.254.69")],
            lb_traffic=self.traffic(ingress=374158, egress_packets=1, egress_bytes=1),
        )
        self.assertNotIn("metered", hits[0]["excerpt"])

    def test_a_service_with_no_address_yet_writes_no_clause(self):
        hits = self.hits(
            services=[self.svc()],
            lb_traffic=self.traffic(ingress=374158, egress_packets=1, egress_bytes=1),
        )
        self.assertNotIn("metered", hits[0]["excerpt"])

    def test_a_cluster_ip_service_never_reaches_the_traffic_read(self):
        # A `ClusterIP` Service holds no forwarding rule, so even an address
        # the read covers is not this workload's load balancer.
        hits = self.hits(
            services=[self.svc(kind="ClusterIP", ip=self.IP)],
            lb_traffic=self.traffic(ingress=374158, egress_packets=1, egress_bytes=1),
        )
        self.assertNotIn("metered", hits[0]["excerpt"])

    def test_two_rules_are_summed_and_named_in_the_plural(self):
        second = "34.186.110.29"
        traffic = self.traffic(ingress=20000, egress_packets=10000, egress_bytes=400000)
        traffic.update(self.traffic(ingress=30000, egress_packets=10000, egress_bytes=600000, ip=second, rule="b-rule"))
        hits = self.hits(
            services=[self.svc(ip=self.IP), self.svc(name="hello-world-2", ip=second)],
            lb_traffic=traffic,
        )
        excerpt = hits[0]["excerpt"]
        self.assertIn("The 2 forwarding rules in front of it metered 50,000 inbound packets", excerpt)
        self.assertIn("1,000,000 bytes across 20,000 outbound packets, 50 bytes each", excerpt)

    def test_the_traffic_clause_precedes_the_endpoints_clause(self):
        """Order is the argument: what it costs, what it carried, what needs it."""
        excerpt = self.hits(
            services=[self.svc(ip=self.IP)],
            lb_traffic=self.traffic(ingress=374158, egress_packets=273430, egress_bytes=17459228),
        )[0]["excerpt"]
        self.assertLess(excerpt.index("still fronts it"), excerpt.index("metered"))
        self.assertLess(excerpt.index("metered"), excerpt.index("no endpoints"))

    def test_traffic_does_not_move_the_severity_or_the_triage_marker(self):
        """Measuring the rule does not decide the stand-down; a reader does.

        A rule serving real payload is the strongest reason not to promote
        this finding, and a rule serving none is not a reason to promote it --
        10,000 packets a week is a floor for noise, not evidence that nothing
        needs the workload. Both stay `major`, both stay marked.
        """
        for label, traffic in (
            ("payload", self.traffic(ingress=200000, egress_packets=100000, egress_bytes=120000000)),
            ("scanning", self.traffic(ingress=374158, egress_packets=273430, egress_bytes=17459228)),
            ("quiet", self.traffic(ingress=1, egress_packets=1, egress_bytes=1)),
            ("unmeasured", self.traffic()),
        ):
            with self.subTest(label):
                hits = self.hits(services=[self.svc(ip=self.IP)], lb_traffic=traffic)
                self.assertEqual(hits[0]["severity"], "major")
                self.assertEqual(hits[0]["_selected_by"], ["Service/hello-world"])

    def test_the_thresholds_match_the_sop(self):
        self.assertEqual(fw.IDLE_WORKLOAD_UTILISATION, 0.2)
        self.assertEqual(fw.IDLE_WORKLOAD_MIN_AGE_DAYS, 14)
        self.assertEqual(fw.IDLE_SERVICE_TRIAGE, "service-fronted")
        self.assertEqual(fw.LB_TRAFFIC_MIN_PACKETS, 10000)
        self.assertEqual(fw.LB_TRAFFIC_PAYLOAD_BYTES_PER_PACKET, 100)
        # The qualifier is the constant. `metric.labels.forwarding_rule_name`
        # is accepted by the API and attributes the project's whole traffic to
        # one rule.
        self.assertEqual(fw.LB_RULE_LABEL, "resource.labels.forwarding_rule_name")


class UnderrequestTest(unittest.TestCase):
    """§3.11 -- the other direction of the same sizing edit as `overrequest`."""

    def pod(self, ns="kubeagents-system", name="litellm-1", mem_req="512Mi", mem_lim="2Gi",
            owner_name="litellm", **kwargs):
        return OverrequestTest().deployment_pod(
            ns=ns, name=name, cpu_req="100m", mem_req=mem_req,
            cpu_lim="500m", mem_lim=mem_lim, owner_name=owner_name, **kwargs
        )

    def check(self, pods, means, peaks=None):
        peaks = peaks or {k: (0.05, v) for k, v in means.items()}
        return fw.check_underrequest({"pods": pods}, peaks, means, now=NOW)

    def test_flags_a_controller_averaging_above_its_request(self):
        """`litellm` on the live fleet, and the finding this check was built
        for: 512Mi requested per replica against a ~0.95 GiB sustained mean, on
        the proxy every agent call in the install traverses."""
        pods = [self.pod(name="litellm-1"), self.pod(name="litellm-2")]
        means = {("kubeagents-system", "litellm-1"): 973.0, ("kubeagents-system", "litellm-2"): 973.0}
        hits = self.check(pods, means)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "ReplicaSet/litellm")
        self.assertEqual(hits[0]["severity"], "major")
        self.assertIn("190% of request", hits[0]["excerpt"])

    def test_an_unread_peak_sizes_the_raise_from_the_mean(self):
        """A memory series missing from the peak read is unmeasured, not zero.
        Read as zero, the raise was sized at the 64Mi floor -- "Raise the
        memory request to 64Mi" under a 512Mi request the workload already
        exceeds. The mean stands in, since a peak is never below its mean."""
        pod = self.pod()
        means = {("kubeagents-system", "litellm-1"): 973.0}
        peaks = {("kubeagents-system", "litellm-1"): (0.05, None)}
        hits = self.check([pod], means, peaks)
        self.assertEqual(len(hits), 1)
        self.assertIn("peak not read", hits[0]["excerpt"])
        self.assertNotIn("64Mi", hits[0]["excerpt"])
        self.assertIn("1265Mi", hits[0]["excerpt"])  # ceil(973 x 1.3)

    def test_an_unread_peak_on_two_replicas_prints_no_per_replica_peak(self):
        """The per-replica clause printed the mean relabelled as a peak a
        sentence after "peak not read"."""
        pods = [self.pod(name="litellm-1"), self.pod(name="litellm-2")]
        means = {("kubeagents-system", "litellm-1"): 973.0, ("kubeagents-system", "litellm-2"): 973.0}
        peaks = {k: (0.05, None) for k in means}
        [hit] = self.check(pods, means, peaks)
        self.assertIn("peak not read", hit["excerpt"])
        self.assertIn("GiB mean, and the manifest change is per replica", hit["excerpt"])
        self.assertNotIn("GiB peak", hit["excerpt"])

    def with_sidecar(self, pod, mem_req="512Mi", mem_lim=None, app="app", sidecar="proxy"):
        """A native sidecar: an init container with `restartPolicy: Always`,
        whose usage Cloud Monitoring sums into the pod's with the app's."""
        pod["spec"]["containers"][0]["name"] = app
        resources = {"requests": {"memory": mem_req}} if mem_req else {}
        if mem_lim:
            resources["limits"] = {"memory": mem_lim}
        pod["spec"]["initContainers"] = [{"name": sidecar, "restartPolicy": "Always", "resources": resources}]
        return pod

    def test_a_native_sidecars_request_is_set_against_the_usage_it_adds(self):
        """512Mi app plus a 1Gi sidecar is 1.5Gi requested against a 973Mi
        mean: under the request, where the app's 512Mi alone read as 190%."""
        pod = self.with_sidecar(self.pod(), mem_req="1Gi")
        self.assertEqual(self.check([pod], {("kubeagents-system", "litellm-1"): 973.0}), [])

    def test_a_native_sidecar_with_no_memory_request_makes_the_mean_unattributable(self):
        pod = self.with_sidecar(self.pod(), mem_req=None)
        self.assertEqual(self.check([pod], {("kubeagents-system", "litellm-1"): 973.0}), [])

    def test_the_raise_lands_on_the_app_container_net_of_the_sidecar(self):
        """The sidecar requests more than the app, and still is not the one
        named; its 512Mi comes off the pod figure rather than landing on the
        app. ceil(1500 x 1.3) = 1950, less 512 = 1438."""
        pod = self.with_sidecar(self.pod(mem_req="256Mi", mem_lim="4Gi"))
        [hit] = self.check([pod], {("kubeagents-system", "litellm-1"): 1500.0})
        self.assertIn("Raise the memory request of container `app` to 1438Mi, leaving the others' 512Mi as it is.", hit["excerpt"])

    def test_an_unlimited_native_sidecar_leaves_the_sum_no_ceiling(self):
        """App limited at 1Gi, sidecar unlimited: the sum binds nothing, so a
        mean near 1Gi is `major`, not the `critical` the app alone read as."""
        pod = self.with_sidecar(self.pod(mem_req="256Mi", mem_lim="1Gi"))
        [hit] = self.check([pod], {("kubeagents-system", "litellm-1"): 1000.0})
        self.assertEqual(hit["severity"], "major")
        self.assertIn("a memory limit on only some containers", hit["excerpt"])

    def test_a_container_with_no_memory_request_makes_the_mean_unattributable(self):
        pod = self.pod()
        pod["spec"]["containers"].append({"resources": {}})
        means = {("kubeagents-system", "litellm-1"): 973.0}
        self.assertEqual(self.check([pod], means), [])

    def test_a_burst_above_the_request_is_not_a_finding(self):
        """The distinction the whole check rests on. Burstable QoS exists so a
        pod may exceed its request; only a *mean* above it says the request was
        sized wrong. Reading the peak here would flag every workload that ever
        spikes -- which is the normal, intended use of a memory limit."""
        pod = self.pod()
        means = {("kubeagents-system", "litellm-1"): 300.0}   # under the 512Mi request
        peaks = {("kubeagents-system", "litellm-1"): (0.05, 1900.0)}  # far over it
        self.assertEqual(self.check([pod], means, peaks), [])

    def test_the_floor_is_on_the_overage_and_not_the_request(self):
        """`cert-manager-cainjector`: 32Mi requested, 65Mi mean -- 203% of its
        request, and 33 MiB of under-booking that distorts no node's
        scheduling. A ratio test alone flags every pod a megabyte over."""
        cainjector = self.pod(ns="cert-manager", name="cainjector-1", mem_req="32Mi", mem_lim="128Mi")
        self.assertEqual(self.check([cainjector], {("cert-manager", "cainjector-1"): 65.0}), [])
        self.assertEqual(fw.UNDERREQUEST_FLOOR_MIB, 128.0)

    def test_the_floor_boundary_separates_the_two_live_examples(self):
        under = self.pod(ns="a", name="a-1", owner_name="a", mem_req="100Mi", mem_lim="1Gi")
        over = self.pod(ns="b", name="b-1", owner_name="b", mem_req="100Mi", mem_lim="1Gi")
        means = {("a", "a-1"): 100.0 + 127.0, ("b", "b-1"): 100.0 + 129.0}
        hits = self.check([under, over], means)
        self.assertEqual([h["namespace"] for h in hits], ["b"])

    def test_two_controllers_of_one_name_in_two_namespaces_stay_apart(self):
        """`_eligible_pods_by_owner` groups by `(namespace, kind, name)`.

        Keyed on `(kind, name)` alone, the same chart installed twice merges
        into a single entry: one finding, naming whichever namespace was seen
        first, whose request and usage totals silently include the other's.
        A ReplicaSet's pod-template hash hides this; a StatefulSet owns pods
        under its bare name and does not.
        """
        pods = [
            self.pod(ns="tenant-a", name="redis-0", owner_kind="StatefulSet", owner_name="redis"),
            self.pod(ns="tenant-b", name="redis-0", owner_kind="StatefulSet", owner_name="redis"),
        ]
        means = {("tenant-a", "redis-0"): 900.0, ("tenant-b", "redis-0"): 900.0}
        hits = self.check(pods, means)
        self.assertEqual(sorted(h["namespace"] for h in hits), ["tenant-a", "tenant-b"])
        self.assertEqual({h["object"] for h in hits}, {"StatefulSet/redis"})
        # Each finding measures only its own namespace's replica: one pod at
        # 900 MiB against one 512Mi request, not two summed to 1800.
        for hit in hits:
            self.assertIn("0.88 GiB", hit["excerpt"])

    def test_a_mean_near_the_limit_is_critical_rather_than_major(self):
        # Above the request is a scheduling problem; within 10% of the limit is
        # an OOMKill waiting for one bad request, so it outranks it.
        pod = self.pod(mem_req="512Mi", mem_lim="1Gi")
        hits = self.check([pod], {("kubeagents-system", "litellm-1"): 950.0})
        self.assertEqual(hits[0]["severity"], "critical")

    def test_no_memory_limit_is_major_and_says_so(self):
        """Without a limit there is no OOMKill to escalate for -- the pod grows
        until the node runs out and kubelet evicts it. The excerpt has to say
        the ceiling is absent rather than print a misleading `0.0 GiB limit`."""
        pod = self.pod(mem_lim=None)
        pod["spec"]["containers"][0]["resources"].pop("limits")
        hits = self.check([pod], {("kubeagents-system", "litellm-1"): 900.0})
        self.assertEqual(hits[0]["severity"], "major")
        self.assertIn("no memory limit", hits[0]["excerpt"])

    def test_a_controller_monitoring_never_measured_is_skipped(self):
        """A pod absent from the means is unmeasured, not idle. The means are
        non-empty -- another namespace was measured -- so this reaches the
        per-controller guard rather than the empty-means early return."""
        pods = [self.pod(name="litellm-1"), self.pod(name="litellm-2")]
        self.assertEqual(self.check(pods, {("elsewhere", "other-1"): 300.0}), [])

    def test_no_means_at_all_flags_nothing(self):
        # `fetch_memory_means` returns `{}` for a cluster it could not read.
        self.assertEqual(fw.check_underrequest({"pods": [self.pod()]}, {}, {}, now=NOW), [])

    def test_the_excerpt_carries_the_peak_beside_the_mean(self):
        """Remediation sizes the new request off the peak, so the number it
        uses has to be in the evidence -- `adopt_collector_evidence` replaces
        whatever the model wrote with this string."""
        pod = self.pod()
        means = {("kubeagents-system", "litellm-1"): 973.0}
        peaks = {("kubeagents-system", "litellm-1"): (0.05, 1004.0)}
        excerpt = self.check([pod], means, peaks)[0]["excerpt"]
        self.assertIn("peak 0.98 GiB", excerpt)
        self.assertIn(f"trailing {fw.USAGE_WINDOW_HOURS}h", excerpt)
        self.assertIn("Sustained, not a burst", excerpt)

    # `litellm` on `kube-agents-host` as the 2026-09-07 08:33 UTC run measured
    # it, re-read from Cloud Monitoring at 14:13 the same day: two replicas,
    # 512Mi requested and 2Gi limited each, per-replica mean 1011.25 MiB and
    # per-replica peak 1655.96 MiB.
    LIVE_MEAN_PR = 1011.25
    LIVE_PEAK_PR = 1655.96

    def live_litellm(self, mem_lim="2Gi"):
        """The published finding's excerpt, from its own numbers."""
        pods = [self.pod(name="litellm-1", mem_lim=mem_lim), self.pod(name="litellm-2", mem_lim=mem_lim)]
        means = {("kubeagents-system", f"litellm-{i}"): self.LIVE_MEAN_PR for i in (1, 2)}
        peaks = {("kubeagents-system", f"litellm-{i}"): (0.05, self.LIVE_PEAK_PR) for i in (1, 2)}
        return self.check(pods, means, peaks)[0]["excerpt"]

    def test_a_prescription_above_the_limit_says_to_raise_the_limit(self):
        """The 2026-09-07 finding, and the regression this pins.

        It was published prescribing 2153Mi per replica against a 2048Mi
        per-replica limit -- a manifest the API server rejects at admission --
        and its recommendation read "limits and CPU request untouched". The
        model had no way to see it: the only limit the excerpt printed was the
        4.0 GiB controller total, which is twice the prescription.
        """
        excerpt = self.live_litellm()
        self.assertIn("Raise the memory request to 2153Mi per replica.", excerpt)
        self.assertIn("exceeds the 2048Mi memory limit declared per replica", excerpt)
        self.assertIn("rejected at admission", excerpt)
        self.assertIn("raise the limit to 4306Mi in the same edit", excerpt)

    def test_a_prescription_under_the_limit_says_nothing_about_the_limit(self):
        # 17 of the 19 sizing-eligible controllers on the fleet are this case.
        excerpt = self.live_litellm(mem_lim="4Gi")
        self.assertIn("Raise the memory request to 2153Mi per replica.", excerpt)
        self.assertNotIn("raise the limit", excerpt)

    def test_the_limit_boundary_is_strict(self):
        """A request equal to its limit is admissible, so equality says nothing.

        `ceil(1000 x 1.3) = 1300`, and a 1300Mi limit is exactly reached.
        """
        pod = self.pod(mem_req="512Mi", mem_lim="1300Mi")
        means = {("kubeagents-system", "litellm-1"): 900.0}
        peaks = {("kubeagents-system", "litellm-1"): (0.05, 1000.0)}
        excerpt = self.check([pod], means, peaks)[0]["excerpt"]
        self.assertIn("Raise the memory request to 1300Mi.", excerpt)
        self.assertNotIn("raise the limit", excerpt)

    def test_the_prescription_is_computed_from_the_unrounded_peak(self):
        """§3.1's `about 10m` failure, transplanted.

        The excerpt prints the per-replica peak to two decimals of a GiB, so
        1655.96 MiB reads as 1.62 GiB and 1.62 x 1.3 comes to 2.11 GiB. The
        measurement behind it gives 2153Mi, which is 2.10 GiB. Recomputing
        from the printed figure is off by a MiB in the direction that matters,
        because the comparison against the limit is what it decides.
        """
        excerpt = self.live_litellm()
        self.assertIn("a 1.62 GiB peak", excerpt)  # the per-replica figure, rounded
        self.assertIn("2153Mi", excerpt)
        self.assertNotIn("2157Mi", excerpt)  # ceil(1.62 * 1024 * 1.3)

    def test_no_memory_limit_gets_the_request_but_no_limit_clause(self):
        # The 1 live controller with no memory limit at all: nothing to collide
        # with, so the prescription stands alone.
        pod = self.pod(mem_lim=None)
        pod["spec"]["containers"][0]["resources"].pop("limits")
        excerpt = self.check([pod], {("kubeagents-system", "litellm-1"): 900.0})[0]["excerpt"]
        self.assertIn("Raise the memory request to", excerpt)
        self.assertNotIn("raise the limit", excerpt)

    def test_a_partially_limited_pod_is_graded_on_no_sum_but_its_limit_still_binds(self):
        """Admission is per container; the summed limit binds neither.

        The main container declares a 2Gi limit and the sidecar declares none,
        so `mem_lim_total` is a real number that enforces nothing: the grade
        stays `major` and no 2.0 GiB ceiling is printed. The main container's
        own limit still binds the raise written on it, though -- 1.3x the
        1900 MiB mean is 2470Mi for the pod, 2406Mi on `main` beside the
        sidecar's 64Mi, past its 2048Mi -- so the limit clause fires.
        """
        pod = self.pod(mem_req="512Mi", mem_lim="2Gi")
        pod["spec"]["containers"][0]["name"] = "main"
        pod["spec"]["containers"].append(
            {"name": "sidecar", "resources": {"requests": {"memory": "64Mi"}}}
        )
        [hit] = self.check([pod], {("kubeagents-system", "litellm-1"): 1900.0})
        self.assertEqual(hit["severity"], "major")
        self.assertIn("a memory limit on only some containers", hit["excerpt"])
        self.assertNotIn("GiB limit", hit["excerpt"])
        self.assertIn("Raise the memory request of container `main` to 2406Mi", hit["excerpt"])
        self.assertIn("exceeds the 2048Mi memory limit declared on that container", hit["excerpt"])

    def test_a_fully_limited_pod_is_compared_with_the_named_containers_limit(self):
        """main 2Gi + sidecar 512Mi sums to 2560Mi. The 2470Mi pod figure fits
        the sum, and written on `main` (2470 - 64 = 2406Mi) it is past main's
        own 2048Mi, which admission rejects."""
        pod = self.pod(mem_req="512Mi", mem_lim="2Gi")
        pod["spec"]["containers"][0]["name"] = "main"
        pod["spec"]["containers"].append(
            {"name": "sidecar", "resources": {"requests": {"memory": "64Mi"}, "limits": {"memory": "512Mi"}}}
        )
        [hit] = self.check([pod], {("kubeagents-system", "litellm-1"): 1900.0})
        self.assertIn("Raise the memory request of container `main` to 2406Mi", hit["excerpt"])
        self.assertIn("exceeds the 2048Mi memory limit declared on that container", hit["excerpt"])
        self.assertIn("raise the limit to", hit["excerpt"])

    def test_the_raise_lands_on_the_container_with_the_largest_request(self):
        pod = self.pod(mem_req="64Mi", mem_lim="4Gi")
        pod["spec"]["containers"][0]["name"] = "proxy"
        pod["spec"]["containers"].append(
            {"name": "app", "resources": {"requests": {"memory": "512Mi"}, "limits": {"memory": "4Gi"}}}
        )
        [hit] = self.check([pod], {("kubeagents-system", "litellm-1"): 1900.0})
        self.assertIn("container `app` to 2406Mi", hit["excerpt"])
        self.assertNotIn("raise the limit", hit["excerpt"])

    def test_a_sidecar_declaring_a_zero_memory_request_skips_the_pod(self):
        """`memory: "0"` sets nothing against the sidecar's own usage, so the
        summed mean would land its usage on the main container's request --
        the mis-attribution a sidecar with no request at all is skipped for."""
        pod = self.pod(mem_req="512Mi", mem_lim="2Gi")
        pod["spec"]["containers"].append(
            {"name": "sidecar", "resources": {"requests": {"memory": "0"}}}
        )
        self.assertEqual(self.check([pod], {("kubeagents-system", "litellm-1"): 700.0}), [])

    def test_a_fully_limited_pod_near_its_limit_is_critical(self):
        # The control for the test above: the same numbers with no sidecar.
        [hit] = self.check([self.pod(mem_req="512Mi", mem_lim="2Gi")], {("kubeagents-system", "litellm-1"): 1900.0})
        self.assertEqual(hit["severity"], "critical")
        self.assertIn("2.0 GiB limit", hit["excerpt"])
        self.assertIn("raise the limit", hit["excerpt"])

    def test_the_per_replica_suffix_tracks_the_replica_count(self):
        # `check_overrequest`'s rule: on a single-replica controller the
        # qualifier distinguishes nothing and reads as though it did.
        pod = self.pod(mem_req="512Mi", mem_lim="8Gi")
        means = {("kubeagents-system", "litellm-1"): 900.0}
        peaks = {("kubeagents-system", "litellm-1"): (0.05, 1000.0)}
        one = self.check([pod], means, peaks)[0]["excerpt"]
        self.assertIn("Raise the memory request to 1300Mi.", one)
        self.assertNotIn("per replica", one)
        self.assertIn("Raise the memory request to 2153Mi per replica.", self.live_litellm(mem_lim="8Gi"))

    def test_resize_target_defaults_to_the_overrequest_multiplier(self):
        """Adding the keyword must not have moved §3.1's number.

        `_resize_target` is shared, and its `check_overrequest` call sites --
        two direct, and `_resize_shrinks_request`, which `check_idle_workload`
        calls too -- pass no multiplier.
        """
        self.assertEqual(fw.UNDERREQUEST_PEAK_MULTIPLIER, 1.3)
        self.assertEqual(fw.UNDERREQUEST_LIMIT_MULTIPLIER, 2)
        self.assertEqual(fw.OVERREQUEST_PEAK_MULTIPLIER, 2)
        both = dict(floor=fw.OVERREQUEST_RESIZE_FLOOR_MIB, unit=1.0)
        self.assertEqual(fw._resize_target(1000.0, 2, **both), 1000.0)
        self.assertEqual(
            fw._resize_target(1000.0, 2, **both, multiplier=fw.UNDERREQUEST_PEAK_MULTIPLIER), 650.0
        )

    def test_it_shares_overrequests_exclusions(self):
        """Both directions of one edit must agree on which pods are eligible,
        or a DaemonSet excluded from shrinking becomes eligible for growing.
        `_eligible_pods_by_owner` is the shared implementation; this is the
        test that it is actually shared."""
        cases = {
            "daemonset": dict(owner_kind="DaemonSet", owner_name="ds"),
            "job": dict(owner_kind="Job", owner_name="batch"),
            "young": dict(started="2026-07-31T23:30:00Z"),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                pod = self.pod(**kwargs)
                self.assertEqual(self.check([pod], {("kubeagents-system", "litellm-1"): 900.0}), [])

    def test_a_system_namespace_pod_is_excluded(self):
        pod = self.pod(ns="kube-system", name="sys-1")
        self.assertEqual(self.check([pod], {("kube-system", "sys-1"): 900.0}), [])

    def test_a_pod_with_no_memory_request_is_left_to_obtainability(self):
        # No request at all is `no-requests` in the obtainability audit, which
        # owns the "declare something" finding. Dividing by it here would also
        # be a ZeroDivisionError.
        pod = self.pod()
        pod["spec"]["containers"][0]["resources"] = {"requests": {"cpu": "100m"}}
        self.assertEqual(self.check([pod], {("kubeagents-system", "litellm-1"): 900.0}), [])

    def test_the_window_shrinks_to_a_controller_younger_than_it(self):
        pod = self.pod(started="2026-07-31T18:00:00Z")
        hits = self.check([pod], {("kubeagents-system", "litellm-1"): 900.0})
        self.assertIn("trailing 6h", hits[0]["excerpt"])

    def test_a_short_window_says_why_it_disagrees_with_the_command(self):
        # Same clause as the over-request check: all three sizing checks share
        # one helper, so a fix to the sentence reaches every one of them.
        pod = self.pod(started="2026-07-31T18:00:00Z")
        excerpt = self.check([pod], {("kubeagents-system", "litellm-1"): 900.0})[0]["excerpt"]
        self.assertIn(f"the read covers {fw.USAGE_WINDOW_HOURS}h", excerpt)
        self.assertIn("under a full daily cycle", excerpt)

    def test_a_multi_replica_excerpt_gives_the_per_replica_arithmetic(self):
        """Inflating a request is the wrong direction to be wrong in here.

        §3.11 sizes the new request at ceil(peak x 1.3) and the manifest holds
        one replica's. `litellm`'s summed 1.96 GiB peak read as one replica's
        would book 2.5 GiB where 1.3 GiB is right -- on a finding whose entire
        subject is the scheduler's booking being inaccurate.
        """
        pods = [self.pod(name="litellm-1"), self.pod(name="litellm-2")]
        means = {("kubeagents-system", "litellm-1"): 973.0, ("kubeagents-system", "litellm-2"): 973.0}
        peaks = {k: (0.05, 1004.0) for k in means}
        excerpt = self.check(pods, means, peaks)[0]["excerpt"]
        self.assertIn("requests 1.00 GiB of memory", excerpt)
        self.assertIn("Totals span 2 replicas", excerpt)
        self.assertIn("0.50 GiB requested", excerpt)
        self.assertIn("0.95 GiB mean", excerpt)
        self.assertIn("0.98 GiB peak", excerpt)

    def test_a_single_replica_excerpt_says_nothing_about_replicas(self):
        excerpt = self.check([self.pod()], {("kubeagents-system", "litellm-1"): 900.0})[0]["excerpt"]
        self.assertNotIn("replica", excerpt)


class ReplacedPodPeaksTest(unittest.TestCase):
    """The pods a controller has rolled away, and the peaks that went with them.

    Cloud Monitoring answers for the whole window keyed by pod name. Joining
    that answer to the *live* pod list threw away every series belonging to a
    pod the controller had since replaced, and then reported the remainder as
    the week's peak -- so a Deployment rolled this morning was sized off this
    morning, under an excerpt claiming a week. On `kube-agents-host` on
    2026-09-06 that was nine of twenty-two findings, `litellm` reading 1164 MiB
    against a replaced pod's 1656, and `argocd-dex-server` 0.3m against 3.6m.
    """

    NS = "shop"
    DEP = "web"
    LIVE = f"{DEP}-aaaaaaaaaa-11111"
    GONE = f"{DEP}-bbbbbbbbbb-22222"

    def pod(self, name=LIVE, *, ns=NS, owner="web-aaaaaaaaaa", sized=True, kind="ReplicaSet"):
        resources = {"requests": {"cpu": "12", "memory": "48Gi"}} if sized else {}
        return obj(
            "Pod", name, ns=ns,
            **{
                "spec.containers": [{"name": "web", "resources": resources}],
                "status.startTime": "2026-07-31T15:00:00Z",
                "status.phase": "Running",
                "metadata.labels": {"pod-template-hash": "aaaaaaaaaa"},
                "metadata.ownerReferences": [{"kind": kind, "name": owner}],
            },
        )

    def context(self, pods, *, created="2026-07-01T00:00:00Z", name=DEP):
        return {
            "pods": pods,
            "deployments": [obj("Deployment", name, ns=self.NS, **{"metadata.creationTimestamp": created})],
        }

    def over(self, ctx, peaks):
        return fw.check_overrequest(ctx, peaks, now=NOW, autopilot=False)

    # -- the peak the join used to discard -------------------------------- #

    def test_a_replaced_pods_peak_suppresses_the_overrequest_finding(self):
        """The whole defect in one assertion: the workload reached 11 vCPU this
        week, on a pod that is no longer running, and was about to be told to
        give 11 of its 12 back."""
        ctx = self.context([self.pod()])
        narrow = {(self.NS, self.LIVE): (0.9, 3072.0)}
        self.assertEqual(len(self.over(ctx, narrow)), 1)
        widened = {**narrow, (self.NS, self.GONE): (11.0, 40000.0)}
        self.assertEqual(self.over(ctx, widened), [])

    def test_the_excerpt_names_the_pods_it_reached_back_through(self):
        ctx = self.context([self.pod()])
        peaks = {(self.NS, self.LIVE): (0.9, 3072.0), (self.NS, self.GONE): (1.1, 4000.0)}
        excerpt = self.over(ctx, peaks)[0]["excerpt"]
        self.assertIn("1 pod it has replaced since", excerpt)
        self.assertIn(f"trailing {fw.USAGE_WINDOW_HOURS}h", excerpt)
        # The peak reported is the replaced pod's, not the live one's.
        self.assertIn("1.10 vCPU", excerpt)

    def test_the_window_clamps_to_the_controller_not_the_read(self):
        """A controller younger than the read cannot have been watched for a
        week however many pods it has been through."""
        ctx = self.context([self.pod()], created="2026-07-30T00:00:00Z")
        peaks = {(self.NS, self.LIVE): (0.9, 3072.0), (self.NS, self.GONE): (1.1, 4000.0)}
        excerpt = self.over(ctx, peaks)[0]["excerpt"]
        self.assertIn("trailing 48h", excerpt)
        self.assertIn("this controller's whole life", excerpt)

    def test_without_a_replaced_pod_the_old_clamp_still_applies(self):
        """The live pod started 9h before `NOW`, and nothing widens that."""
        ctx = self.context([self.pod()])
        excerpt = self.over(ctx, {(self.NS, self.LIVE): (0.9, 3072.0)})[0]["excerpt"]
        self.assertIn("oldest pod started", excerpt)
        self.assertNotIn("replaced since", excerpt)

    def statefulset(self, created):
        pod = self.pod(name="db-0", owner="db", kind="StatefulSet")
        pod["status"]["startTime"] = "2026-07-31T18:00:00Z"
        ctx = {
            "pods": [pod],
            "statefulsets": [obj("StatefulSet", "db", ns=self.NS, **{"metadata.creationTimestamp": created})],
        }
        return ctx, {(self.NS, "db-0"): (0.9, 3072.0)}

    def test_a_statefulset_recreated_under_its_old_name_is_measured_over_the_week(self):
        """`db-0` restarted 6h ago as `db-0`, so its series holds the week the
        read asked for, and the excerpt may not claim only the live pod's 6h."""
        excerpt = self.over(*self.statefulset("2026-07-01T00:00:00Z"))[0]["excerpt"]
        self.assertNotIn("over the trailing 6h", excerpt)
        self.assertNotIn("oldest pod started", excerpt)
        self.assertIn(f"over the trailing {fw.USAGE_WINDOW_HOURS}h", excerpt)
        self.assertIn("recreates its pods under the same names", excerpt)

    def test_a_statefulset_younger_than_the_read_clamps_to_its_own_life(self):
        excerpt = self.over(*self.statefulset("2026-07-30T00:00:00Z"))[0]["excerpt"]
        self.assertIn("over the trailing 48h", excerpt)
        self.assertIn("this controller's whole life", excerpt)

    # -- what the pattern must not claim ---------------------------------- #

    def test_a_siblings_pods_are_not_this_controllers(self):
        """`web` and `web-db` in one namespace. Every character of the
        sibling's pod name is one a generated segment can carry, so only the
        two-segment tail keeps them apart -- the hash segment cannot span
        `db`'s hyphen."""
        ctx = self.context([self.pod()])
        peaks = {(self.NS, self.LIVE): (0.9, 3072.0), (self.NS, "web-db-cccccccc-x7k2p"): (11.0, 40000.0)}
        hits = self.over(ctx, peaks)
        self.assertEqual(len(hits), 1)
        self.assertIn("0.90 vCPU", hits[0]["excerpt"])

    def test_a_live_pod_another_controller_owns_is_not_claimed(self):
        """The name matches and the cluster says otherwise. The cluster wins."""
        intruder = self.pod(name=f"{self.DEP}-cccccccccc-44444", owner="logger", kind="DaemonSet")
        ctx = self.context([self.pod(), intruder])
        peaks = {(self.NS, self.LIVE): (0.9, 3072.0), (self.NS, f"{self.DEP}-cccccccccc-44444"): (11.0, 40000.0)}
        hits = self.over(ctx, peaks)
        self.assertEqual(len(hits), 1)
        self.assertIn("0.90 vCPU", hits[0]["excerpt"])

    def test_a_live_pod_of_this_controller_the_checks_skipped_is_not_replaced(self):
        """A sibling started ten minutes ago is too young to size and still
        live. It is not a replaced pod, so it neither counts toward `replaced`
        nor widens the window claimed for the others."""
        young = self.pod(name=self.GONE)
        young["status"]["startTime"] = "2026-07-31T23:50:00Z"
        ctx = self.context([self.pod(), young])
        peaks = {(self.NS, self.LIVE): (0.9, 3072.0), (self.NS, self.GONE): (0.8, 3000.0)}
        excerpt = self.over(ctx, peaks)[0]["excerpt"]
        self.assertNotIn("replaced", excerpt)
        self.assertIn("oldest pod started", excerpt)

    def test_a_bare_replicaset_with_replaced_pods_claims_no_age_bound(self):
        """A ReplicaSet's own age is not in the dump. Its live pod is 9h old,
        but the peak came partly from a replaced pod, so neither the 9h
        oldest-pod clamp nor a controller age is a true bound."""
        pod = self.pod(owner="web-rs")
        ctx = {"pods": [pod]}
        peaks = {(self.NS, self.LIVE): (0.9, 3072.0), (self.NS, "web-rs-22222"): (1.1, 4000.0)}
        excerpt = self.over(ctx, peaks)[0]["excerpt"]
        self.assertNotIn("oldest pod started", excerpt)
        self.assertIn("1 pod it has replaced", excerpt)
        self.assertIn("was not read", excerpt)

    def test_a_hook_jobs_pods_are_not_this_deployments(self):
        """Job `web-migrate` leaves `web-migrate-x7k2p` behind. Two segments,
        like a Deployment pod's -- but `migrate` has vowels, and a ReplicaSet
        hash is drawn from an alphabet with none."""
        ctx = self.context([self.pod()])
        peaks = {(self.NS, self.LIVE): (0.9, 3072.0), (self.NS, f"{self.DEP}-migrate-x7k2p"): (11.0, 40000.0)}
        hits = self.over(ctx, peaks)
        self.assertEqual(len(hits), 1)
        self.assertIn("0.90 vCPU", hits[0]["excerpt"])

    def test_another_namespace_is_not_this_controller(self):
        ctx = self.context([self.pod()])
        peaks = {(self.NS, self.LIVE): (0.9, 3072.0), ("other", self.GONE): (11.0, 40000.0)}
        self.assertEqual(len(self.over(ctx, peaks)), 1)

    def test_a_kind_with_no_pattern_is_not_widened(self):
        """A bare Pod names nothing after itself, so there is no rule to apply
        and the narrow answer is the honest one."""
        pod = self.pod(name="standalone", owner="")
        pod["metadata"]["ownerReferences"] = []
        ctx = self.context([pod])
        peaks = {(self.NS, "standalone"): (0.9, 3072.0), (self.NS, "standalone-bbbbb"): (11.0, 40000.0)}
        self.assertEqual(len(self.over(ctx, peaks)), 1)

    def test_a_statefulset_ordinal_is_matched(self):
        """A StatefulSet keeps its pod names across a roll, so what this finds
        is a replica the controller has scaled away -- whose peak is still a
        replica's peak."""
        pod = self.pod(name="db-0", owner="db", kind="StatefulSet")
        ctx = {"pods": [pod], "statefulsets": [obj("StatefulSet", "db", ns=self.NS)]}
        narrow = {(self.NS, "db-0"): (0.9, 3072.0)}
        self.assertEqual(len(self.over(ctx, narrow)), 1)
        self.assertEqual(self.over(ctx, {**narrow, (self.NS, "db-2"): (11.0, 40000.0)}), [])

    # -- max per replica, never the sum ----------------------------------- #

    def test_the_controller_total_is_the_worst_replica_times_replicas(self):
        """Summing would be nonsense once replaced pods are in the set -- seven
        pod names for a one-replica Deployment would total seven replicas of
        usage. `max x replicas` is the rule, and it holds for the live pods too:
        the hot replica is the one a request has to cover."""
        pods = [self.pod(), self.pod(name=f"{self.DEP}-aaaaaaaaaa-99999")]
        ctx = self.context(pods)
        # 0.2 and 1.0: sum is 1.2 vCPU, max x 2 replicas is 2.0.
        peaks = {(self.NS, self.LIVE): (0.2, 100.0), (self.NS, f"{self.DEP}-aaaaaaaaaa-99999"): (1.0, 100.0)}
        excerpt = self.over(ctx, peaks)[0]["excerpt"]
        self.assertIn("2.00 vCPU", excerpt)

    def test_unsized_sizes_off_the_replaced_pods_peak(self):
        """§3.12 writes a number into a manifest, so this is the check with the
        least margin for a short window: every Argo CD component on the live
        hub was sized off six hours on 2026-09-06."""
        pod = self.pod(sized=False)
        ctx = self.context([pod])
        peaks = {(self.NS, self.LIVE): (0.010, 40.0), (self.NS, self.GONE): (0.050, 200.0)}
        hits = fw.check_unsized(ctx, peaks, now=NOW, autopilot=False)
        self.assertEqual(len(hits), 1)
        # 2x the replaced pod's peak, not 2x the live one's 10m/40Mi.
        self.assertIn("100m / 400Mi per replica", hits[0]["excerpt"])


class UnsizedWorkloadTest(unittest.TestCase):
    """§3.12 -- the population §3.1 and §3.11 both skip.

    Both sizing checks drop a container that declares no request, on the
    grounds that the Workload Reliability audit's `no-requests` owns it. That
    audit reports the absence and, by design, proposes no number, deferring the
    value to here. Until this check existed the deferral ended nowhere: on the
    sixteen-cluster fleet on 2026-09-05 seven of twenty-one eligible
    controllers -- every Argo CD component on the hub -- were named by neither.
    """

    def pod(self, ns="argocd", name="argocd-repo-server-1", owner_kind="ReplicaSet",
            owner_name="argocd-repo-server", started="2026-01-01T00:00:00Z", containers=None):
        return obj(
            "Pod", name, ns=ns,
            **{
                "spec.containers": containers or [{"name": "repo-server", "resources": {}}],
                "status.startTime": started,
                "status.phase": "Running",
                "metadata.ownerReferences": [{"kind": owner_kind, "name": owner_name}],
            },
        )

    def check(self, pods, peaks, autopilot=False):
        return fw.check_unsized({"pods": pods}, peaks, now=NOW, autopilot=autopilot)

    def test_flags_a_controller_that_declares_no_request(self):
        """`argocd-application-controller` on the live hub: the scheduler books
        zero for a workload peaking at 0.175 vCPU and 1.587 GiB."""
        pod = self.pod(name="argocd-application-controller-0", owner_kind="StatefulSet",
                       owner_name="argocd-application-controller")
        peaks = {("argocd", "argocd-application-controller-0"): (0.175, 1625.0)}
        hits = self.check([pod], peaks)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "StatefulSet/argocd-application-controller")
        self.assertEqual(hits[0]["namespace"], "argocd")
        self.assertEqual(hits[0]["severity"], "minor")

    def test_a_request_naming_no_cpu_or_memory_is_unsized(self):
        """None of these names a nonzero CPU or memory request. Partitioned on
        whether `requests` was an empty dict, they read as sized, every sizing
        check dropped them for having no CPU or memory to compare, and nothing
        produced the number. `collect.py`'s `no-requests` files the first two
        as request-less; the declared `cpu: "0"` it passes, since it asks only
        whether the key is present."""
        shapes = {
            "gpu-only": {"nvidia.com/gpu": "1"},
            "ephemeral-storage-only": {"ephemeral-storage": "1Gi"},
            "zero-cpu": {"cpu": "0"},
        }
        for label, requests in shapes.items():
            with self.subTest(shape=label):
                pod = self.pod(containers=[{"name": "c", "resources": {"requests": requests}}])
                peaks = {("argocd", "argocd-repo-server-1"): (0.175, 1625.0)}
                self.assertEqual(len(self.check([pod], peaks)), 1)
                self.assertEqual(fw._eligible_pods_by_owner({"pods": [pod]}, now=NOW), {})

    def test_a_nonzero_memory_request_beside_a_gpu_is_sized(self):
        pod = self.pod(containers=[{"name": "c", "resources": {"requests": {"nvidia.com/gpu": "1", "memory": "1Gi"}}}])
        self.assertEqual(self.check([pod], {("argocd", "argocd-repo-server-1"): (0.175, 1625.0)}), [])
        self.assertEqual(len(fw._eligible_pods_by_owner({"pods": [pod]}, now=NOW)), 1)

    def test_the_excerpt_carries_the_measured_peak_and_the_recommendation(self):
        """`adopt_collector_evidence` overwrites whatever the model wrote with
        this string, so the number remediation sizes off has to be in it."""
        pod = self.pod()
        peaks = {("argocd", "argocd-repo-server-1"): (0.0071, 66.6)}
        excerpt = self.check([pod], peaks)[0]["excerpt"]
        self.assertIn("declares no nonzero CPU or memory request", excerpt)
        self.assertIn("peak observed 7.1m vCPU / 67Mi", excerpt)
        self.assertIn(f"trailing {fw.USAGE_WINDOW_HOURS}h", excerpt)
        # Ceiled, not rounded: 2 x 7.1m is 14.2m and 2 x 66.6Mi is 133.2Mi,
        # and §3.1 never sizes below twice the peak.
        self.assertIn("15m / 134Mi per replica", excerpt)
        self.assertNotIn("floor", excerpt)

    def test_a_short_window_says_why_it_disagrees_with_the_command(self):
        # The third of the three sizing checks sharing the helper. On the live
        # fleet this is the one that fired at 7h.
        pod = self.pod(started="2026-07-31T18:00:00Z")
        excerpt = self.check([pod], {("argocd", "argocd-repo-server-1"): (0.0071, 66.6)})[0]["excerpt"]
        self.assertIn("trailing 6h", excerpt)
        self.assertIn(f"the read covers {fw.USAGE_WINDOW_HOURS}h", excerpt)
        self.assertIn("under a full daily cycle", excerpt)

    def test_the_excerpt_is_written_in_the_units_of_the_manifest_edit(self):
        """§3.1's vCPU and GiB round this population to zero -- nobody forgets
        a request on the workload sized for the cluster, so everything here is
        small. The live fleet on 2026-09-05 produced "peak observed 0.000 vCPU
        / 0.03 GiB ... sized at 0.010 vCPU / 68Mi": a CPU peak that reads as
        nothing, beside a memory recommendation in different units from the
        measurement it is derived from."""
        peaks = {("argocd", "argocd-repo-server-1"): (0.0004, 34.0)}
        excerpt = self.check([self.pod()], peaks)[0]["excerpt"]
        self.assertIn("0.4m vCPU", excerpt)
        self.assertIn("34Mi over the trailing", excerpt)
        self.assertNotIn("0.000", excerpt)
        self.assertNotIn("GiB", excerpt)

    def test_a_floored_figure_says_it_is_the_floor(self):
        """Otherwise the arithmetic in the excerpt is visibly wrong: a
        controller peaking at 0.4m recommended 10m is off by twenty-five
        times, and a reviewer who checks the doubling finds it."""
        excerpt = self.check([self.pod()], {("argocd", "argocd-repo-server-1"): (0.0004, 34.0)})[0]["excerpt"]
        self.assertIn("the cpu figure is the 10m floor rather than 2x the peak", excerpt)
        excerpt = self.check([self.pod()], {("argocd", "argocd-repo-server-1"): (0.0004, 4.0)})[0]["excerpt"]
        self.assertIn("the cpu and memory figure is the 10m/32Mi floor", excerpt)
        excerpt = self.check([self.pod()], {("argocd", "argocd-repo-server-1"): (0.5, 4.0)})[0]["excerpt"]
        self.assertIn("the memory figure is the 32Mi floor", excerpt)

    def test_it_never_reports_the_absence_itself(self):
        """§3.1's boundary rule: the two audits do not restate one another's
        half. This one states the number; `no-requests` states that there is
        no number. An excerpt phrased as a missing-request finding would put
        the same defect in two ledgers under two severities."""
        excerpt = self.check([self.pod()], {("argocd", "argocd-repo-server-1"): (0.01, 64.0)})[0]["excerpt"]
        for phrasing in ("missing", "should declare", "no requests set", "unset"):
            self.assertNotIn(phrasing, excerpt.lower())

    def test_a_sized_controller_is_left_to_the_other_two_checks(self):
        """The complement of `_eligible_pods_by_owner`: a pod lands in exactly
        one of the two populations, so a controller cannot be both sized badly
        and unsized."""
        pod = OverrequestTest().deployment_pod()
        self.assertEqual(self.check([pod], {("default", "api-1"): (0.9, 3072.0)}), [])

    def test_a_partially_sized_controller_belongs_to_the_sizing_checks(self):
        """One container with a request and one without is a pod the other two
        checks already measure, and `_eligible_pods_by_owner` admits it on
        `_declares_cpu_or_memory`. Claiming it here too would double-report it."""
        pod = self.pod(containers=[
            {"name": "main", "resources": {"requests": {"cpu": "100m"}}},
            {"name": "sidecar", "resources": {}},
        ])
        self.assertEqual(self.check([pod], {("argocd", "argocd-repo-server-1"): (0.01, 64.0)}), [])

    def test_an_unmeasured_controller_is_not_reported(self):
        """The whole finding is the measurement. With no Monitoring answer the
        recommendation would be to request zero, which is the state being
        complained about."""
        self.assertEqual(self.check([self.pod()], {("argocd", "other-pod"): (0.5, 500.0)}), [])
        self.assertEqual(self.check([self.pod()], {}), [])

    def test_a_controller_measured_on_one_dimension_is_not_reported(self):
        """A pod with a CPU series and no memory series is unmeasured on
        memory: sized from a zero, the recommendation would request the 64Mi
        floor for a workload nobody measured."""
        self.assertEqual(self.check([self.pod()], {("argocd", "argocd-repo-server-1"): (0.5, None)}), [])
        self.assertEqual(self.check([self.pod()], {("argocd", "argocd-repo-server-1"): (None, 500.0)}), [])

    def test_a_measured_but_idle_controller_still_gets_a_floor(self):
        """A near-silent sidecar measured at almost nothing must not be handed
        a `0m`/`0Mi` request: no scheduler decision turns on it, and it is the
        same BestEffort pod afterwards."""
        peaks = {("argocd", "argocd-repo-server-1"): (0.0001, 1.0)}
        excerpt = self.check([self.pod()], peaks)[0]["excerpt"]
        self.assertIn("10m / 32Mi per replica", excerpt)

    def test_it_shares_the_sizing_checks_exclusions(self):
        """A DaemonSet, a Job pod, or a pod under an hour old is out of scope
        for the sizing checks, and declaring no request does not put it back
        in. `no-requests` on a DaemonSet is a real reliability finding; the
        request value still is not this stream's to set."""
        peaks = {("argocd", "argocd-repo-server-1"): (0.01, 64.0)}
        cases = {
            "daemonset": dict(owner_kind="DaemonSet", owner_name="ds"),
            "job": dict(owner_kind="Job", owner_name="batch"),
            "young": dict(started="2026-07-31T23:30:00Z"),
            "system-namespace": dict(ns="kube-system"),
        }
        for label, kwargs in cases.items():
            with self.subTest(label):
                key = (kwargs.get("ns", "argocd"), "argocd-repo-server-1")
                self.assertEqual(self.check([self.pod(**kwargs)], {key: (0.01, 64.0)}), [])
        self.assertEqual(len(self.check([self.pod()], peaks)), 1)  # the control

    def test_the_recommendation_is_per_replica(self):
        """The peak sums across replicas; the manifest declares one replica's
        request. Dividing is the difference between a 2x headroom and a 2N x
        one -- which on a three-replica controller reserves six times what it
        needs and turns this finding into an `overrequest` next week."""
        # Three replicas, so 2x-per-replica (0.18) is distinct from both the
        # summed peak (0.27) and one replica's share of it (0.09) -- at two
        # replicas the first two collide and the test would pass either way.
        pods = [self.pod(name=f"argocd-repo-server-{i}") for i in (1, 2, 3)]
        peaks = {("argocd", f"argocd-repo-server-{i}"): (0.09, 100.0) for i in (1, 2, 3)}
        excerpt = self.check(pods, peaks)[0]["excerpt"]
        self.assertIn("peak observed 270.0m vCPU / 300Mi", excerpt)
        self.assertIn("180m / 200Mi per replica", excerpt)
        self.assertIn("spans 3 replicas", excerpt)
        # `0.09 * 3 / 3 * 2` does not round-trip, and comparing the floored
        # result back against a re-derivation of the raw value claimed this
        # controller -- eighteen times the CPU floor -- was sitting on it.
        self.assertNotIn("floor", excerpt)

    def test_autopilot_takes_the_same_one_level_bump_as_overrequest(self):
        """Autopilot bills on requests and substitutes its own default where a
        manifest declares none, so the number is being paid for either way."""
        pod = self.pod()
        peaks = {("argocd", "argocd-repo-server-1"): (0.01, 64.0)}
        [hit] = self.check([pod], peaks, autopilot=True)
        self.assertEqual(hit["severity"], "major")
        self.assertTrue(hit["_autopilot_bumped"])
        [hit] = self.check([pod], peaks, autopilot=False)
        self.assertFalse(hit["_autopilot_bumped"])

    def test_it_never_reaches_critical(self):
        """§3 caps a manifest remediation below `critical`, which opens a
        merge-ready pull request. A right-size patch is never that urgent."""
        peaks = {("argocd", "argocd-repo-server-1"): (8.0, 65536.0)}
        for autopilot in (False, True):
            with self.subTest(autopilot=autopilot):
                self.assertNotEqual(self.check([self.pod()], peaks, autopilot=autopilot)[0]["severity"], "critical")


class UnattachedDiskTest(unittest.TestCase):
    def disk(self, name="d1", size_gb=200, created="2026-01-01T00:00:00Z", disk_type="pd-standard", users=None):
        return {"name": name, "sizeGb": str(size_gb), "type": disk_type, "creationTimestamp": created, "zone": "us-central1-a", "users": users or []}

    def test_flags_unattached_over_30_days(self):
        self.assertEqual(len(fw.check_unattached_disk([self.disk()], set(), now=NOW)), 1)

    def test_a_disk_whose_size_does_not_parse_is_skipped_alone(self):
        odd = {**self.disk(name="odd"), "sizeGb": "n/a"}
        hits = fw.check_unattached_disk([odd, self.disk(name="d2")], set(), now=NOW)
        self.assertEqual([h["object"] for h in hits], ["Disk/us-central1-a:d2"])

    def test_a_pv_claims_only_the_disk_in_its_own_zone(self):
        """A detached PV holding `us-central1-a/data-1` used to claim every
        disk named `data-1`, so a truly orphaned one in `us-central1-b` was
        never reported. A handle with no location still claims by name."""
        pv = obj("PersistentVolume", "pv1", **{"spec.csi": {"volumeHandle": "projects/p/zones/us-central1-a/disks/data-1"}})
        handles = fw._fleet_facts({"pvs": [pv], "services": []})["pv_handles"]
        claimed = self.disk(name="data-1")
        orphan = dict(self.disk(name="data-1"), zone="https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-b")
        hits = fw.check_unattached_disk([claimed, orphan], handles, now=NOW)
        self.assertEqual([h["object"] for h in hits], ["Disk/us-central1-b:data-1"])
        self.assertEqual(fw.check_unattached_disk([claimed, orphan], {"data-1"}, now=NOW), [])

    def test_same_named_disks_in_two_zones_derive_two_finding_ids(self):
        """A disk name is unique per zone, and every disk finding is filed
        under `project/<p>` with no namespace, so `object` is the only field
        left to tell them apart. With the bare name both derived one id and
        `finish` refused the document for the duplicate."""
        import audit_report

        east = self.disk(name="data-1")
        west = dict(self.disk(name="data-1"), zone="https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-b")
        hits = fw.check_unattached_disk([east, west], set(), now=NOW)
        self.assertEqual([h["object"] for h in hits], ["Disk/us-central1-a:data-1", "Disk/us-central1-b:data-1"])
        ids = {
            audit_report.published_id({"check": "unattached-disk", "cluster": "project/p", "namespace": "", "object": h["object"]})
            for h in hits
        }
        self.assertEqual(len(ids), 2)

    def test_a_live_owner_is_named_in_the_excerpt(self):
        """The finding stays on `project/<p>`, so the excerpt is where the
        owning cluster is recorded."""
        disk = self.disk()
        disk["labels"] = {fw.GKE_CLUSTER_LABEL: "prod"}
        hits = fw.check_unattached_disk([disk], set(), now=NOW, known_clusters={"prod"})
        self.assertIn("labelled for cluster prod", hits[0]["excerpt"])
        self.assertNotIn("labelled for", fw.check_unattached_disk([self.disk()], set(), now=NOW)[0]["excerpt"])

    def test_an_unlabelled_disk_is_attributed_by_its_gke_name_prefix(self):
        """§3.4's second rung: the longest `gke-<cluster>-` prefix among the
        project's clusters, named only when the audit read that cluster. The
        7-day floor stays keyed on the label, so a prefix alone never lowers it."""
        disk = self.disk(name="gke-prod-usc1-pvc-1")
        known = {"prod", "prod-usc1"}
        hits = fw.check_unattached_disk([disk], set(), now=NOW, known_clusters=known)
        self.assertIn("named for cluster prod-usc1", hits[0]["excerpt"])
        unread = fw.check_unattached_disk([disk], set(), now=NOW, known_clusters=known, unread_clusters=frozenset({"prod-usc1"}))
        self.assertNotIn("named for", unread[0]["excerpt"])
        self.assertNotIn("named for", fw.check_unattached_disk([disk], set(), now=NOW, known_clusters={"staging"})[0]["excerpt"])
        self.assertNotIn("named for", fw.check_unattached_disk([disk], set(), now=NOW)[0]["excerpt"])
        labelled = dict(disk, labels={fw.GKE_CLUSTER_LABEL: "prod"})
        self.assertIn("labelled for cluster prod", fw.check_unattached_disk([labelled], set(), now=NOW, known_clusters=known)[0]["excerpt"])
        recent = dict(disk, creationTimestamp=(NOW - timedelta(days=fw.DEAD_CLUSTER_AGE_DAYS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        self.assertEqual(fw.check_unattached_disk([recent], set(), now=NOW, known_clusters={"staging"}), [])

    def test_a_disk_labelled_for_an_unread_cluster_is_not_judged(self):
        """Its PersistentVolumes were never read, so a detached disk it still
        binds is missing from `live_pv_handles` and would read as abandoned."""
        disk = self.disk()
        disk["labels"] = {fw.GKE_CLUSTER_LABEL: "sick"}
        self.assertEqual(fw.check_unattached_disk([disk], set(), now=NOW, unread_clusters=frozenset({"sick"})), [])

    def test_an_unlabelled_pvc_disk_is_not_judged_while_any_cluster_is_unread(self):
        disk = self.disk()
        disk["description"] = json.dumps({"kubernetes.io/created-for/pvc/name": "data"})
        self.assertEqual(fw.check_unattached_disk([disk], set(), now=NOW, unread_clusters=frozenset({"sick"})), [])

    def test_a_disk_no_pvc_created_is_still_judged_beside_an_unread_cluster(self):
        self.assertEqual(len(fw.check_unattached_disk([self.disk()], set(), now=NOW, unread_clusters=frozenset({"sick"}))), 1)

    def test_a_disk_labelled_for_a_read_cluster_is_still_judged(self):
        disk = self.disk()
        disk["labels"] = {fw.GKE_CLUSTER_LABEL: "healthy"}
        self.assertEqual(len(fw.check_unattached_disk([disk], set(), now=NOW, unread_clusters=frozenset({"sick"}))), 1)

    def test_a_name_counts_as_unread_when_any_cluster_by_that_name_went_unread(self):
        """Two `prod` clusters in one project, one read: a disk labelled `prod`
        may be the unread one's, so the name is unread."""
        known = {("prod", "us-central1"), ("prod", "europe-west1"), ("ok", "us-central1")}
        collected = {("prod", "us-central1"), ("ok", "us-central1")}
        self.assertEqual(fw._unread_names(known, collected), frozenset({"prod"}))
        self.assertEqual(fw._unread_names(known, known), frozenset())

    def test_a_not_running_cluster_is_unread_under_its_bare_name(self):
        """`not_running_entry` qualifies its name as a manifest target, and a
        disk's `goog-k8s-cluster-name` label never is: a DEGRADED `prod` must be
        known and unread as `prod`, or its detached data disk reads as the
        leftover of a deleted cluster."""
        degraded = fw.not_running_entry({"name": "prod", "location": "us-central1", "status": "DEGRADED"}, "p")
        running = {"name": "ok", "location": "us-central1"}
        names, pairs = fw._known_clusters([running, degraded])
        self.assertEqual(names, {"ok", "prod"})
        self.assertEqual(fw._unread_names(pairs, {("ok", "us-central1")}), frozenset({"prod"}))

    def test_managed_service_disks_are_not_judged(self):
        for label in ("goog-composer-environment", "goog-dataproc-cluster-name"):
            disk = self.disk()
            disk["labels"] = {label: "x"}
            self.assertEqual(fw.check_unattached_disk([disk], set(), now=NOW), [])

    def test_a_node_boot_disk_is_judged_only_once_its_cluster_is_gone(self):
        disk = self.disk()
        disk["labels"] = {fw.GKE_NODE_DISK_LABEL: "", fw.GKE_CLUSTER_LABEL: "prod"}
        self.assertEqual(fw.check_unattached_disk([disk], set(), now=NOW, known_clusters={"prod"}), [])
        self.assertEqual(len(fw.check_unattached_disk([disk], set(), now=NOW, known_clusters={"other"})), 1)

    def test_does_not_flag_attached(self):
        self.assertEqual(fw.check_unattached_disk([self.disk(users=["some-vm"])], set(), now=NOW), [])

    def test_does_not_flag_recently_created(self):
        self.assertEqual(fw.check_unattached_disk([self.disk(created="2026-07-30T00:00:00Z")], set(), now=NOW), [])

    def test_a_disk_detached_yesterday_is_churn_however_old_it_is(self):
        """The 30 days are justified as outliving a maintenance cycle, and a
        maintenance cycle detaches disks — it does not create them. Reading the
        age off `creationTimestamp` flagged a year-old boot disk that PD-CSI
        released this morning, which is the churn the threshold excludes."""
        disk = self.disk(created="2025-01-01T00:00:00Z")
        disk["lastDetachTimestamp"] = "2026-07-31T00:00:00Z"
        self.assertEqual(fw.check_unattached_disk([disk], set(), now=NOW), [])

    def test_the_excerpt_dates_the_detach_not_the_creation(self):
        disk = self.disk(created="2025-01-01T00:00:00Z")
        disk["lastDetachTimestamp"] = "2026-05-30T00:00:00Z"
        hits = fw.check_unattached_disk([disk], set(), now=NOW)
        self.assertIn("unattached since 2026-05-30T00:00:00Z", hits[0]["excerpt"])
        self.assertNotIn("2025-01-01", hits[0]["excerpt"])

    def test_a_disk_never_attached_says_so_rather_than_implying_a_detach(self):
        """No `lastDetachTimestamp` means GCE never attached it, so creation is
        genuinely when it went idle — but "unattached since" would assert a
        detach that never happened."""
        hits = fw.check_unattached_disk([self.disk()], set(), now=NOW)
        self.assertIn("never attached, created 2026-01-01T00:00:00Z", hits[0]["excerpt"])

    def test_does_not_flag_a_disk_matching_a_live_pv_handle(self):
        self.assertEqual(fw.check_unattached_disk([self.disk(name="pv-handle-1")], {"pv-handle-1"}, now=NOW), [])

    def test_large_ssd_is_major(self):
        hits = fw.check_unattached_disk([self.disk(size_gb=600, disk_type="pd-ssd")], set(), now=NOW)
        self.assertEqual(hits[0]["severity"], "major")

    def test_the_excerpt_shortens_the_disk_type_selflink(self):
        """Live `disks list` returns `type` as a diskTypes URL. Printed whole it
        put 100 characters of `googleapis.com` in the excerpt beside a `--zone`
        flag `_scope_flag` had already shortened for being unreadable."""
        url = "https://www.googleapis.com/compute/v1/projects/p/zones/us-east4-b/diskTypes/pd-balanced"
        hits = fw.check_unattached_disk([self.disk(disk_type=url)], set(), now=NOW)
        self.assertIn("pd-balanced", hits[0]["excerpt"])
        self.assertNotIn("googleapis", hits[0]["excerpt"])

    def test_severity_still_reads_ssd_out_of_a_selflink_type(self):
        url = "https://www.googleapis.com/compute/v1/projects/p/zones/us-east4-b/diskTypes/pd-ssd"
        hits = fw.check_unattached_disk([self.disk(size_gb=10, disk_type=url)], set(), now=NOW)
        self.assertEqual(hits[0]["severity"], "major")

    def test_a_zonal_disk_carries_its_zone_scope_flag(self):
        """The excerpt used to print `zone=` with gcloud's raw selfLink in it.

        A URL is not something you can paste after `--zone`, so the describe and
        delete in §3.2's chain went out unscoped and resolved against gcloud's
        configured zone.
        """
        url = "https://www.googleapis.com/compute/v1/projects/p/zones/us-east4-a"
        hits = fw.check_unattached_disk([{**self.disk(), "zone": url}], set(), now=NOW)
        self.assertIn("--zone=us-east4-a", hits[0]["excerpt"])
        self.assertNotIn("googleapis", hits[0]["excerpt"])

    def test_a_regional_disk_carries_a_region_flag_not_a_zone_one(self):
        """A regional PD has `region` and no `zone`; `--zone` would not find it."""
        disk = {k: v for k, v in self.disk().items() if k != "zone"}
        disk["region"] = "https://www.googleapis.com/compute/v1/projects/p/regions/us-east4"
        hits = fw.check_unattached_disk([disk], set(), now=NOW)
        self.assertIn("--region=us-east4", hits[0]["excerpt"])
        self.assertNotIn("--zone", hits[0]["excerpt"])


class DeadClusterDiskTest(unittest.TestCase):
    """§3.4's short floor for a disk whose owning cluster no longer exists.

    Live case this was written from: two `platform-agent-host` PDs detached on
    2026-08-26 when that cluster was deleted, still billing ten days later, and
    still three weeks short of the 30-day floor that assumes something will
    reattach them.
    """

    def disk(self, name="d1", detached="2026-07-22T00:00:00Z", cluster="dead-cluster", size_gb=10):
        disk = {
            "name": name,
            "sizeGb": str(size_gb),
            "type": "pd-balanced",
            "creationTimestamp": "2026-01-01T00:00:00Z",
            "zone": "us-east4-b",
            "users": [],
            "lastDetachTimestamp": detached,
        }
        if cluster is not None:
            disk["labels"] = {"goog-k8s-cluster-name": cluster}
        return disk

    def test_ten_days_off_a_deleted_cluster_is_a_finding(self):
        hits = fw.check_unattached_disk([self.disk()], set(), now=NOW, known_clusters={"live-one"})
        self.assertEqual(len(hits), 1)

    def test_the_same_disk_is_churn_while_its_cluster_still_runs(self):
        self.assertEqual(
            fw.check_unattached_disk([self.disk()], set(), now=NOW, known_clusters={"dead-cluster"}),
            [],
        )

    def test_an_unknown_fleet_does_not_read_as_an_empty_one(self):
        """`known_clusters=None` is the enumeration having failed. Treating it
        as "no clusters exist" would drop every GKE disk in the project to the
        7-day floor on the one run that could not see the fleet."""
        self.assertEqual(fw.check_unattached_disk([self.disk()], set(), now=NOW), [])

    def test_the_short_floor_still_has_a_floor(self):
        """Six days is inside the asynchronous-teardown and same-name-recreate
        window the seven days exist to outlive."""
        recent = self.disk(detached="2026-07-26T00:00:00Z")
        self.assertEqual(fw.check_unattached_disk([recent], set(), now=NOW, known_clusters={"live"}), [])

    def test_an_unlabelled_disk_keeps_the_thirty_day_floor(self):
        """No `goog-k8s-cluster-name` means no cluster was shown to be gone —
        an unlabelled disk is not evidence of a deleted cluster."""
        bare = self.disk(cluster=None)
        self.assertEqual(fw.check_unattached_disk([bare], set(), now=NOW, known_clusters={"live"}), [])

    def test_the_excerpt_names_the_cluster_that_is_gone(self):
        """`adopt_collector_evidence` makes this string the only evidence a
        reader sees, and without the cluster there is nothing in it explaining
        why a ten-day-old disk was reported when a twenty-day-old one was not.
        """
        excerpt = fw.check_unattached_disk([self.disk()], set(), now=NOW, known_clusters={"live"})[0]["excerpt"]
        self.assertIn("dead-cluster", excerpt)
        self.assertIn("no longer runs", excerpt)

    def test_a_thirty_day_disk_says_nothing_about_a_dead_cluster(self):
        old = self.disk(detached="2026-06-01T00:00:00Z", cluster="live")
        excerpt = fw.check_unattached_disk([old], set(), now=NOW, known_clusters={"live"})[0]["excerpt"]
        self.assertNotIn("no longer runs", excerpt)

    def test_a_live_pv_handle_still_wins(self):
        """The claimed-but-detached exclusion is not weakened by the short
        floor: a PV still bound to the handle is data, not waste."""
        self.assertEqual(
            fw.check_unattached_disk([self.disk(name="h1")], {"h1"}, now=NOW, known_clusters={"live"}),
            [],
        )

    def test_a_degraded_cluster_still_owns_its_disks(self):
        """`enumerate_clusters` hands DEGRADED and PROVISIONING clusters back in
        its second list rather than its first. They exist; a disk labelled for
        one is not orphaned."""
        self.assertEqual(
            fw.check_unattached_disk([self.disk(cluster="degraded-one")], set(), now=NOW, known_clusters={"live", "degraded-one"}),
            [],
        )


class DiskPvcOriginTest(unittest.TestCase):
    """A PD-CSI volume is named for a PV UID, so the finding's object is an
    opaque handle. The claim it was cut for is what tells the operator whether
    deleting it destroys anything they care about.
    """

    DESCRIPTION = json.dumps(
        {
            "kubernetes.io/created-for/pvc/name": "platform-agent-data",
            "kubernetes.io/created-for/pvc/namespace": "kubeagents-system",
            "kubernetes.io/created-for/pv/name": "pvc-d45cfdfd",
        }
    )

    def disk(self, **overrides):
        disk = {
            "name": "pvc-d45cfdfd-f194-4bda-9d53-f83a67d8ac34",
            "sizeGb": "10",
            "type": "pd-balanced",
            "creationTimestamp": "2026-01-01T00:00:00Z",
            "zone": "us-east4-b",
            "users": [],
            # Past the 30-day floor on its own, so these tests exercise the
            # excerpt rather than the dead-cluster short floor.
            "lastDetachTimestamp": "2026-06-01T00:00:00Z",
        }
        disk.update(overrides)
        return disk

    def test_the_excerpt_names_the_claim_the_disk_was_cut_for(self):
        hits = fw.check_unattached_disk([self.disk(description=self.DESCRIPTION)], set(), now=NOW)
        self.assertIn("kubeagents-system/platform-agent-data", hits[0]["excerpt"])

    def test_a_disk_no_driver_provisioned_says_nothing_about_a_claim(self):
        hits = fw.check_unattached_disk([self.disk()], set(), now=NOW)
        self.assertNotIn("PersistentVolumeClaim", hits[0]["excerpt"])

    def test_a_description_that_is_not_json_is_not_a_crash(self):
        """`description` is a free-text field an operator can write anything
        into, including the marker substring."""
        disk = self.disk(description="notes about kubernetes.io/created-for, not JSON")
        hits = fw.check_unattached_disk([disk], set(), now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertNotIn("PersistentVolumeClaim", hits[0]["excerpt"])

    def test_a_claim_with_no_namespace_is_named_bare(self):
        disk = self.disk(
            description=json.dumps({"kubernetes.io/created-for/pvc/name": "lonely"})
        )
        hits = fw.check_unattached_disk([disk], set(), now=NOW)
        self.assertIn("held the lonely PersistentVolumeClaim", hits[0]["excerpt"])

    def test_the_claim_rides_alongside_the_dead_cluster_note(self):
        disk = self.disk(
            description=self.DESCRIPTION,
            labels={"goog-k8s-cluster-name": "platform-agent-host"},
        )
        excerpt = fw.check_unattached_disk([disk], set(), now=NOW, known_clusters={"live"})[0]["excerpt"]
        self.assertIn("kubeagents-system/platform-agent-data", excerpt)
        self.assertIn("platform-agent-host", excerpt)
        self.assertIn("no longer runs", excerpt)


class IdleAddressTest(unittest.TestCase):
    def address(self, name="addr1", addr_type="EXTERNAL", status="RESERVED", purpose="", created="2026-01-01T00:00:00Z", region="us-central1"):
        return {"name": name, "address": "1.2.3.4", "addressType": addr_type, "status": status, "purpose": purpose, "creationTimestamp": created, "region": region}

    def test_flags_reserved_external_over_14_days(self):
        self.assertEqual(len(fw.check_idle_address([self.address()], set(), project="p", now=NOW)), 1)

    def test_does_not_flag_internal(self):
        self.assertEqual(fw.check_idle_address([self.address(addr_type="INTERNAL")], set(), project="p", now=NOW), [])

    def test_does_not_flag_gce_endpoint_purpose(self):
        self.assertEqual(fw.check_idle_address([self.address(purpose="GCE_ENDPOINT")], set(), project="p", now=NOW), [])

    def test_does_not_flag_referenced_by_annotation(self):
        self.assertEqual(fw.check_idle_address([self.address(name="my-ip")], {"my-ip"}, project="p", now=NOW), [])

    def test_does_not_flag_an_address_held_for_dr_failover_or_migration(self):
        # §3.5's description exclusion, whole words in any case.
        for description in ("DR standby for prod", "failover for prod-usc1", "Fail-over VIP", "Disaster Recovery", "planned migration to us-east4"):
            with self.subTest(description=description):
                addr = {**self.address(), "description": description}
                self.assertEqual(fw.check_idle_address([addr], set(), project="p", now=NOW), [])

    def test_every_form_of_migrate_holds_an_address(self):
        for description in ("migrated from us-central1", "migrates to prod next quarter", "held for migrations", "migrate target"):
            with self.subTest(description=description):
                addr = {**self.address(), "description": description}
                self.assertEqual(fw.check_idle_address([addr], set(), project="p", now=NOW), [])

    def test_a_word_that_only_starts_with_migrat_does_not_exempt_an_address(self):
        addr = {**self.address(), "description": "migratory bird telemetry"}
        self.assertEqual(len(fw.check_idle_address([addr], set(), project="p", now=NOW)), 1)

    def test_dr_inside_a_word_does_not_exempt_an_address(self):
        addr = {**self.address(), "description": "old address for the drain job"}
        self.assertEqual(len(fw.check_idle_address([addr], set(), project="p", now=NOW)), 1)

    def test_an_address_an_ingress_names_is_referenced(self):
        """An Ingress holds its global address RESERVED until its load
        balancer provisions, and names it only in its own annotation."""
        ingress = {"kind": "Ingress", "metadata": {"namespace": "web", "name": "shop", "annotations": {"kubernetes.io/ingress.global-static-ip-name": "shop-ip"}}}
        context = fw.build_context({"items": [ingress]})
        referenced = fw._fleet_facts(context)["referenced_addresses"]
        self.assertEqual(fw.check_idle_address([self.address(name="shop-ip", region=None)], referenced, project="p", now=NOW), [])

    def test_rolls_up_ten_or_more_into_one_major_finding(self):
        addrs = [self.address(name=f"a{i}") for i in range(10)]
        hits = fw.check_idle_address(addrs, set(), project="p", now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["severity"], "major")

    def test_a_regional_address_carries_the_region_scope_flag(self):
        hits = fw.check_idle_address([self.address()], set(), project="p", now=NOW)
        self.assertIn("--region=us-central1", hits[0]["excerpt"])

    def test_a_global_address_carries_the_global_scope_flag(self):
        """gcloud omits `region` entirely for a global address.

        Left to infer it, an agent writes the remediation with no scope flag at
        all, gcloud resolves it against its configured default region, and the
        command answers `was not found` on an address that is really there.
        """
        hits = fw.check_idle_address([self.address(region=None)], set(), project="p", now=NOW)
        self.assertIn("--global", hits[0]["excerpt"])
        self.assertNotIn("--region", hits[0]["excerpt"])

    def test_a_region_selflink_is_reduced_to_its_name(self):
        """The list API returns `region` as a full URL, not `us-central1`."""
        url = "https://www.googleapis.com/compute/v1/projects/p/regions/us-east4"
        hits = fw.check_idle_address([self.address(region=url)], set(), project="p", now=NOW)
        self.assertIn("--region=us-east4", hits[0]["excerpt"])
        self.assertNotIn("googleapis", hits[0]["excerpt"])

    def test_the_rollup_names_a_location_not_a_url(self):
        url = "https://www.googleapis.com/compute/v1/projects/p/regions/us-east4"
        addrs = [self.address(name=f"a{i}", region=url) for i in range(10)]
        hits = fw.check_idle_address(addrs, set(), project="p", now=NOW)
        self.assertIn("us-east4", hits[0]["excerpt"])
        self.assertNotIn("googleapis", hits[0]["excerpt"])

    def test_the_rollup_is_named_after_the_project_not_one_members_region(self):
        """§3.5's roll-up is per project and §5 forbids naming it after a member.

        The old `Address/rollup-<region of the first address>` was both: it
        claimed a region for addresses that were not in it, and it moved --
        releasing that one address renamed the finding, so the ledger announced
        the same leak as resolved and then as new.
        """
        addrs = [self.address(name=f"a{i}", region="us-east4") for i in range(6)]
        addrs += [self.address(name=f"b{i}", region="europe-west1") for i in range(6)]
        hits = fw.check_idle_address(addrs, set(), project="adamparco-kage", now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "Project/adamparco-kage")
        # Both regions named, with their own counts -- not one region's name
        # over a total that spans two.
        self.assertIn("us-east4 (6)", hits[0]["excerpt"])
        self.assertIn("europe-west1 (6)", hits[0]["excerpt"])
        self.assertTrue(hits[0]["excerpt"].startswith("12 external addresses"))

    def test_the_rollup_identity_survives_releasing_one_member(self):
        many = [self.address(name=f"a{i}", region="us-east4") for i in range(11)]
        first = fw.check_idle_address(many, set(), project="p", now=NOW)
        # Release the one that used to decide the name, in the region that used
        # to decide the name.
        rest = [a for a in many if a["name"] != "a0"]
        rest[0]["region"] = "europe-west1"
        second = fw.check_idle_address(rest, set(), project="p", now=NOW)
        self.assertEqual(first[0]["object"], second[0]["object"])

    def test_the_rollup_excerpt_names_its_members(self):
        addrs = [self.address(name=f"a{i:02d}") for i in range(10)]
        hits = fw.check_idle_address(addrs, set(), project="p", now=NOW)
        self.assertIn("a00", hits[0]["excerpt"])
        self.assertIn("a09", hits[0]["excerpt"])

    def test_a_long_rollup_says_how_many_it_did_not_name(self):
        addrs = [self.address(name=f"a{i:03d}") for i in range(40)]
        hits = fw.check_idle_address(addrs, set(), project="p", now=NOW)
        self.assertIn(f"and {40 - fw.ROLLUP_EXCERPT_MEMBERS} more", hits[0]["excerpt"])


GIB = 1024**3


class RegistryNoCleanupTest(unittest.TestCase):
    """§3.14 -- the one waste class that grows while nobody touches it."""

    def repo(
        self,
        name="projects/acme/locations/us-east4/repositories/images",
        mode="STANDARD_REPOSITORY",
        size=90 * GIB,
        created="2026-06-02T00:00:00Z",
        policies=None,
        dry_run=None,
    ):
        r = {"name": name, "mode": mode, "format": "DOCKER", "createTime": created}
        if size is not None:
            # The API sends int64 as a JSON string; the collector has to cope.
            r["sizeBytes"] = str(size)
        if policies is not None:
            r["cleanupPolicies"] = policies
        if dry_run is not None:
            r["cleanupPolicyDryRun"] = dry_run
        return r

    def check(self, *repos):
        return fw.check_registry_no_cleanup(list(repos), project="acme", now=NOW)

    def test_flags_a_large_old_repository_with_no_policy(self):
        hits = self.check(self.repo())
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["object"], "ArtifactRegistryRepository/us-east4:images")
        self.assertIn("no cleanup policy", hits[0]["excerpt"])

    def test_the_object_is_the_short_name_not_the_resource_path(self):
        """`object` is half the finding's identity. The project is already the
        finding's `cluster`, so the resource path would only repeat it; the
        location stays, because a repository name is unique per location."""
        self.assertNotIn("projects/", self.check(self.repo())[0]["object"])

    def test_same_named_repositories_in_two_locations_are_two_objects(self):
        east = self.repo()
        west = self.repo(name="projects/acme/locations/us-west1/repositories/images")
        objects = {h["object"] for h in self.check(east, west)}
        self.assertEqual(objects, {"ArtifactRegistryRepository/us-east4:images", "ArtifactRegistryRepository/us-west1:images"})

    def test_a_live_policy_is_not_flagged(self):
        self.assertEqual(self.check(self.repo(policies={"keep-recent": {}})), [])

    def test_a_dry_run_policy_counts_as_no_policy(self):
        """The forgotten case. `cleanupPolicyDryRun` deletes nothing, and both
        the console and `gcloud ... list` render it as a configured policy, so a
        check that tested only for the key's presence would call it clean."""
        hits = self.check(self.repo(policies={"keep-recent": {}}, dry_run=True))
        self.assertEqual(len(hits), 1)
        self.assertIn("dry-run", hits[0]["excerpt"])
        self.assertIn("1 cleanup policy", hits[0]["excerpt"])

    def test_the_dry_run_count_is_pluralised(self):
        hits = self.check(self.repo(policies={"a": {}, "b": {}}, dry_run=True))
        self.assertIn("2 cleanup policies", hits[0]["excerpt"])

    def test_below_the_size_floor_is_not_flagged(self):
        self.assertEqual(self.check(self.repo(size=fw.REGISTRY_SIZE_FLOOR_BYTES - 1)), [])

    def test_below_the_age_floor_is_not_flagged(self):
        """One day of pushes over one day of age reads as a runaway, and a
        repository that new has no history to clean."""
        self.assertEqual(self.check(self.repo(created="2026-07-25T00:00:00Z")), [])

    def test_a_remote_repository_is_not_flagged(self):
        self.assertEqual(self.check(self.repo(mode="REMOTE_REPOSITORY")), [])

    def test_a_virtual_repository_is_not_flagged(self):
        """It stores nothing; its `sizeBytes` double-counts the repositories
        behind it, which this check reads on their own."""
        self.assertEqual(self.check(self.repo(mode="VIRTUAL_REPOSITORY")), [])

    def test_an_empty_repository_is_not_flagged(self):
        """`sizeBytes` is absent, not zero, on a repository holding nothing."""
        self.assertEqual(self.check(self.repo(size=None)), [])

    def test_an_unparseable_size_is_skipped_rather_than_raising(self):
        r = self.repo()
        r["sizeBytes"] = "not-a-number"
        self.assertEqual(self.check(r), [])

    def test_a_repository_with_no_create_time_is_skipped(self):
        self.assertEqual(self.check(self.repo(created="")), [])

    def test_size_alone_reaches_major(self):
        hits = self.check(self.repo(size=fw.REGISTRY_SIZE_MAJOR_BYTES, created="2025-01-01T00:00:00Z"))
        self.assertEqual(hits[0]["severity"], "major")

    def test_growth_alone_reaches_major(self):
        """100 GiB in 40 days is 2.5 GiB/day -- well under the size bar and
        still worth a `major`, because the bill only goes one way."""
        hits = self.check(self.repo(size=100 * GIB, created="2026-06-22T00:00:00Z"))
        self.assertLess(100 * GIB, fw.REGISTRY_SIZE_MAJOR_BYTES)
        self.assertEqual(hits[0]["severity"], "major")

    def test_large_but_slow_stays_minor(self):
        hits = self.check(self.repo(size=60 * GIB, created="2025-06-02T00:00:00Z"))
        self.assertEqual(hits[0]["severity"], "minor")

    def test_the_excerpt_carries_the_location_scope_flag(self):
        """§3.5's lesson in a different API. An Artifact Registry verb with no
        `--location` resolves against gcloud's configured default and answers
        `NOT_FOUND`, which reads as a repository somebody already cleaned up."""
        self.assertIn("--location=us-east4", self.check(self.repo())[0]["excerpt"])

    def test_the_excerpt_names_the_age_rather_than_only_the_timestamp(self):
        self.assertIn("(60d ago)", self.check(self.repo())[0]["excerpt"])


class OrphanLbTest(unittest.TestCase):
    def test_flags_forwarding_rule_targeting_deleted_service(self):
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout", "creationTimestamp": "2026-01-01T00:00:00Z"}
        hits = fw.check_orphan_lb([rule], [], [], set(), now=NOW)
        self.assertEqual(len(hits), 1)

    def test_does_not_flag_when_service_still_exists(self):
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout", "creationTimestamp": "2026-01-01T00:00:00Z"}
        self.assertEqual(fw.check_orphan_lb([rule], [], [], {"staging/checkout"}, now=NOW), [])

    def test_does_not_flag_multicluster_ingress(self):
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout multiclusteringress", "creationTimestamp": "2026-01-01T00:00:00Z"}
        self.assertEqual(fw.check_orphan_lb([rule], [], [], set(), now=NOW), [])

    def test_does_not_flag_a_psc_endpoint_rule(self):
        for extra in ({"target": "projects/p/regions/us-central1/serviceAttachments/sa1"}, {"pscConnectionId": "123"}):
            with self.subTest(extra=extra):
                rule = {"name": "fr1", "description": self.GKE_DESC, "creationTimestamp": "2026-01-01T00:00:00Z", **extra}
                self.assertEqual(fw.check_orphan_lb([rule], [], [], set(), now=NOW), [])

    def test_does_not_flag_an_internal_rule_whose_backend_service_is_live(self):
        bs = {"name": "bs1", "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-central1", "backends": [{"group": "ig1"}]}
        rule = {
            "name": "fr1",
            "description": self.GKE_DESC,
            "creationTimestamp": "2026-01-01T00:00:00Z",
            "loadBalancingScheme": "INTERNAL",
            "backendService": "https://www.googleapis.com/compute/v1/projects/p/regions/us-central1/backendServices/bs1",
        }
        self.assertEqual(fw.check_orphan_lb([rule], [], [bs], set(), now=NOW), [])
        # The controls: an empty backend service, one in another region, and
        # an external rule all still leave the rule orphaned.
        empty = {**bs, "backends": []}
        self.assertEqual(len([h for h in fw.check_orphan_lb([rule], [], [empty], set(), now=NOW) if h["object"].startswith("ForwardingRule")]), 1)
        elsewhere = {**bs, "region": "https://www.googleapis.com/compute/v1/projects/p/regions/us-east4"}
        self.assertEqual(len(fw.check_orphan_lb([rule], [], [elsewhere], set(), now=NOW)), 1)
        external = {**rule, "loadBalancingScheme": "EXTERNAL"}
        self.assertEqual(len(fw.check_orphan_lb([external], [], [bs], set(), now=NOW)), 1)

    def test_flags_empty_target_pool(self):
        hits = fw.check_orphan_lb([], [{"name": "tp1", "instances": []}], [], set(), now=NOW)
        self.assertEqual(hits[0]["object"], "TargetPool/global:tp1")

    def test_flags_empty_backend_service(self):
        hits = fw.check_orphan_lb([], [], [{"name": "bs1", "backends": []}], set(), now=NOW)
        self.assertEqual(hits[0]["object"], "BackendService/global:bs1")

    #: What the GKE service controller actually writes into a forwarding rule's
    #: description. The bare `key: value` form is what the SOP's prose example
    #: shows and what no live rule carries; while every test used it, the whole
    #: leg passed its tests while matching nothing in the fleet.
    GKE_DESC = '{"kubernetes.io/service-name":"staging/checkout","kubernetes.io/api-version":"v1"}'

    def test_the_json_description_gke_really_writes_is_matched(self):
        rule = {"name": "fr1", "description": self.GKE_DESC, "creationTimestamp": "2026-01-01T00:00:00Z"}
        hits = fw.check_orphan_lb([rule], [], [], set(), now=NOW)
        self.assertEqual(len(hits), 1)
        self.assertIn("staging/checkout", hits[0]["excerpt"])

    def test_a_live_service_still_suppresses_the_json_form(self):
        rule = {"name": "fr1", "description": self.GKE_DESC, "creationTimestamp": "2026-01-01T00:00:00Z"}
        self.assertEqual(fw.check_orphan_lb([rule], [], [], {"staging/checkout"}, now=NOW), [])

    def test_a_rule_with_no_kubernetes_description_is_still_skipped(self):
        rule = {"name": "tf-managed", "description": "managed by terraform", "creationTimestamp": "2026-01-01T00:00:00Z"}
        self.assertEqual(fw.check_orphan_lb([rule], [], [], set(), now=NOW), [])

    def test_does_not_flag_recent_forwarding_rule(self):
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout", "creationTimestamp": "2026-07-30T00:00:00Z"}
        self.assertEqual(fw.check_orphan_lb([rule], [], [], set(), now=NOW), [])

    REGION = "https://www.googleapis.com/compute/v1/projects/p/regions/us-east4"

    def test_a_regional_forwarding_rule_carries_its_scope_flag(self):
        """The remediation chain deletes the rule, so it needs to find it.

        Every resource in §3.6's chain is regional-or-global, and an unscoped
        `gcloud compute` verb resolves against whatever region gcloud is
        configured for -- answering `was not found` for a rule that is really
        there, which reads as already-remediated.
        """
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout", "creationTimestamp": "2026-01-01T00:00:00Z", "region": self.REGION}
        hits = fw.check_orphan_lb([rule], [], [], set(), now=NOW)
        self.assertIn("--region=us-east4", hits[0]["excerpt"])
        self.assertNotIn("googleapis", hits[0]["excerpt"])

    def test_a_global_forwarding_rule_carries_the_global_flag(self):
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout", "creationTimestamp": "2026-01-01T00:00:00Z"}
        hits = fw.check_orphan_lb([rule], [], [], set(), now=NOW)
        self.assertIn("--global", hits[0]["excerpt"])

    def test_a_target_pool_carries_its_region(self):
        hits = fw.check_orphan_lb([], [{"name": "tp1", "instances": [], "region": self.REGION}], [], set(), now=NOW)
        self.assertIn("--region=us-east4", hits[0]["excerpt"])

    def test_a_backend_service_carries_its_scope_flag(self):
        """A backend service is regional or global, and the listing tells them
        apart only by whether `region` is there at all."""
        regional = fw.check_orphan_lb([], [], [{"name": "bs1", "backends": [], "region": self.REGION}], set(), now=NOW)
        self.assertIn("--region=us-east4", regional[0]["excerpt"])
        glob = fw.check_orphan_lb([], [], [{"name": "bs2", "backends": []}], set(), now=NOW)
        self.assertIn("--global", glob[0]["excerpt"])


class AgeInExcerptTest(unittest.TestCase):
    """Every check that gates on age says the age it gated on.

    A check computes an age, decides with it, and then quoted the raw ISO
    timestamp. The model reading the manifest still has to say how long the
    thing has been idle -- that is the finding -- so it did the date arithmetic
    itself and got it wrong: `argocd-webhook-ip`, reserved 2026-08-02 and read
    2026-09-01, was published as "unused for 28 days". `adopt_collector_evidence`
    replaces the model's evidence with the collector's and leaves the title
    alone, so the wrong number outlives the correct evidence beside it.

    Each timestamp below is 2026-01-01 against a NOW of 2026-08-01: 212 days.
    """

    AGO = "(212d ago)"

    def test_orphan_pv_dates_the_phase_transition(self):
        pv = obj("PersistentVolume", "pv-1", **{"spec.persistentVolumeReclaimPolicy": "Retain", "status.phase": "Released", "spec.capacity": {"storage": "10Gi"}, "status.lastPhaseTransitionTime": "2026-01-01T00:00:00Z"})
        hits = fw.check_orphan_pv({"pvs": [pv], "pvcs": [], "statefulsets": []}, now=NOW)
        self.assertIn(self.AGO, hits[0]["excerpt"])

    def test_terminal_pods_dates_the_oldest_pod(self):
        pods = [obj("Pod", "p", ns="default", **{"status.phase": "Succeeded", "metadata.creationTimestamp": "2026-01-01T00:00:00Z"})]
        hits = fw.check_terminal_pods({"pods": pods, "jobs": [], "cronjobs": []}, now=NOW)
        self.assertIn(self.AGO, hits[0]["excerpt"])

    def test_a_ttl_less_job_dates_its_completion(self):
        job = obj("Job", "batch", ns="default", **{"status.succeeded": 1, "status.completionTime": "2026-01-01T00:00:00Z"})
        hits = fw.check_terminal_pods({"pods": [], "jobs": [job], "cronjobs": []}, now=NOW)
        self.assertIn(self.AGO, hits[0]["excerpt"])

    def test_an_unattached_disk_dates_the_detach(self):
        disk = {"name": "d1", "sizeGb": "200", "type": "pd-standard", "creationTimestamp": "2020-01-01T00:00:00Z", "lastDetachTimestamp": "2026-01-01T00:00:00Z", "zone": "us-central1-a", "users": []}
        hits = fw.check_unattached_disk([disk], set(), now=NOW)
        self.assertIn(f"unattached since 2026-01-01T00:00:00Z {self.AGO}", hits[0]["excerpt"])

    def test_an_idle_address_dates_its_reservation(self):
        addr = {"name": "a1", "address": "1.2.3.4", "addressType": "EXTERNAL", "status": "RESERVED", "purpose": "", "creationTimestamp": "2026-01-01T00:00:00Z", "region": "us-central1"}
        hits = fw.check_idle_address([addr], set(), project="p", now=NOW)
        self.assertIn(f"since 2026-01-01T00:00:00Z {self.AGO}", hits[0]["excerpt"])

    def test_an_orphan_forwarding_rule_dates_its_creation(self):
        rule = {"name": "fr1", "description": "kubernetes.io/service-name: staging/checkout", "creationTimestamp": "2026-01-01T00:00:00Z"}
        hits = fw.check_orphan_lb([rule], [], [], set(), now=NOW)
        self.assertIn(self.AGO, hits[0]["excerpt"])

    def test_the_age_belongs_to_the_address_it_is_printed_beside(self):
        """`idle` was a list of addresses and the age a loop variable left over
        from the filter pass, so reading it in the emit loop would have stamped
        every address with the last one's age. Two addresses of different ages,
        emitted separately, is the only shape that catches it."""
        old = {"name": "old", "address": "1.1.1.1", "addressType": "EXTERNAL", "status": "RESERVED", "creationTimestamp": "2026-01-01T00:00:00Z", "region": "us-central1"}
        new = {"name": "new", "address": "2.2.2.2", "addressType": "EXTERNAL", "status": "RESERVED", "creationTimestamp": "2026-07-01T00:00:00Z", "region": "us-central1"}
        by_name = {h["object"]: h["excerpt"] for h in fw.check_idle_address([old, new], set(), project="p", now=NOW)}
        self.assertIn("(212d ago)", by_name["Address/us-central1:old"])
        self.assertIn("(31d ago)", by_name["Address/us-central1:new"])

    def test_an_unreadable_timestamp_prints_no_age_rather_than_zero(self):
        """`_age_days` returns None for a timestamp it cannot parse, and "(0d
        ago)" would assert the thing went idle today."""
        self.assertEqual(fw._ago(None), "")

    def test_a_part_day_rounds_down_so_a_title_cannot_claim_the_threshold(self):
        """The second half of the same bug, one release later.

        Dating the excerpt stopped the model doing its own arithmetic, but
        `{age:.0f}` rounds to nearest, so the same `argocd-webhook-ip` -- 29.69
        days old when the 2026-09-01 cost run read it -- was published as "(30d
        ago)" and the model titled the finding "unused for 30+ days". A claim
        about a threshold, derived from a rounded number, and false. Anything
        under a whole day has to round down, or the collector hands the model
        the licence to state the threshold.
        """
        self.assertEqual(fw._ago(29.69), " (29d ago)")
        self.assertEqual(fw._ago(29.999), " (29d ago)")
        self.assertEqual(fw._ago(30.0), " (30d ago)")

    def test_an_age_that_predates_the_read_prints_zero_not_a_negative(self):
        """A clock that moved backwards between creation and read gives a
        negative age. "(-1d ago)" would say the object is from the future."""
        self.assertEqual(fw._ago(-0.5), " (0d ago)")


class CollectProjectComputeTest(unittest.TestCase):
    """The five gated project-scope reads, and what happens when one fails.

    Six reads happen here, not five: §3.14's Artifact Registry list is gated by
    itself, so it is deliberately absent from the "N of 5" arithmetic below.

    The `run` here emulates gcloud's *argument parser*, not just its API, which
    is the whole point: the disks read spent its entire life failing on a filter
    value gcloud would not accept, and a fake that answers every argv with `[]`
    cannot tell the difference between a command gcloud runs and one it rejects.
    """

    FACTS = {"pv_handles": set(), "referenced_addresses": set(), "service_names": set()}

    def run_with(self, fail: dict | None = None):
        fail = fail or {}

        def run(argv, **kwargs):
            # gcloud reads `--filter` followed by a token starting with `-` as
            # two flags and rejects the command for the argument it thinks is
            # missing. `--filter=-users:*` is one token and parses fine.
            for i, tok in enumerate(argv):
                if tok == "--filter" and (i + 1 >= len(argv) or argv[i + 1].startswith("-")):
                    return run_of(2, "", f"ERROR: (gcloud.compute.{argv[2]}.list) argument --filter: expected one argument")
            resource = argv[2] if len(argv) > 2 else ""
            if resource in fail:
                return run_of(1, "", fail[resource])
            return run_of(0, "[]")

        return run

    def test_the_disks_read_survives_gcloud_argument_parsing(self):
        target = fw.collect_project_compute("acme", True, self.FACTS, run=self.run_with(), now=NOW)
        self.assertEqual(target["outcome"], "collected")

    def test_the_collector_does_not_pass_a_filter_gcloud_would_reject(self):
        """Guards the fake as much as the collector. If the parser emulation
        above stopped rejecting the old spelling it would pass everything, and
        the test above would go green against a disks read that never ran."""
        broken = ["gcloud", "compute", "disks", "list", "--project", "acme", "--filter", "-users:*", "--format", "json"]
        self.assertEqual(self.run_with()(broken).rc, 2)

        seen = []

        def recording(argv, **kwargs):
            seen.append(argv)
            return self.run_with()(argv, **kwargs)

        fw.collect_project_compute("acme", True, self.FACTS, run=recording, now=NOW)
        disks = next(argv for argv in seen if argv[2] == "disks")
        self.assertIn("--filter=-users:*", disks)
        self.assertNotIn("--filter", disks)

    def test_a_failed_read_names_the_command_and_what_it_said(self):
        target = fw.collect_project_compute(
            "acme", True, self.FACTS, run=self.run_with(fail={"addresses": "PERMISSION_DENIED: compute.addresses.list"}), now=NOW
        )
        self.assertIn("1 of 5", target["limitations"])
        self.assertIn("gcloud compute addresses list", target["limitations"])
        self.assertIn("PERMISSION_DENIED", target["limitations"])
        reasons = {c["check"]: c["reason"] for c in target["checks_unevaluated"]}
        self.assertIn("PERMISSION_DENIED", reasons["idle-address"])

    def test_the_error_names_every_read_that_failed_not_just_the_first(self):
        target = fw.collect_project_compute(
            "acme", True, self.FACTS, run=self.run_with(fail={"addresses": "denied-a", "target-pools": "denied-t"}), now=NOW
        )
        self.assertIn("2 of 5", target["limitations"])
        self.assertIn("denied-a", target["limitations"])
        self.assertIn("denied-t", target["limitations"])

    def test_a_read_that_returns_no_stderr_still_names_its_command(self):
        target = fw.collect_project_compute("acme", True, self.FACTS, run=self.run_with(fail={"disks": ""}), now=NOW)
        self.assertIn("gcloud compute disks list", target["limitations"])
        self.assertIn("no stderr", target["limitations"])

    def test_withholding_orphan_lb_says_why_rather_than_just_dropping_it(self):
        """§6's roster half names the missing check on its own, so the gap reads
        "orphan-lb did not run" whatever this entry says. Without a reason that
        sends a reader hunting a broken gcloud read: all three compute reads
        succeeded here and the check was withheld deliberately."""
        target = fw.collect_project_compute("acme", False, self.FACTS, run=self.run_with(), now=NOW)
        self.assertEqual(target["outcome"], "collected")
        self.assertNotIn("orphan-lb", [c["check"] for c in target["commands"]])
        self.assertIn("orphan-lb", target["limitations"])

    def test_a_project_whose_clusters_all_read_carries_no_limitation(self):
        """The other half of the pair. A limitation set unconditionally would
        make every healthy run partial, which costs more than the silence did."""
        target = fw.collect_project_compute("acme", True, self.FACTS, run=self.run_with(), now=NOW)
        self.assertIn("orphan-lb", [c["check"] for c in target["commands"]])
        self.assertNotIn("limitations", target)

    #: rc-0 answers that parse but are not a list of objects. `{}` iterated
    #: to nothing and recorded the check as run clean; a string element
    #: crashed the first `.get`.
    NOT_OBJECT_LISTS = ("{}", json.dumps([{"name": "x"}, "y"]))

    def answering(self, resource: str, stdout: str):
        inner = self.run_with()

        def run(argv, **kwargs):
            if len(argv) > 2 and argv[2] == resource:
                return run_of(0, stdout)
            return inner(argv, **kwargs)

        return run

    def test_a_compute_read_that_is_not_a_list_of_objects_fails_the_gate(self):
        for resource in ("disks", "addresses", "forwarding-rules", "target-pools", "backend-services"):
            for stdout in self.NOT_OBJECT_LISTS:
                with self.subTest(resource=resource, stdout=stdout):
                    target = fw.collect_project_compute("acme", True, self.FACTS, run=self.answering(resource, stdout), now=NOW)
                    self.assertEqual([c["check"] for c in target["commands"]], ["registry-no-cleanup"])
                    self.assertEqual(
                        [c["check"] for c in target["checks_unevaluated"]],
                        ["idle-address", "orphan-lb", "unattached-disk"],
                    )
                    self.assertIn("1 of 5", target["limitations"])
                    self.assertIn(f"gcloud compute {resource} list", target["limitations"])
                    self.assertIn(fw.NOT_AN_OBJECT_LIST, target["limitations"])

    def test_a_registry_read_that_is_not_a_list_of_objects_is_unevaluated(self):
        for stdout in self.NOT_OBJECT_LISTS:
            with self.subTest(stdout=stdout):
                target = fw.collect_project_compute("acme", True, self.FACTS, run=self.answering("repositories", stdout), now=NOW)
                self.assertNotIn("registry-no-cleanup", [c["check"] for c in target["commands"]])
                reasons = {c["check"]: c["reason"] for c in target["checks_unevaluated"]}
                self.assertIn(fw.NOT_AN_OBJECT_LIST, reasons["registry-no-cleanup"])
                self.assertIn("registry-no-cleanup was not evaluated", target["limitations"])
                self.assertIn("unattached-disk", [c["check"] for c in target["commands"]])

    def test_the_registry_read_is_recorded_alongside_the_compute_ones(self):
        target = fw.collect_project_compute("acme", True, self.FACTS, run=self.run_with(), now=NOW)
        entry = next(c for c in target["commands"] if c["check"] == "registry-no-cleanup")
        self.assertIn("gcloud artifacts repositories list", entry["command"])

    def test_a_failed_registry_read_does_not_gate_the_compute_checks(self):
        """The whole reason §3.14 is outside the five-read gate. The Artifact
        Registry API is enabled per project and the read needs its own IAM
        permission, so on a project that has neither this must cost one check
        rather than `unattached-disk` and `idle-address` as well."""
        target = fw.collect_project_compute(
            "acme", True, self.FACTS, run=self.run_with(fail={"repositories": "PERMISSION_DENIED"}), now=NOW
        )
        self.assertEqual(target["outcome"], "collected")
        self.assertIn("unattached-disk", [c["check"] for c in target["commands"]])
        self.assertNotIn("registry-no-cleanup", [c["check"] for c in target["commands"]])
        self.assertIn("registry-no-cleanup was not evaluated", target["limitations"])
        self.assertIn("PERMISSION_DENIED", target["limitations"])

    def test_a_failed_compute_read_leaves_the_registry_check_running(self):
        """The other direction, as SOP §3.14 states it: a failed compute read
        does not take the registry check with it. The five that cross-reference
        each other still gate as one, so all three compute checks go."""
        target = fw.collect_project_compute(
            "acme", True, self.FACTS, run=self.run_with(fail={"disks": "denied"}), now=NOW
        )
        self.assertEqual(target["outcome"], "collected")
        self.assertEqual([c["check"] for c in target["commands"]], ["registry-no-cleanup"])
        self.assertEqual(
            [c["check"] for c in target["checks_unevaluated"]], ["idle-address", "orphan-lb", "unattached-disk"]
        )
        self.assertIn("1 of 5", target["limitations"])
        self.assertNotIn("checks_not_applicable", target)

    def test_a_failed_compute_read_files_the_registry_finding(self):
        repo = {
            "name": "projects/acme/locations/us/repositories/big", "format": "DOCKER", "mode": "STANDARD_REPOSITORY",
            "sizeBytes": str(600 * 1024**3), "createTime": "2026-01-01T00:00:00Z",
        }
        inner = self.run_with(fail={"disks": "denied"})

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "artifacts", "repositories"]:
                return run_of(0, json.dumps([repo]))
            return inner(argv, **kwargs)

        target = fw.collect_project_compute("acme", True, self.FACTS, run=run, now=NOW)
        self.assertEqual([c["check"] for c in target["candidates"]], ["registry-no-cleanup"])

    def test_a_failed_compute_and_registry_read_gates_the_target(self):
        target = fw.collect_project_compute(
            "acme", True, self.FACTS, run=self.run_with(fail={"disks": "denied", "repositories": "denied-r"}), now=NOW
        )
        self.assertEqual(target["outcome"], "gate-failed")
        self.assertIn("1 of 5", target["error"])

    def test_a_withheld_orphan_lb_and_a_failed_registry_read_are_both_reported(self):
        """`limitations` is one string and the orphan-lb branch assigns it
        outright. Written in the other order the registry gap would vanish on
        any project holding an unreadable cluster, which is most of them."""
        target = fw.collect_project_compute(
            "acme", False, self.FACTS, run=self.run_with(fail={"repositories": "denied-r"}), now=NOW
        )
        self.assertIn("orphan-lb was not evaluated", target["limitations"])
        self.assertIn("registry-no-cleanup was not evaluated", target["limitations"])
        self.assertIn("idle-address was not evaluated", target["limitations"])
        self.assertEqual([c["check"] for c in target["checks_unevaluated"]], ["idle-address", "orphan-lb", "registry-no-cleanup"])

    def recording(self, fail=None):
        seen = []
        inner = self.run_with(fail)

        def run(argv, **kwargs):
            seen.append(argv)
            return inner(argv, **kwargs)

        return run, seen

    def test_a_rule_list_read_before_the_pool_is_not_read_again_here(self):
        """§3.13's traffic read needs these rules before any cluster is
        collected, so `collect_fleet` hoists the call and hands the answer down.
        Re-reading it would double a per-project gcloud call and leave two
        answers that can disagree about the same project."""
        run, seen = self.recording()
        pre = ([{"name": "rule-a", "IPAddress": "34.186.100.26", "loadBalancingScheme": "EXTERNAL", "region": "us-central1"}], run_of(0, "[]"))
        target = fw.collect_project_compute("acme", True, self.FACTS, run=run, now=NOW, forwarding_rules=pre)
        self.assertEqual(target["outcome"], "collected")
        self.assertEqual([argv for argv in seen if argv[2] == "forwarding-rules"], [])
        self.assertIn("orphan-lb", [c["check"] for c in target["commands"]])

    def test_without_a_pre_read_the_rules_are_still_read_here(self):
        """The fallback that keeps this file runnable on its own and keeps the
        eleven call sites that predate the hoist honest."""
        run, seen = self.recording()
        fw.collect_project_compute("acme", True, self.FACTS, run=run, now=NOW)
        self.assertEqual(len([argv for argv in seen if argv[2] == "forwarding-rules"]), 1)

    def test_a_pre_read_that_failed_still_fails_the_compute_gate(self):
        """§3.6 owns this read's failure whoever issued it. Passing the pair
        down must not turn a PERMISSION_DENIED into a project that collected
        cleanly with `orphan-lb` silently missing."""
        pre = (None, run_of(1, "", "PERMISSION_DENIED: compute.forwardingRules.list"))
        target = fw.collect_project_compute("acme", True, self.FACTS, run=self.run_with(), now=NOW, forwarding_rules=pre)
        self.assertNotIn("orphan-lb", {c["check"] for c in target["commands"]})
        self.assertIn("orphan-lb", {c["check"] for c in target["checks_unevaluated"]})
        self.assertIn("gcloud compute forwarding-rules list", target["limitations"])
        self.assertIn("PERMISSION_DENIED", target["limitations"])


class CollectClusterTest(unittest.TestCase):
    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}

    def run_with(self, dump_items=(), pools=(), session=None, cluster=None):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(*dump_items)))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, json.dumps(list(pools)))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                return fw.collect_cluster(cluster or self.CLUSTER, run=run, session=session or usage_session(), now=NOW)

    def test_the_object_dump_reads_ingresses(self):
        # §3.5's Ingress annotation is only seen if the dump holds Ingresses.
        seen = []

        def run(argv, **kwargs):
            if argv[:2] == ["kubectl", "get"]:
                seen.append(argv)
                return run_of(0, json.dumps(dump_of()))
            return run_of(0, "[]" if argv[:3] == ["gcloud", "container", "node-pools"] else "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                fw.collect_cluster(self.CLUSTER, run=run, session=usage_session(), now=NOW)
        self.assertIn("ingress", seen[0][2].split(","))

    def bump_candidates(self, autopilot):
        unsized = UnsizedWorkloadTest().pod()
        over = OverrequestTest().deployment_pod(cpu_req="3", mem_req="6Gi")
        session = usage_session(
            ("argocd", "argocd-repo-server-1", 0.01, 64.0),
            ("default", "api-1", 0.1, 100.0),
        )
        entry, _ = self.run_with(
            dump_items=[unsized, over],
            session=session,
            cluster={**self.CLUSTER, "autopilot": autopilot},
        )
        return {c["check"]: c for c in entry["candidates"]}

    def test_an_autopilot_bumped_sizing_finding_is_marked_for_triage(self):
        """The bump lifts both §3.1 and §3.12 to `major`, which clears the
        sweep's floor. The marker is what keeps a platform attribute from
        opening a pull request by itself."""
        candidates = self.bump_candidates(autopilot=True)
        for check in ("overrequest", "unsized-workload"):
            with self.subTest(check=check):
                self.assertEqual(candidates[check]["severity"], "major")
                self.assertEqual(candidates[check]["needs_triage"], fw.AUTOPILOT_BUMP_TRIAGE)

    def test_the_same_findings_off_autopilot_carry_no_marker(self):
        candidates = self.bump_candidates(autopilot=False)
        for check in ("overrequest", "unsized-workload"):
            with self.subTest(check=check):
                self.assertEqual(candidates[check]["severity"], "minor")
                self.assertIsNone(candidates[check]["needs_triage"])

    def test_the_bump_marker_is_one_the_sweep_withholds(self):
        """The two files carry the string separately; a drift here would mark
        the finding and let the sweep open it anyway."""
        import audit_report

        self.assertEqual(fw.AUTOPILOT_BUMP_TRIAGE, "autopilot-bumped")
        self.assertIn(fw.AUTOPILOT_BUMP_TRIAGE, audit_report.NO_SWEEP_TRIAGE)
        self.assertIn(fw.IDLE_SERVICE_TRIAGE, audit_report.NO_SWEEP_TRIAGE)
        self.assertEqual(fw.IDLE_STANDDOWN_TRIAGE, "scale-to-zero")
        self.assertIn(fw.IDLE_STANDDOWN_TRIAGE, audit_report.NO_SWEEP_TRIAGE)
        self.assertEqual(fw.GUARANTEED_QOS_TRIAGE, "guaranteed-qos")
        self.assertIn(fw.GUARANTEED_QOS_TRIAGE, audit_report.NO_SWEEP_TRIAGE)

    def test_a_stand_down_no_service_selects_is_marked_scale_to_zero(self):
        """No Service means no `service-fronted` marker, but the fix still
        takes the controller to zero on an idle reading, which is a judgement
        the sweep must not make for a reader."""
        idle = IdleWorkloadTest()
        entry, _ = self.run_with(
            dump_items=[idle.pod(), obj("Deployment", "hello-world", ns=idle.NS, **{"spec.replicas": 1})],
            session=usage_session((idle.NS, idle.POD, 0.0021, 6.0)),
        )
        found = [c for c in entry["candidates"] if c["check"] == "idle-workload"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["needs_triage"], fw.IDLE_STANDDOWN_TRIAGE)

    def test_a_guaranteed_overrequest_is_marked_guaranteed_qos(self):
        """Wins over the bump marker on Autopilot, too: the model needs it to
        publish the fix as `manual`, and both keep it out of the sweep."""
        over = OverrequestTest().deployment_pod(cpu_req="3", mem_req="6Gi", cpu_lim="3", mem_lim="6Gi")
        for autopilot in (False, True):
            with self.subTest(autopilot=autopilot):
                entry, _ = self.run_with(
                    dump_items=[over],
                    session=usage_session(("default", "api-1", 0.1, 100.0)),
                    cluster={**self.CLUSTER, "autopilot": autopilot},
                )
                found = [c for c in entry["candidates"] if c["check"] == "overrequest"]
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["needs_triage"], fw.GUARANTEED_QOS_TRIAGE)

    def test_every_outcome_publishes_the_mode(self):
        # The mode is a cluster property `enumerate_clusters` already resolved,
        # so it rides on the error shapes too: a cluster does not stop being
        # Autopilot because this run failed to read inside it.
        cluster = {**self.CLUSTER, "autopilot": True}

        def denied(argv, **kwargs):
            return run_of(1, "", "denied") if "get-credentials" in argv else run_of(0, "")

        def gated(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(1, "", "forbidden")
            return run_of(0, "")

        entries = [self.run_with(cluster=cluster)[0]]
        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                for runner in (denied, gated):
                    entries.append(
                        fw.collect_cluster(cluster, run=runner, session=usage_session(), now=NOW)[0]
                    )
        self.assertEqual(
            [e["outcome"] for e in entries], ["collected", "unreachable", "gate-failed"]
        )
        for entry in entries:
            with self.subTest(outcome=entry["outcome"]):
                self.assertIs(entry["autopilot"], True)

    def test_a_cluster_that_never_ran_still_publishes_the_mode(self):
        entry = fw.not_running_entry(
            {"name": "dr-west", "location": "us-west1", "status": "DEGRADED",
             "autopilot": {"enabled": True}},
            "acme",
        )
        self.assertEqual(entry["outcome"], "unreachable")
        self.assertIs(entry["autopilot"], True)
        self.assertIs(
            fw.not_running_entry({"name": "c", "status": "STOPPING"}, "acme")["autopilot"], False
        )

    def test_clean_cluster_collects_with_no_candidates(self):
        entry, facts = self.run_with()
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual(entry["candidates"], [])
        self.assertIn("overrequest", {c["check"] for c in entry["commands"]})

    def test_get_credentials_failure_is_unreachable(self):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(1, "", "denied")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, facts = fw.collect_cluster(self.CLUSTER, run=run, session=usage_session(), now=NOW)
        self.assertEqual(entry["outcome"], "unreachable")
        self.assertEqual(facts, {"pv_handles": set(), "service_names": set(), "referenced_addresses": set()})

    def test_a_dump_with_no_items_list_is_gate_failed_not_empty(self):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps({"kind": "Status"}))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, facts = fw.collect_cluster(self.CLUSTER, run=run, session=usage_session(), now=NOW)
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("items", entry["error"])

    def test_object_dump_failure_is_gate_failed(self):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(1, "", "forbidden")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, _ = fw.collect_cluster(self.CLUSTER, run=run, session=usage_session(), now=NOW)
        self.assertEqual(entry["outcome"], "gate-failed")

    def _metrics_down(self, *dump_items, session=None):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(*dump_items)))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, _ = fw.collect_cluster(
                    self.CLUSTER, run=run, session=session or FakeSession(**NO_USAGE), now=NOW
                )
        return entry

    def test_metrics_unavailable_still_collects_object_checks(self):
        # The node matters: without one this is the empty-cluster case below,
        # where the check is not applicable rather than degraded.
        entry = self._metrics_down(obj("Node", "node-1"))
        self.assertEqual(entry["outcome"], "collected")
        # All three read the same peak query, so they are lost together.
        self.assertNotIn("overrequest", {c["check"] for c in entry["commands"]})
        self.assertNotIn("unsized-workload", {c["check"] for c in entry["commands"]})
        self.assertNotIn("idle-workload", {c["check"] for c in entry["commands"]})
        # Dropping the check out of `commands` is only half the job: §6 raises
        # it as a gap either way, and without this the ledger named a check
        # nobody could explain.
        self.assertIn(
            "overrequest, unsized-workload and idle-workload could not be measured",
            entry["limitations"],
        )
        self.assertIn("Cloud Monitoring", entry["limitations"])

    def test_a_denied_usage_read_says_so_rather_than_saying_no_data(self):
        # An IAM gap and a cluster that ships no metrics both stop the check,
        # but only one of them is something an operator can fix, so the
        # limitation has to carry which it was.
        entry = self._metrics_down(
            obj("Node", "node-1"),
            session=FakeSession(status=403, text="caller lacks monitoring.timeSeries.list"),
        )
        self.assertIn("rc=403", entry["limitations"])
        self.assertIn("monitoring.timeSeries.list", entry["limitations"])

    def test_a_cluster_with_no_nodes_cannot_be_over_requesting(self):
        # An empty cluster is not a degraded one. The usage read comes back
        # empty there because nothing ran to report any, and reading that as
        # lost coverage published `partial: true` over two freshly created
        # Autopilot peers on 2026-08-29 -- a gap naming a check that had no
        # object to run against.
        #
        # All four sizing checks take the same exemption, and for the same
        # reason: neither an over- nor an under-sized request can exist where
        # nothing is scheduled, an absent one cannot be sized from a history
        # that was never recorded, and a controller that does not exist cannot
        # be the one nobody is using.
        entry = self._metrics_down()
        self.assertEqual(entry["outcome"], "collected")
        self.assertNotIn("limitations", entry)
        self.assertEqual(
            {na["check"] for na in entry["checks_not_applicable"]},
            {"overrequest", "underrequest", "unsized-workload", "idle-workload"},
        )
        for na in entry["checks_not_applicable"]:
            self.assertIn("no nodes", na["reason"])

    def test_an_empty_cluster_whose_read_failed_is_a_gap_not_an_exemption(self):
        """The exemption above rests on the read having come back — empty, but
        come back. A read that never completed says nothing about the cluster,
        and the same run says so out loud for every cluster that has nodes: a
        403 that produces "no metrics because nothing ran on it" here and
        "read failed (rc=403)" three clusters down is one report making two
        claims about one failure."""
        entry = self._metrics_down(
            session=FakeSession(status=403, text="caller lacks monitoring.timeSeries.list"),
        )
        self.assertNotIn("checks_not_applicable", entry)
        self.assertIn("rc=403", entry["limitations"])
        self.assertIn("overrequest, unsized-workload and idle-workload could not be measured",
                      entry["limitations"])
        self.assertIn("underrequest could not be measured", entry["limitations"])

    def test_a_transport_failure_does_not_exempt_an_empty_cluster(self):
        # rc -1: the arm a credential fault at startup or a dropped connection
        # takes. It fails every cluster's read at once, so this is where one
        # fault would have become a fleet of structural verdicts.
        entry = self._metrics_down(
            session=FakeSession(raises=ConnectionError("connection reset")),
        )
        self.assertNotIn("checks_not_applicable", entry)
        self.assertIn("rc=-1", entry["limitations"])
        self.assertIn("connection reset", entry["limitations"])

    def test_a_failed_usage_read_marks_its_checks_unevaluated(self):
        """So `finish` can refuse a document that files them as not applicable."""
        entry = self._metrics_down(obj("Node", "node-1"))
        self.assertEqual(
            [c["check"] for c in entry["checks_unevaluated"]],
            ["idle-workload", "overrequest", "underrequest", "unsized-workload"],
        )

    def test_an_unevaluated_reason_names_the_read_once_and_only_the_read_issued(self):
        # The phrase already opens "the Cloud Monitoring usage read ...", and
        # with the peak read down the mean read was never made.
        entry = self._metrics_down(obj("Node", "node-1"), session=FakeSession(raises=ConnectionError("connection reset")))
        for item in entry["checks_unevaluated"]:
            with self.subTest(check=item["check"]):
                self.assertEqual(item["reason"].count("Cloud Monitoring"), 1, item["reason"])
                self.assertIn("usage read failed", item["reason"])
                self.assertNotIn("mean-memory", item["reason"])
        self.assertNotIn("mean-memory", entry["limitations"])

    def test_an_empty_answer_is_not_described_as_a_failure(self):
        """rc 0 is a 200 that carried no series — the cluster is not shipping
        system metrics. Reporting that as `failed (rc=0)` asked the reader to
        reconcile a failure with the exit status that means success."""
        entry = self._metrics_down(obj("Node", "node-1"))
        self.assertIn("not shipping system metrics", entry["limitations"])
        self.assertNotIn("rc=0", entry["limitations"])
        self.assertNotIn("failed", entry["limitations"])

    def test_an_empty_mean_read_beside_a_usage_read_that_answered_is_not_a_cluster_without_metrics(self):
        """The usage read's records say the cluster ships system metrics, so
        an empty mean-memory answer described as "not shipping system metrics"
        contradicted them in the same entry."""
        empty_means = ({}, False, fw.Run(["curl ..."], 0, "", "no time series", 0.0))
        with patch.object(fw, "fetch_memory_means", return_value=empty_means):
            entry = self._metrics_down(obj("Node", "node-1"), session=usage_session())
        reasons = {c["check"]: c["reason"] for c in entry["checks_unevaluated"]}
        self.assertEqual(set(reasons), {"underrequest"})
        self.assertIn("although the usage read for the same cluster did", reasons["underrequest"])
        self.assertNotIn("not shipping system metrics", entry["limitations"])

    def test_one_usage_metric_empty_beside_the_other_leaves_the_peak_checks_unevaluated(self):
        """Every pod is unmeasured on the empty dimension, so `_measured_peaks`
        skips every controller; recorded as run, the three checks read clean
        over a cluster nothing measured."""
        peak_checks = {"overrequest", "unsized-workload", "idle-workload"}
        for missing, present, session in (
            ("memory", "CPU", FakeSession(cpu=[series_of("default", "api-1", 0.3)], mem=[])),
            ("CPU", "memory", FakeSession(cpu=[], mem=[series_of("default", "api-1", 512 * MIB)])),
        ):
            with self.subTest(missing=missing):
                entry = self._metrics_down(obj("Node", "node-1"), session=session)
                reasons = {c["check"]: c["reason"] for c in entry["checks_unevaluated"]}
                self.assertTrue(peak_checks <= set(reasons), reasons)
                for slug in peak_checks:
                    self.assertIn(f"returned no {missing} container time series", reasons[slug])
                    self.assertIn(f"although it returned {present} series", reasons[slug])
                self.assertFalse(peak_checks & {c["check"] for c in entry["commands"]})
                self.assertIn(
                    f"idle-workload could not be measured on this cluster: the Cloud Monitoring usage read returned no {missing}",
                    entry["limitations"],
                )

    def test_underrequest_still_runs_when_only_the_cpu_metric_is_empty(self):
        # Its figure is the mean-memory read; the CPU peak is not an input.
        session = FakeSession(cpu=[], mem=[series_of("default", "api-1", 512 * MIB)])
        entry = self._metrics_down(obj("Node", "node-1"), session=session)
        self.assertIn("underrequest", {c["check"] for c in entry["commands"]})
        self.assertNotIn("underrequest", {c["check"] for c in entry.get("checks_unevaluated", [])})

    def _unreadable_pools(self, cluster=None, answer=None):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return answer or run_of(1, "", "PERMISSION_DENIED: container.nodePools.list")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                return fw.collect_cluster(
                    cluster or self.CLUSTER, run=run, session=usage_session(), now=NOW
                )

    def test_an_unreadable_node_pool_list_is_not_an_absence_of_idle_pools(self):
        """The purest silent-clean shape this collector had.

        `node-pools list` was run bare, so a denied or throttled read parsed to
        `[]`, and a cluster with no node pools has no idle ones. Both 3.7 and
        3.8 recorded their command and reported nothing found. The evidence
        line carried `rc=1` and nothing downstream reads it.
        """
        entry, _ = self._unreadable_pools()
        commands = {c["check"] for c in entry["commands"]}
        self.assertNotIn("idle-nodepool", commands)
        self.assertNotIn("scaledown-blocked", commands)
        self.assertEqual(entry["outcome"], "collected")

    def test_a_node_pool_answer_that_is_not_a_list_is_an_unread_list(self):
        """An error object at rc=0: one key read as a sole pool and dispositioned
        both checks not-applicable; two keys crashed the cluster on a string.
        A list holding a non-object is as unread, not a pool dropped unseen."""
        for answer in ({"error": "denied"}, {"error": "denied", "code": 403}, [{"name": "p1"}, "p2"]):
            with self.subTest(answer=answer):
                entry, _ = self._unreadable_pools(answer=run_of(0, json.dumps(answer)))
                self.assertEqual(entry["outcome"], "collected")
                self.assertEqual(
                    [c["check"] for c in entry["checks_unevaluated"]], ["idle-nodepool", "scaledown-blocked"]
                )
                self.assertNotIn("checks_not_applicable", entry)
                self.assertIn("not a JSON list of node pools (rc=0)", entry["limitations"])

    def test_the_unreadable_pool_list_says_why(self):
        entry, _ = self._unreadable_pools()
        self.assertIn("idle-nodepool and scaledown-blocked", entry["limitations"])
        self.assertIn("rc=1", entry["limitations"])
        self.assertIn("PERMISSION_DENIED", entry["limitations"])

    def test_an_unreadable_pool_list_marks_both_pool_checks_unevaluated(self):
        entry, _ = self._unreadable_pools()
        self.assertEqual([c["check"] for c in entry["checks_unevaluated"]], ["idle-nodepool", "scaledown-blocked"])

    def test_an_unreadable_pool_list_leaves_the_object_checks_alone(self):
        """A degradation, not a gate failure: the object dump still backs 3.1–3.4."""
        entry, _ = self._unreadable_pools()
        commands = {c["check"] for c in entry["commands"]}
        for slug in ("orphan-pv", "unconsumed-pvc", "terminal-pods", "idle-namespace", "overrequest"):
            self.assertIn(slug, commands)

    def test_autopilot_with_unreadable_pools_claims_no_pool_limitation(self):
        """Autopilot owns its pools, so 3.7/3.8 are inapplicable rather than
        unmeasured — naming them in `limitations` would raise a gap for a check
        the cluster does not owe."""
        entry, _ = self._unreadable_pools({**self.CLUSTER, "autopilot": True})
        self.assertNotIn("limitations", entry)

    def test_autopilot_never_lists_node_pools(self):
        # Nothing on the Autopilot branch reads the answer.
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                fw.collect_cluster({**self.CLUSTER, "autopilot": True}, run=run, session=usage_session(), now=NOW)
        self.assertFalse([argv for argv in calls if argv[:3] == ["gcloud", "container", "node-pools"]])

    def test_a_readable_empty_pool_list_still_records_the_checks(self):
        """Zero pools is a measurement. It must not look like the failure above."""
        entry, _ = self.run_with(pools=[])
        commands = {c["check"] for c in entry["commands"]}
        self.assertIn("idle-nodepool", commands)
        self.assertIn("scaledown-blocked", commands)
        self.assertNotIn("limitations", entry)

    def test_autopilot_skips_idle_nodepool_and_scaledown_blocked(self):
        cluster = {**self.CLUSTER, "autopilot": True}

        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, _ = fw.collect_cluster(cluster, run=run, session=usage_session(), now=NOW)
        commands = {c["check"] for c in entry["commands"]}
        self.assertNotIn("idle-nodepool", commands)
        self.assertNotIn("scaledown-blocked", commands)

    def test_fleet_facts_carry_pv_handles_and_service_names(self):
        pv = obj("PersistentVolume", "pv1", **{"spec.csi": {"volumeHandle": "projects/p/disks/d1"}})
        regional = obj("PersistentVolume", "pv2", **{"spec.csi": {"volumeHandle": "projects/p/regions/us-central1/disks/d2"}})
        svc = obj("Service", "web", ns="default")
        entry, facts = self.run_with(dump_items=[pv, regional, svc])
        self.assertIn("d1", facts["pv_handles"])
        self.assertIn("us-central1/d2", facts["pv_handles"])
        self.assertIn("default/web", facts["service_names"])


class AutopilotNotApplicableTest(unittest.TestCase):
    """The two node-pool checks Autopilot cannot owe, declared by the collector.

    Leaving this to the model cost three false coverage gaps a week: it declared
    `idle-nodepool` not-applicable and forgot `scaledown-blocked`, and a check
    that is neither run nor dispositioned reads as one nobody performed.
    """

    def collect(self, autopilot: bool):
        cluster = {"name": "ap-1", "project": "acme", "location": "us-central1", "autopilot": autopilot}

        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, _ = fw.collect_cluster(cluster, run=run, session=usage_session(), now=NOW)
        return entry

    def test_autopilot_declares_both_node_pool_checks_not_applicable(self):
        entry = self.collect(autopilot=True)
        self.assertEqual(
            {na["check"] for na in entry["checks_not_applicable"]},
            {"idle-nodepool", "scaledown-blocked"},
        )

    def test_neither_check_is_also_reported_as_having_run(self):
        """A check cannot be both dispositioned and performed -- that is the
        contradiction `audit_report.cross_check_manifest` rejects downstream."""
        entry = self.collect(autopilot=True)
        ran = {c["check"] for c in entry["commands"]}
        self.assertNotIn("idle-nodepool", ran)
        self.assertNotIn("scaledown-blocked", ran)

    def test_every_not_applicable_entry_carries_a_reason(self):
        for na in self.collect(autopilot=True)["checks_not_applicable"]:
            self.assertTrue(na.get("reason", "").strip(), na)

    def test_a_standard_cluster_declares_nothing_not_applicable(self):
        """The disposition is Autopilot's alone. A Standard cluster owes both
        checks, so declaring them here would hide a real gap."""
        entry = self.collect(autopilot=False)
        self.assertNotIn("checks_not_applicable", entry)
        self.assertIn("idle-nodepool", {c["check"] for c in entry["commands"]})


class NodePoolAgeFromOperationsTest(unittest.TestCase):
    """§3.7's age exclusion reads the pool's `CREATE_NODE_POOL` operation."""

    CLUSTER = {"name": "two-usc1", "project": "acme", "location": "us-central1", "autopilot": False}
    POOLS = [{"name": "idle", "config": {"machineType": "e2-standard-4"}},
             {"name": "busy", "config": {"machineType": "e2-standard-4"}}]

    def collect(self, operations, cluster=None):
        calls = []
        node = obj("Node", "node-1", **{
            "metadata.labels": {"cloud.google.com/gke-nodepool": "idle"},
            "status.allocatable": {"cpu": "4", "memory": "8Gi"},
            "metadata.creationTimestamp": (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        })

        def run(argv, **kwargs):
            calls.append(argv)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(node)))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, json.dumps(self.POOLS))
            if argv[:3] == ["gcloud", "container", "operations"]:
                return run_of(0, json.dumps(operations))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, _ = fw.collect_cluster(cluster or self.CLUSTER, run=run, session=usage_session(), now=NOW)
        return entry, calls

    def test_the_read_is_filtered_to_this_cluster_s_pool_creations(self):
        _, calls = self.collect([])
        ops = [a for a in calls if a[:3] == ["gcloud", "container", "operations"]]
        self.assertEqual(len(ops), 1)
        self.assertIn("operationType=CREATE_NODE_POOL AND targetLink~/clusters/two-usc1/nodePools/", ops[0])

    def test_a_pool_with_no_recent_creation_is_judged_despite_new_nodes(self):
        entry, _ = self.collect([])
        self.assertIn("NodePool/idle", {c["object"] for c in entry["candidates"] if c["check"] == "idle-nodepool"})
        self.assertNotIn("limitations", entry)

    def test_an_operations_read_that_is_not_a_list_falls_back_to_node_age(self):
        # The node is a day old, so node age spares the pool; read as "no
        # recent creation", the object answer judged it idle.
        # A string element was skipped the same way. `{}` was never taken for
        # a list by `node_pool_creation_ages`; it stays as a guard that an
        # empty object keeps falling back rather than reading as no creation.
        for answer in ({"operations": []}, {}, ["op"]):
            with self.subTest(answer=answer):
                entry, _ = self.collect(answer)
                self.assertNotIn("idle-nodepool", {c["check"] for c in entry["candidates"]})
                self.assertIn("operations read failed", entry.get("limitations", ""))

    def test_a_young_cluster_s_pools_are_dated_from_its_create_time(self):
        young = {**self.CLUSTER, "create_time": (NOW - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S+00:00")}
        entry, _ = self.collect([], cluster=young)
        self.assertNotIn("idle-nodepool", {c["check"] for c in entry["candidates"]})

    def test_the_cluster_list_carries_each_cluster_s_create_time(self):
        listing = [{"name": "c1", "location": "us-central1", "status": "RUNNING", "createTime": "2026-09-26T00:00:00+00:00"}]

        def run(argv, **kwargs):
            self.assertIn("createTime", argv[-1])
            return run_of(0, json.dumps(listing))

        running, _ = fw.enumerate_clusters("acme", run=run)
        self.assertEqual(running[0]["create_time"], "2026-09-26T00:00:00+00:00")


class SoleNodePoolNotApplicableTest(unittest.TestCase):
    """Autopilot's disposition reached from the other direction.

    §3.7 will not flag a cluster's only node pool, so `check_idle_nodepool`
    drops out before measuring and `scaledown-blocked` gets no idle pool to
    read. The collector recorded both commands anyway, so issue #113 published
    an rc=0 `node-pools list` against `idle-nodepool` for all eleven Standard
    clusters on the fleet when ten of them have a single pool -- a denominator
    of one presented to the reader as eleven.
    """

    CLUSTER = {"name": "sole-usc1", "project": "acme", "location": "us-central1", "autopilot": False}
    POOL = {"name": "default-pool", "config": {"machineType": "e2-standard-4"}}

    def collect(self, pools):
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(obj("Node", "node-1"))))
            if argv[:3] == ["gcloud", "container", "node-pools"]:
                return run_of(0, json.dumps(pools))
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                entry, _ = fw.collect_cluster(
                    self.CLUSTER, run=run, session=usage_session(), now=NOW
                )
        return entry

    def test_a_sole_node_pool_declares_both_checks_not_applicable(self):
        entry = self.collect([self.POOL])
        self.assertEqual(
            {na["check"] for na in entry["checks_not_applicable"]},
            {"idle-nodepool", "scaledown-blocked"},
        )

    def test_neither_check_is_also_reported_as_having_run(self):
        """The double-count that made the gap invisible: a check cannot be both
        dispositioned and performed."""
        entry = self.collect([self.POOL])
        ran = {c["check"] for c in entry["commands"]}
        self.assertNotIn("idle-nodepool", ran)
        self.assertNotIn("scaledown-blocked", ran)

    def test_the_reason_names_the_sole_pool_rather_than_a_failure(self):
        """A reader has to be able to tell this from the unreadable-pools
        degradation, which is something they can go and fix."""
        for na in self.collect([self.POOL])["checks_not_applicable"]:
            self.assertIn("single node pool", na["reason"])
        self.assertNotIn("limitations", self.collect([self.POOL]))

    def test_two_pools_still_run_both_checks(self):
        """The disposition is the sole pool's alone -- a cluster that owes the
        checks must still be seen to owe them."""
        entry = self.collect([self.POOL, {**self.POOL, "name": "second-pool"}])
        ran = {c["check"] for c in entry["commands"]}
        self.assertIn("idle-nodepool", ran)
        self.assertIn("scaledown-blocked", ran)
        self.assertNotIn("checks_not_applicable", entry)

    def test_zero_pools_stays_a_measurement(self):
        """The boundary this fix must not cross. An empty pool list is a real
        read over an empty set, so it keeps recording the commands; only
        *exactly* one pool is the structural exemption."""
        entry = self.collect([])
        ran = {c["check"] for c in entry["commands"]}
        self.assertIn("idle-nodepool", ran)
        self.assertIn("scaledown-blocked", ran)
        self.assertNotIn("checks_not_applicable", entry)


class FleetConcurrencyTest(unittest.TestCase):
    def test_clusters_are_collected_in_parallel_up_to_the_pool_size(self):
        """Per-cluster work runs concurrently, not one cluster after another.

        This used to rendezvous on the injected `sleep` and assert the pool
        grew to the whole fleet, because each cluster held a ten-minute
        sampling window and serializing those would have taken hours. Nothing
        sleeps now, so the pool is back to the stream's usual 8 and the
        invariant worth holding is the plain one: a fleet larger than the pool
        still saturates it.

        The gate counts callers inside the Monitoring read and releases them
        once `max_workers` are in flight at once, so it cannot deadlock on an
        uneven split of clusters across workers the way a `threading.Barrier`
        sized to a wave would.
        """
        cluster_count, workers = 12, 4
        clusters_json = json.dumps(
            [
                {"name": f"c{i}", "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}}
                for i in range(cluster_count)
            ]
        )
        lock, saturated = threading.Lock(), threading.Event()
        state = {"live": 0, "peak": 0}

        class GatedSession:
            def get(self, url, params=None, timeout=None):
                with lock:
                    state["live"] += 1
                    state["peak"] = max(state["peak"], state["live"])
                    if state["live"] >= workers:
                        saturated.set()
                saturated.wait(timeout=10)
                with lock:
                    state["live"] -= 1
                is_cpu = "cpu/core_usage_time" in params["filter"]
                return FakeResponse(200, {"timeSeries": [series_of("d", "p", 1.0 if is_cpu else MIB)]})

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=GatedSession(), max_workers=workers, now=NOW)

        self.assertTrue(saturated.is_set(), "never reached the pool size; collection was serialized")
        self.assertGreaterEqual(state["peak"], workers)
        self.assertEqual(len({c["name"] for c in manifest["clusters"]} & {f"acme/us-central1/c{i}" for i in range(cluster_count)}), cluster_count)


class CrashIsolationTest(unittest.TestCase):
    def test_one_cluster_crashing_costs_that_cluster_and_no_other(self):
        """`future.result()` re-raises, and the SOP redirects this collector's
        stdout into the manifest — so an unmodelled exception on one cluster
        used to leave a zero-byte file and lose the whole fleet."""
        clusters_json = json.dumps(
            [
                {"name": "c1", "location": "us-central1", "status": "RUNNING"},
                {"name": "boom", "location": "us-central1", "status": "RUNNING"},
            ]
        )

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                if "boom" in str((kwargs.get("env") or {}).get("KUBECONFIG", "")):
                    raise TypeError("unsupported operand type(s) for /: 'str' and 'str'")
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)

        outcomes = {c["name"]: c["outcome"] for c in manifest["clusters"] if c["name"] in ("acme/us-central1/c1", "acme/us-central1/boom")}
        self.assertEqual(outcomes, {"acme/us-central1/c1": "collected", "acme/us-central1/boom": "gate-failed"})
        boom = next(c for c in manifest["clusters"] if c["name"] == "acme/us-central1/boom")
        self.assertIn("TypeError", boom["error"])


    def test_a_crashing_project_read_costs_that_project_and_no_other(self):
        """The project pools had no `crashed_entry`: a 200 whose body is not
        JSON, or a disk with a non-numeric size, raised out of the pool and
        left the SOP's redirect a zero-byte manifest. The disk is skipped on
        its own now, so the crash is raised by the read itself."""
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nbeta\n")
            if argv[:3] == ["gcloud", "container", "clusters"]:
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "disks"] and argv[argv.index("--project") + 1] == "beta":
                raise ValueError("could not convert string to float: 'ten'")
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            raise AssertionError(argv)

        manifest = fw.collect_fleet(None, run=run, session=usage_session(), now=NOW)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["project/acme"]["outcome"], "collected")
        self.assertEqual(by_name["project/beta"]["outcome"], "gate-failed")
        self.assertIn("ValueError", by_name["project/beta"]["error"])

    def test_a_crashing_cluster_list_costs_that_project_and_no_other(self):
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nbeta\n")
            if argv[:3] == ["gcloud", "container", "clusters"]:
                # A cluster with no name: `enumerate_clusters` indexes it.
                return run_of(0, "[]" if argv[argv.index("--project") + 1] == "acme" else '[{"status": "RUNNING"}]')
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            raise AssertionError(argv)

        manifest = fw.collect_fleet(None, run=run, session=usage_session(), now=NOW)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["project/acme"]["outcome"], "collected")
        self.assertEqual(by_name["project/beta"]["outcome"], "gate-failed")
        self.assertIn("KeyError", by_name["project/beta"]["error"])


class DefaultRunTest(unittest.TestCase):
    def test_a_timed_out_childs_output_arrives_as_str(self):
        """`TimeoutExpired` carries the child's output as bytes even under
        `text=True`, and `enumerate_clusters` searches it with `in`."""
        exc = subprocess.TimeoutExpired(["gcloud"], 60, output=b"partial", stderr=b"SERVICE_DISABLED")
        with patch.object(fw.subprocess, "run", side_effect=exc):
            result = fw.default_run(["gcloud"])
        self.assertEqual((result.rc, result.stdout, result.stderr), (124, "partial", "SERVICE_DISABLED"))


class ZoneTimeoutTest(unittest.TestCase):
    CLUSTERS = json.dumps([{"name": "c1", "location": "us-central1-a", "status": "RUNNING"}])
    SILENT = "WARNING: The following zones did not respond: us-east1-b. List results may be incomplete."

    def test_a_silent_zone_is_an_incomplete_enumeration_carrying_what_arrived(self):
        def run(argv, **kwargs):
            return run_of(0, self.CLUSTERS, self.SILENT)

        with self.assertRaises(fw.IncompleteEnumeration) as caught:
            fw.enumerate_clusters("acme", run=run)
        self.assertEqual([c["name"] for c in caught.exception.running], ["c1"])
        self.assertIn("did not respond", str(caught.exception))

    def test_an_answer_that_is_not_a_list_of_clusters_fails_the_listing(self):
        """A non-empty object crashed on its string keys, and `{}` iterated
        nothing and read as a project with no cluster."""
        for answer in ('{"error": "denied"}', "{}", '[{"name": "c1", "status": "RUNNING"}, "c2"]'):
            with self.subTest(answer=answer):
                with self.assertRaisesRegex(RuntimeError, "not a list of clusters"):
                    fw.enumerate_clusters("acme", run=lambda argv, **kwargs: run_of(0, answer))

    def test_a_silent_zone_audits_the_listed_clusters_and_fails_the_project_target(self):
        """The project checks read the cluster list to tell an orphan from
        something a cluster still owns, so a silent zone's cluster would make
        its disks and forwarding rules read as orphans."""
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, self.CLUSTERS, self.SILENT)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                raise AssertionError(f"a project read ran over an incomplete cluster list: {argv}")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["acme/us-central1-a/c1"]["outcome"], "collected")
        self.assertEqual(by_name["project/acme"]["outcome"], "gate-failed")
        self.assertIn("did not respond", by_name["project/acme"]["error"])


class GetTargetProjectsTest(unittest.TestCase):
    def test_project_override_skips_discovery(self):
        def run(argv, **kwargs):
            raise AssertionError(f"unexpected discovery call: {argv}")

        projects, partial = fw.get_target_projects("acme-only", run=run)
        self.assertEqual(projects, ["acme-only"])
        # Discovery was skipped, so the run cannot vouch for the rest of the fleet.
        self.assertIn("acme-only", partial)

    def test_discovers_every_listed_project_and_lists_none_of_them(self):
        # A cluster-free project stays in scope: §3.4-§3.6 and §3.14 look for
        # what its last cluster left behind. Listing each candidate's clusters
        # here was also a serial read per project before any worker started.
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nother\nempty\n")
            raise AssertionError(f"discovery read more than the project list: {argv}")

        self.assertEqual(fw.get_target_projects(None, run=run), (["acme", "other", "empty"], None))

    def test_the_listing_runs_under_its_own_timeout_not_the_default(self):
        """Under the 60 s default, a credential that sees hundreds of projects
        was killed mid-listing and the run read the active project alone."""
        timeouts = {}

        def run(argv, **kwargs):
            timeouts[argv[1]] = kwargs.get("timeout")
            return run_of(0, "acme\n")

        fw.get_target_projects(None, run=run)
        self.assertEqual(timeouts["projects"], fw.PROJECTS_LIST_TIMEOUT_S)
        self.assertGreater(fw.PROJECTS_LIST_TIMEOUT_S, fw.DEFAULT_TIMEOUT_S)
        self.assertLess(fw.PROJECTS_LIST_TIMEOUT_S, fw.PROJECT_READ_DEADLINE_S)

    def test_a_credential_that_sees_no_project_is_an_error_not_an_empty_fleet(self):
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "")
            raise AssertionError(argv)

        manifest = fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)
        self.assertEqual(manifest["error"], fw.NO_PROJECT_IN_SCOPE_ERROR)
        self.assertEqual(manifest["clusters"], [])

    def test_no_active_project_and_a_failed_listing_is_an_error(self):
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(1, "", "permission denied")
            raise AssertionError(argv)

        manifest = fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)
        self.assertIn("permission denied", manifest["error"])
        self.assertEqual(manifest["clusters"], [])

    def test_listed_projects_that_hold_no_cluster_are_read_not_an_error(self):
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "proj-a\nproj-b\n")
            if argv[:3] == ["gcloud", "container", "clusters"]:
                return run_of(0, "[]")
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            raise AssertionError(argv)

        manifest = fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)
        self.assertNotIn("error", manifest)
        self.assertEqual(
            {(c["name"], c["outcome"]) for c in manifest["clusters"]},
            {("project/proj-a", "collected"), ("project/proj-b", "collected")},
        )

    def test_project_list_failure_falls_back_to_the_base_project(self):
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(1, "", "permission denied")
            raise AssertionError(argv)

        projects, partial = fw.get_target_projects(None, run=run)
        self.assertEqual(projects, ["acme"])
        self.assertIn("permission denied", partial)


    def test_a_listing_that_omits_the_active_project_is_partial(self):
        # rc 0 without the active project: the listing is filtered, so the
        # scope is provably short, as `collect.discover_fleet` reads it.
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "beta\n")
            raise AssertionError(argv)

        projects, partial = fw.get_target_projects(None, run=run)
        self.assertEqual(projects, ["acme", "beta"])
        self.assertIn("did not name the active project 'acme'", partial)

    def test_a_listing_that_names_the_active_project_is_complete(self):
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nbeta\n")
            raise AssertionError(argv)

        self.assertEqual(fw.get_target_projects(None, run=run), (["acme", "beta"], None))

    def test_a_project_with_every_api_disabled_leaves_no_target_and_no_gap(self):
        # Kept as a target, a project that can hold nothing any check looks
        # for is a row the document has to explain on every run, and an
        # organisation-wide credential sees many of them.
        disabled = "ERROR: SERVICE_DISABLED: {api} API has not been used in project beta"

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nbeta\ngamma\n")
            project = argv[argv.index("--project") + 1] if "--project" in argv else ""
            if argv[:3] == ["gcloud", "container", "clusters"]:
                if project == "beta":
                    return run_of(1, "", disabled.format(api="Kubernetes Engine"))
                if project == "gamma":
                    return run_of(1, "", "PERMISSION_DENIED: container.clusters.list")
                return run_of(0, "[]")
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                if project == "beta":
                    return run_of(1, "", disabled.format(api="Compute Engine" if argv[1] == "compute" else "Artifact Registry"))
                return run_of(0, "[]")
            raise AssertionError(argv)

        manifest = fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)
        outcomes = {c["name"]: c["outcome"] for c in manifest["clusters"]}
        # gamma's failure is not an answer, so it is recorded rather than dropped.
        self.assertEqual(outcomes, {"project/acme": "collected", "project/gamma": "gate-failed"})

    def test_a_cluster_free_listed_project_is_audited_for_what_its_clusters_left(self):
        # The project whose last cluster was deleted is where its disks become
        # orphans; dropped from scope, it wrote no row and a finding filed
        # there on an earlier run read as resolved.
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nbeta\n")
            if argv[:3] == ["gcloud", "container", "clusters"]:
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "disks"] and argv[argv.index("--project") + 1] == "beta":
                disk = {"name": "left-behind", "creationTimestamp": "2020-01-01T00:00:00Z", "sizeGb": "10", "type": "pd-standard", "zone": "z"}
                return run_of(0, json.dumps([disk]))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            raise AssertionError(argv)

        manifest = fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)
        beta = next(c for c in manifest["clusters"] if c["name"] == "project/beta")
        self.assertEqual(beta["outcome"], "collected")
        self.assertIn("unattached-disk", {c["check"] for c in beta["candidates"]})

    GKE_OFF = (
        "ERROR: (gcloud.container.clusters.list) ResponseError: code=403, message=Kubernetes Engine API "
        "has not been used in project {number} before or it is disabled. Reason: SERVICE_DISABLED"
    )

    def refusing_run(self, named: str, own: str = "123456789"):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[:3] == ["gcloud", "projects", "describe"]:
                return run_of(0, own + "\n")
            return run_of(1, "", self.GKE_OFF.format(number=named))

        return run, calls

    def test_the_active_project_with_the_gke_api_disabled_holds_no_cluster(self):
        run, _ = self.refusing_run("123456789")
        self.assertEqual(fw.enumerate_clusters("acme", run=run), ([], []))

    def test_a_refusal_naming_the_project_by_id_needs_no_describe(self):
        run, calls = self.refusing_run("acme")
        self.assertEqual(fw.enumerate_clusters("acme", run=run), ([], []))
        self.assertFalse([c for c in calls if c[:3] == ["gcloud", "projects", "describe"]])

    def test_a_refusal_naming_a_longer_project_id_is_a_failed_list(self):
        """A hyphen ends a word, so `\\b` after `acme` matched `acme-prod`'s
        refusal and read it as acme's own, marking acme cluster-free."""
        run, _ = self.refusing_run("acme-prod")
        with self.assertRaises(RuntimeError):
            fw.enumerate_clusters("acme", run=run)

    def test_a_refusal_naming_another_project_id_says_so(self):
        run, _ = self.refusing_run("acme-prod")
        with self.assertRaisesRegex(RuntimeError, r"names another project \('acme-prod'\)"):
            fw.enumerate_clusters("acme", run=run)

    def test_a_quota_project_s_refusal_is_a_failed_list(self):
        """With `billing/quota_project` set to a project whose GKE API is off,
        every listing is refused in that project's name. Read as this project's
        answer, every project was marked cluster-free and nothing was read."""
        run, _ = self.refusing_run("987654321")
        with self.assertRaisesRegex(RuntimeError, "quota project"):
            fw.enumerate_clusters("acme", run=run)

    def test_a_refusal_naming_no_project_is_a_failed_list(self):
        def run(argv, **kwargs):
            return run_of(1, "", "accessNotConfigured: Kubernetes Engine API is disabled")

        with self.assertRaises(RuntimeError):
            fw.enumerate_clusters("acme", run=run)

    def test_a_refusal_whose_project_number_cannot_be_read_is_a_failed_list(self):
        """And says the describe failed, with its stderr: calling it a quota
        project sent the operator after the wrong setting."""
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "projects", "describe"]:
                return run_of(1, "", "PERMISSION_DENIED: resourcemanager.projects.get")
            return run_of(1, "", self.GKE_OFF.format(number="123456789"))

        with self.assertRaises(RuntimeError) as raised:
            fw.enumerate_clusters("acme", run=run)
        self.assertIn("`gcloud projects describe acme` failed (rc=1)", str(raised.exception))
        self.assertIn("resourcemanager.projects.get", str(raised.exception))
        self.assertNotIn("quota project", str(raised.exception))


def cluster_free_run(compute=None, registry=None, projects="acme\n"):
    """A fleet of cluster-free projects whose compute and registry reads answer
    `compute(project)` / `registry(project)`, or `[]` when those return None."""
    def run(argv, **kwargs):
        if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
            return run_of(0, projects.split()[0] + "\n")
        if argv[:2] == ["gcloud", "projects"] and "list" in argv:
            return run_of(0, projects)
        project = argv[argv.index("--project") + 1]
        if argv[:3] == ["gcloud", "container", "clusters"]:
            return run_of(0, "[]")
        answer = (compute if argv[1] == "compute" else registry if argv[1] == "artifacts" else None)
        if answer is None:
            raise AssertionError(argv)
        return answer(project) or run_of(0, "[]")
    return run


class DisabledApiProjectTest(unittest.TestCase):
    COMPUTE_OFF = "ERROR: SERVICE_DISABLED: Compute Engine API has not been used in project acme"
    REGISTRY_OFF = "ERROR: accessNotConfigured: Artifact Registry API has not been used in project acme"

    def test_a_scoped_project_with_both_apis_off_does_not_blame_the_scope(self):
        """`--project acme`, whose Compute and Artifact Registry APIs are both
        off: nothing is collected, and the run error named the `--project`
        note as the first failure, which says nothing about why acme yielded
        nothing."""
        run = cluster_free_run(compute=lambda p: run_of(1, "", self.COMPUTE_OFF), registry=lambda p: run_of(1, "", self.REGISTRY_OFF))
        manifest = fw.collect_fleet("acme", run=run, session=FakeSession(**NO_USAGE), now=NOW)
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("nothing collected", manifest["error"])
        self.assertNotIn(fw.UNENUMERATED_PROJECTS_TARGET, manifest["error"])
        self.assertIn(f"First: {fw.NO_TARGET_REASON}", manifest["error"])

    def test_a_project_with_both_apis_off_is_logged_once(self):
        """The prefetch records the project's reads by walking the same path
        the replay then walks, so the line was said twice per run."""
        run = cluster_free_run(compute=lambda p: run_of(1, "", self.COMPUTE_OFF), registry=lambda p: run_of(1, "", self.REGISTRY_OFF))
        with patch.object(fw, "log") as logged:
            fw.collect_fleet("acme", run=run, session=FakeSession(**NO_USAGE), now=NOW)
        said = [c.args[0] for c in logged.call_args_list if "no project-scoped check applies" in c.args[0]]
        self.assertEqual(len(said), 1, said)

    def _no_project_entry_run(self, status="RUNNING"):
        """One cluster whose credentials fail, in a project whose registry read
        crashes, so no `project/<id>` entry is collected."""
        def run(argv, **kwargs):
            if "get-credentials" in argv:
                return run_of(1, "", "ERROR: credential broker unavailable")
            if argv[:2] == ["gcloud", "config"]:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "container", "clusters"]:
                return run_of(0, json.dumps([{"name": "c1", "location": "us-central1", "status": status}]))
            if argv[1] == "artifacts":
                raise RuntimeError("project read crashed")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                return fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)

    def test_a_credential_failed_cluster_keeps_the_manifest_buildable(self):
        """§2 retries an unreachable cluster by hand under a `limitations`
        note, so a fleet of them is not nothing collected."""
        manifest = self._no_project_entry_run()
        self.assertNotIn("error", manifest)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["acme/us-central1/c1"]["outcome"], "unreachable")

    def test_a_cluster_not_running_and_no_project_entry_is_nothing_collected(self):
        manifest = self._no_project_entry_run(status="DEGRADED")
        self.assertIn("nothing collected", manifest.get("error", ""))
        self.assertIn("and no running cluster", manifest["error"])

    def test_a_filtered_listing_is_not_named_as_why_nothing_was_collected(self):
        """A `projects list` that succeeded without naming the active project
        was read in full; its note says what the run may have missed, so the
        error gives the reason nothing was collected instead."""
        inner = cluster_free_run(compute=lambda p: run_of(1, "", self.COMPUTE_OFF.replace("acme", p)), registry=lambda p: run_of(1, "", self.REGISTRY_OFF.replace("acme", p)))

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "other\n")
            return inner(argv, **kwargs)

        manifest = fw.collect_fleet(run=run, session=FakeSession(**NO_USAGE), now=NOW)
        self.assertEqual(manifest["clusters"], [])
        self.assertNotIn(fw.UNENUMERATED_PROJECTS_TARGET, manifest["error"])
        self.assertIn(f"First: {fw.NO_TARGET_REASON}", manifest["error"])

    def test_a_failed_project_listing_is_named_when_nothing_is_collected(self):
        """Without `--project`, the discovery entry is a real failure: a
        `projects list` that failed took the rest of the fleet with it, and the
        run error names it rather than saying nothing recorded an error."""
        inner = cluster_free_run(compute=lambda p: run_of(1, "", self.COMPUTE_OFF), registry=lambda p: run_of(1, "", self.REGISTRY_OFF))

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(1, "", "ERROR: PERMISSION_DENIED")
            return inner(argv, **kwargs)

        manifest = fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)
        self.assertIn(f"First: {fw.UNENUMERATED_PROJECTS_TARGET}: `gcloud projects list` rc=1", manifest["error"])

    def test_compute_off_declares_its_checks_inapplicable_and_still_reads_the_registry(self):
        run = cluster_free_run(compute=lambda p: run_of(1, "", self.COMPUTE_OFF), registry=lambda p: None)
        entry = next(c for c in fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)["clusters"] if c["name"] == "project/acme")
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual([c["check"] for c in entry["commands"]], ["registry-no-cleanup"])
        self.assertEqual({c["check"] for c in entry["checks_not_applicable"]}, set(fw.COMPUTE_CHECKS))
        self.assertNotIn("checks_unevaluated", entry)
        self.assertNotIn("limitations", entry)

    def test_registry_off_is_inapplicable_not_a_coverage_gap(self):
        run = cluster_free_run(compute=lambda p: None, registry=lambda p: run_of(1, "", self.REGISTRY_OFF))
        entry = next(c for c in fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)["clusters"] if c["name"] == "project/acme")
        self.assertEqual([c["check"] for c in entry["checks_not_applicable"]], ["registry-no-cleanup"])
        self.assertEqual({c["check"] for c in entry["commands"]}, {"unattached-disk", "idle-address", "orphan-lb"})
        self.assertNotIn("checks_unevaluated", entry)
        self.assertNotIn("limitations", entry)

    def test_a_quota_project_s_compute_refusal_gates_and_declares_nothing_inapplicable(self):
        # A project with no cluster has nothing else to say the refusal is someone else's.
        run = cluster_free_run(compute=lambda p: run_of(1, "", self.COMPUTE_OFF.replace("acme", "quota-proj")), registry=lambda p: None)
        entry = next(c for c in fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)["clusters"] if c["name"] == "project/acme")
        self.assertEqual(set(fw.COMPUTE_CHECKS), {c["check"] for c in entry["checks_unevaluated"]})
        self.assertNotIn("checks_not_applicable", entry)

    def test_a_quota_project_s_registry_refusal_is_unevaluated_not_inapplicable(self):
        run = cluster_free_run(compute=lambda p: None, registry=lambda p: run_of(1, "", self.REGISTRY_OFF.replace("acme", "quota-proj")))
        entry = next(c for c in fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)["clusters"] if c["name"] == "project/acme")
        self.assertNotIn("registry-no-cleanup", {c["check"] for c in entry.get("checks_not_applicable", [])})
        self.assertIn("registry-no-cleanup", {c["check"] for c in entry.get("checks_unevaluated", [])})

    def test_one_compute_read_refused_for_another_reason_still_gates(self):
        # Only all five answering "disabled" says the project has no Compute
        # Engine; one denied read among them is a read that failed.
        calls = []

        def compute(project):
            calls.append(project)
            return run_of(1, "", "PERMISSION_DENIED" if len(calls) == 1 else self.COMPUTE_OFF)

        run = cluster_free_run(compute=compute, registry=lambda p: None)
        entry = next(c for c in fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)["clusters"] if c["name"] == "project/acme")
        self.assertEqual(set(fw.COMPUTE_CHECKS), {c["check"] for c in entry["checks_unevaluated"]})
        self.assertNotIn("checks_not_applicable", entry)


class ClustersListedMarkerTest(unittest.TestCase):
    """`clusters_listed: 0` is what lets `finish` tell a fleet with no clusters
    from a run that lost them, so only a completed, empty list may set it."""
    SILENT = ZoneTimeoutTest.SILENT

    def manifest(self, cluster_list):
        base = cluster_free_run(compute=lambda p: None, registry=lambda p: None, projects="acme\nbeta\n")

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "projects", "describe"]:
                return run_of(0, "123456789\n" if "acme" in argv else "222222222\n")
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                # beta completes empty in every run, so each one shows the marker set beside the entry under test.
                return cluster_list() if "acme" in argv else run_of(0, "[]")
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:3] in (["gcloud", "container", "node-pools"], ["gcloud", "container", "operations"]):
                return run_of(0, "[]")
            return base(argv, **kwargs)

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                return fw.collect_fleet(None, run=run, session=usage_session(), now=NOW)

    def project_entry(self, cluster_list):
        return next(c for c in self.manifest(cluster_list)["clusters"] if c["name"] == "project/acme")

    def test_a_completed_empty_list_marks_the_project(self):
        entry = self.project_entry(lambda: run_of(0, "[]"))
        self.assertEqual(entry["outcome"], "collected")
        self.assertEqual(entry[fw.CLUSTERS_LISTED_KEY], 0)

    def test_the_gke_api_off_is_an_empty_list_and_marks_the_project(self):
        entry = self.project_entry(lambda: run_of(1, "", GetTargetProjectsTest.GKE_OFF.format(number="123456789")))
        self.assertEqual(entry[fw.CLUSTERS_LISTED_KEY], 0)

    def test_a_quota_project_s_refusal_does_not_mark_the_project(self):
        manifest = self.manifest(lambda: run_of(1, "", GetTargetProjectsTest.GKE_OFF.format(number="987654321")))
        self.assertEqual([c["name"] for c in manifest["clusters"] if fw.CLUSTERS_LISTED_KEY in c], ["project/beta"])
        acme = next(c for c in manifest["clusters"] if c["name"] == "project/acme")
        self.assertEqual(acme["outcome"], "gate-failed")

    def test_a_failed_list_does_not_mark_the_project(self):
        manifest = self.manifest(lambda: run_of(1, "", "PERMISSION_DENIED: container.clusters.list"))
        self.assertEqual([c["name"] for c in manifest["clusters"] if fw.CLUSTERS_LISTED_KEY in c], ["project/beta"])

    def test_an_empty_list_with_a_silent_zone_does_not_mark_the_project(self):
        manifest = self.manifest(lambda: run_of(0, "[]", self.SILENT))
        self.assertEqual([c["name"] for c in manifest["clusters"] if fw.CLUSTERS_LISTED_KEY in c], ["project/beta"])

    def test_a_project_holding_a_cluster_is_not_marked(self):
        entry = self.project_entry(lambda: run_of(0, ZoneTimeoutTest.CLUSTERS))
        self.assertNotIn(fw.CLUSTERS_LISTED_KEY, entry)

    def test_a_project_whose_only_cluster_is_not_running_is_not_marked(self):
        entry = self.project_entry(lambda: run_of(0, json.dumps([{"name": "c1", "location": "us-central1-a", "status": "DEGRADED"}])))
        self.assertNotIn(fw.CLUSTERS_LISTED_KEY, entry)


class ProjectReadScaleTest(unittest.TestCase):
    def test_project_reads_run_in_the_pool(self):
        # Two projects' reads have to be in flight at once to pass the barrier,
        # at both phases; one project at a time breaks it instead of hanging.
        clusters_barrier = threading.Barrier(2, timeout=5)
        disks_barrier = threading.Barrier(2, timeout=5)

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nbeta\n")
            if argv[:3] == ["gcloud", "container", "clusters"]:
                clusters_barrier.wait()
                return run_of(0, "[]")
            if argv[:3] == ["gcloud", "compute", "disks"]:
                disks_barrier.wait()
            return run_of(0, "[]")

        manifest = fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW, max_workers=2)
        self.assertEqual({c["outcome"] for c in manifest["clusters"]}, {"collected"})

    def test_a_run_past_the_deadline_reads_nothing_and_says_so(self):
        # A run killed at its terminal timeout leaves no manifest; one that
        # stops starting projects leaves one naming what it skipped -- here
        # everything, which is the top-level error.
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nbeta\n")
            raise AssertionError(f"read a project after the deadline: {argv}")

        manifest = fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW, project_budget_s=0)
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("2 project(s)", manifest["error"])
        self.assertIn("not read:", manifest["error"])

    def test_a_slow_project_listing_spends_the_read_budget(self):
        """The clock starts before discovery, so a `projects list` that takes
        the whole budget leaves none for the reads; a deadline taken after
        discovery would read both projects and overrun the terminal timeout."""
        clock = [0.0]

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                clock[0] += fw.PROJECT_READ_DEADLINE_S + 1
                return run_of(0, "acme\nbeta\n")
            raise AssertionError(f"read a project after the deadline: {argv}")

        with patch.object(fw.time, "monotonic", lambda: clock[0]):
            manifest = fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("2 project(s)", manifest["error"])
        self.assertIn("not read:", manifest["error"])

    def test_a_deadline_reached_after_listing_fails_only_the_project_reads(self):
        # The listing is admitted and the clock then runs out: the cluster is
        # still read, and the project's own reads are what the run reports as
        # unread -- without one of them being made.
        compute_reads = []

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}]))
            if argv[:3] == ["gcloud", "compute", "forwarding-rules"]:
                return run_of(0, "[]")
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                compute_reads.append(argv)
            return run_of(0, "")

        admitted = iter([True])
        with TemporaryDirectory() as tmp, patch.object(fw, "KUBECONFIG_DIR", Path(tmp)), patch.object(fw, "_before", lambda deadline: next(admitted, False)):
            manifest = fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertIn("acme/us-central1/c1", by_name)
        self.assertEqual(by_name["project/acme"]["outcome"], "gate-failed")
        self.assertTrue(by_name["project/acme"]["error"].startswith("not read:"))
        self.assertEqual(compute_reads, [])

    def test_project_reads_share_the_cluster_pool(self):
        # The disk read and the cluster's credential fetch have to be in
        # flight together to pass the barrier.
        barrier = threading.Barrier(2, timeout=5)

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}]))
            if argv[:3] == ["gcloud", "compute", "disks"] or "get-credentials" in argv:
                barrier.wait()
            return run_of(0, "[]") if argv[0] == "gcloud" else run_of(0, "")

        with TemporaryDirectory() as tmp, patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
            manifest = fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW, max_workers=2)
        self.assertIn("acme/us-central1/c1", {c["name"] for c in manifest["clusters"]})
        self.assertEqual(next(c for c in manifest["clusters"] if c["name"] == "project/acme")["outcome"], "collected")

    def test_the_replay_makes_no_second_read(self):
        # Recording the project's reads in the pool and judging them after
        # it must not double the gcloud calls a project costs.
        seen = []

        def run(argv, **kwargs):
            seen.append(tuple(argv))
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\n")
            return run_of(0, "[]")

        fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)
        self.assertEqual(len(seen), len(set(seen)))

    def test_a_project_with_every_api_off_by_number_is_described_once(self):
        """The refusal branch is the one place a repeat argv is issued: the
        Kubernetes Engine, Compute Engine and Artifact Registry refusals each
        ask for the project's number. The fixture above answers `[]` and never
        reaches it."""
        seen = []
        refusal = "ERROR: SERVICE_DISABLED: {api} API has not been used in project 123456789 before or it is disabled"

        def run(argv, **kwargs):
            seen.append(tuple(argv))
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\n")
            if argv[:3] == ["gcloud", "projects", "describe"]:
                return run_of(0, "123456789\n")
            if argv[:2] == ["gcloud", "container"]:
                return run_of(1, "", refusal.format(api="Kubernetes Engine"))
            if argv[:2] == ["gcloud", "compute"]:
                return run_of(1, "", refusal.format(api="Compute Engine"))
            if argv[:2] == ["gcloud", "artifacts"]:
                return run_of(1, "", refusal.format(api="Artifact Registry"))
            raise AssertionError(argv)

        manifest = fw.collect_fleet(None, run=run, session=FakeSession(**NO_USAGE), now=NOW)
        # The refusals were read as the project's own: nothing to report.
        self.assertIn("error", manifest)
        describes = [a for a in seen if a[:3] == ("gcloud", "projects", "describe")]
        self.assertEqual(len(describes), 1)


class MultiProjectCollectFleetTest(unittest.TestCase):
    def test_discovers_and_audits_every_project_with_a_cluster(self):
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nbeta\n")
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                project = argv[argv.index("--project") + 1]
                name = "c1" if project == "acme" else "c2"
                cluster = {"name": name, "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}}
                return run_of(0, json.dumps([cluster]))
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet(None, run=run, session=usage_session(), now=NOW)

        names = {c["name"] for c in manifest["clusters"]}
        self.assertEqual(names, {"acme/us-central1/c1", "beta/us-central1/c2", "project/acme", "project/beta"})

    def test_cross_project_facts_do_not_leak(self):
        """A PV handle live in project acme's cluster must not suppress a
        genuinely unattached disk of the same name in project beta -- the
        cross-cluster fact union is scoped per project, not fleet-wide.
        Beta gets its own (PV-less) cluster, so the handle is the only thing
        that could clear the disk."""

        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nbeta\n")
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                project = argv[argv.index("--project") + 1]
                name = "c1" if project == "acme" else "c2"
                cluster = {"name": name, "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}}
                return run_of(0, json.dumps([cluster]))
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                kc = str(kwargs.get("env", {}).get("KUBECONFIG", ""))
                if "_acme_" in kc:
                    pv = obj("PersistentVolume", "pv1", **{"spec.csi": {"volumeHandle": "projects/acme/disks/shared-disk-id"}})
                    return run_of(0, json.dumps(dump_of(pv)))
                return run_of(0, json.dumps(dump_of()))
            if argv[:3] == ["gcloud", "compute", "disks"]:
                project = argv[argv.index("--project") + 1]
                if project == "beta":
                    disk = {"name": "shared-disk-id", "creationTimestamp": "2020-01-01T00:00:00Z", "sizeGb": "10", "type": "pd-standard", "zone": "z"}
                    return run_of(0, json.dumps([disk]))
                return run_of(0, "[]")
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet(None, run=run, session=usage_session(), now=NOW)

        beta_entry = next(c for c in manifest["clusters"] if c["name"] == "project/beta")
        self.assertIn("unattached-disk", {c["check"] for c in beta_entry["candidates"]})

    def test_a_project_whose_clusters_cannot_be_listed_is_recorded_not_skipped(self):
        # A `collected` compute entry and no clusters is exactly what a
        # genuinely cluster-free project looks like, so the failed enumeration
        # takes the project's own entry down with it.
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "acme\nbeta\n")
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                project = argv[argv.index("--project") + 1]
                if project == "beta":
                    return run_of(1, "", "PERMISSION_DENIED: container.clusters.list")
                cluster = {"name": "c1", "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}}
                return run_of(0, json.dumps([cluster]))
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet(None, run=run, session=usage_session(), now=NOW)

        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["project/beta"]["outcome"], "gate-failed")
        self.assertIn("PERMISSION_DENIED", by_name["project/beta"]["error"])
        self.assertEqual(by_name["project/acme"]["outcome"], "collected")
        # One entry per project name: audit_report rejects a duplicate.
        self.assertEqual(len(by_name), len(manifest["clusters"]))

    def test_a_failed_project_list_is_recorded_as_an_unenumerated_target(self):
        def run(argv, **kwargs):
            if argv[:2] == ["gcloud", "config"] and "get-value" in argv:
                return run_of(0, "acme\n")
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(1, "", "PERMISSION_DENIED: resourcemanager.projects.list")
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, "[]")
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        manifest = fw.collect_fleet(None, run=run, session=usage_session(), now=NOW)
        by_name = {c["name"]: c for c in manifest["clusters"]}
        entry = by_name[fw.UNENUMERATED_PROJECTS_TARGET]
        self.assertEqual(entry["outcome"], "gate-failed")
        self.assertIn("PERMISSION_DENIED", entry["error"])

    def test_disks_an_unreachable_cluster_may_own_are_withheld_with_a_limitation(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, json.dumps([
                    {"name": "c1", "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}},
                    {"name": "sick", "location": "us-east4", "status": "RUNNING", "autopilot": {"enabled": False}},
                ]))
            if "get-credentials" in argv:
                return run_of(1, "", "unreachable") if "sick" in argv else run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:3] == ["gcloud", "compute", "disks"]:
                disk = {"name": "pvc-1", "creationTimestamp": "2020-01-01T00:00:00Z", "sizeGb": "10", "type": "pd-standard", "zone": "us-east4-a", "labels": {fw.GKE_CLUSTER_LABEL: "sick"}}
                return run_of(0, json.dumps([disk]))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)
        project = next(c for c in manifest["clusters"] if c["name"] == "project/acme")
        self.assertNotIn("unattached-disk", {c["check"] for c in project["candidates"]})
        self.assertIn("unattached-disk skipped", project["limitations"])
        self.assertIn("sick", project["limitations"])

    def test_one_of_two_same_named_clusters_read_still_judges_unlabelled_disks(self):
        """`c1` in two locations, one read. The names alone say every `c1` went
        unread, which withheld the whole check and said none of the project's
        clusters could be read."""
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, json.dumps([
                    {"name": "c1", "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}},
                    {"name": "c1", "location": "us-east4", "status": "RUNNING", "autopilot": {"enabled": False}},
                ]))
            if "get-credentials" in argv:
                return run_of(1, "", "unreachable") if "us-east4" in argv else run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:3] == ["gcloud", "compute", "disks"]:
                return run_of(0, json.dumps([{"name": "boot", "creationTimestamp": "2020-01-01T00:00:00Z", "sizeGb": "10", "type": "pd-standard", "zone": "us-east4-a"}]))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)
        project = next(c for c in manifest["clusters"] if c["name"] == "project/acme")
        self.assertIn("unattached-disk", {c["check"] for c in project["candidates"]})
        self.assertNotIn("unattached-disk", {c["check"] for c in project.get("checks_unevaluated", [])})
        self.assertIn("(c1 in us-east4)", project["limitations"])
        self.assertNotIn("none of its clusters", project["limitations"])

    def test_a_project_with_no_readable_cluster_withholds_unattached_disk(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, json.dumps([{"name": "sick", "location": "us-east4", "status": "RUNNING", "autopilot": {"enabled": False}}]))
            if "get-credentials" in argv:
                return run_of(1, "", "unreachable")
            if argv[:3] == ["gcloud", "compute", "disks"]:
                return run_of(0, json.dumps([{"name": "boot", "creationTimestamp": "2020-01-01T00:00:00Z", "sizeGb": "10", "type": "pd-standard", "zone": "us-east4-a"}]))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)
        project = next(c for c in manifest["clusters"] if c["name"] == "project/acme")
        self.assertEqual(project["candidates"], [])
        self.assertNotIn("unattached-disk", {c["check"] for c in project["commands"]})
        self.assertIn("unattached-disk was not evaluated", project["limitations"])

    def test_an_unparseable_cluster_list_on_the_only_project_is_a_run_error(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, "WARNING: something printed to stdout")
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)
        # The only project, so nothing is left to audit and the run says so.
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("project/acme: cluster enumeration returned no parseable JSON", manifest["error"])

    def test_a_cluster_that_is_not_running_is_recorded_as_an_unreachable_target(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(
                    0,
                    json.dumps(
                        [
                            {"name": "c1", "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}},
                            {"name": "sick", "location": "us-east4", "status": "DEGRADED"},
                        ]
                    ),
                )
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)

        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["acme/us-east4/sick"]["outcome"], "unreachable")
        self.assertIn("DEGRADED", by_name["acme/us-east4/sick"]["error"])
        self.assertEqual(by_name["acme/us-central1/c1"]["outcome"], "collected")

    def test_a_reconciling_cluster_is_audited_rather_than_skipped(self):
        """GKE sets RECONCILING while work proceeds on a cluster whose API
        server is up, and any config change puts one there for minutes. Skipped
        as unreachable, a routine edit dropped the cluster from the whole audit
        -- which on this fleet meant losing the only cluster with more than one
        node pool, the only place `idle-nodepool` can run."""

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(
                    0,
                    json.dumps(
                        [
                            {"name": "busy", "location": "us-east4", "status": "RECONCILING", "autopilot": {"enabled": False}},
                            {"name": "gone", "location": "us-west1", "status": "PROVISIONING"},
                        ]
                    ),
                )
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)

        by_name = {c["name"]: c for c in manifest["clusters"]}
        self.assertEqual(by_name["acme/us-east4/busy"]["outcome"], "collected")
        self.assertEqual(by_name["acme/us-west1/gone"]["outcome"], "unreachable")
        self.assertIn("PROVISIONING", by_name["acme/us-west1/gone"]["error"])

    def fleet_with(self, clusters, orphan_rule=True):
        rule = {
            "name": "fr1",
            "description": '{"kubernetes.io/service-name":"gone/svc"}',
            "creationTimestamp": "2026-01-01T00:00:00Z",
        }

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, json.dumps(clusters))
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:4] == ["gcloud", "compute", "forwarding-rules", "list"]:
                return run_of(0, json.dumps([rule] if orphan_rule else []))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)
        return {c["name"]: c for c in manifest["clusters"]}

    RUNNING = {"name": "c1", "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}}
    DEGRADED = {"name": "sick", "location": "us-east4", "status": "DEGRADED"}

    def test_a_readable_fleet_evaluates_orphan_lb(self):
        by_name = self.fleet_with([self.RUNNING])
        project = by_name["project/acme"]
        self.assertIn("orphan-lb", {c["check"] for c in project["commands"]})
        self.assertIn("orphan-lb", {c["check"] for c in project["candidates"]})
        self.assertNotIn("limitations", project)

    def test_a_degraded_cluster_closes_the_orphan_lb_gate_for_its_project(self):
        """§3.6 runs only if *every* cluster in the project was enumerated, and
        a DEGRADED cluster goes straight into `scope.skipped` per §2. The gate
        was read off the RUNNING clusters alone, so a project with a cluster in
        `scope.skipped` still published `orphan-lb` findings against a Service
        list it had not finished collecting -- the false positive §3.6 calls the
        highest-risk cross-check in the audit."""
        by_name = self.fleet_with([self.RUNNING, self.DEGRADED])
        project = by_name["project/acme"]
        self.assertNotIn("orphan-lb", {c["check"] for c in project["commands"]})
        self.assertNotIn("orphan-lb", {c["check"] for c in project["candidates"]})
        self.assertIn("orphan-lb was not evaluated", project["limitations"])
        # §3.5 needs the union too: an unread cluster's Service or Ingress
        # can name an address. §3.4 narrows instead, and the registry is
        # project-wide.
        self.assertEqual(
            {c["check"] for c in project["commands"]},
            {"unattached-disk", "registry-no-cleanup"},
        )
        self.assertIn("idle-address was not evaluated", project["limitations"])
        self.assertIn("idle-address", {e["check"] for e in project["checks_unevaluated"]})

    def test_an_unlistable_only_project_is_a_run_error(self):
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(1, "", "PERMISSION_DENIED")
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)
        self.assertEqual(manifest["clusters"], [])
        self.assertIn("PERMISSION_DENIED", manifest["error"])


class LbTrafficReachesTheIdleCheckTest(unittest.TestCase):
    """The hoist: rules read once per project, before the worker pool.

    §3.13 has to turn a Service's external address into a forwarding-rule name
    before any cluster is collected, and only `gcloud compute forwarding-rules
    list` holds both halves of that join. So the read moved out of
    `collect_project_compute`, which runs after the pool, and the answer is
    handed down to both consumers. The two things that can go wrong are
    invisible in a report: reading it twice, or reading it in the right place
    and never passing it to the check.
    """

    IP = "34.186.100.26"
    RULE = {"name": "rule-a", "IPAddress": IP, "loadBalancingScheme": "EXTERNAL", "region": "us-central1"}

    class Session:
        """`usage_session`'s answers plus the three load-balancer counters."""

        def __init__(self, inner, ingress, egress_packets, egress_bytes):
            self.inner = inner
            self.lb = {
                fw.LB_INGRESS_PACKETS_METRIC: ingress,
                fw.LB_EGRESS_PACKETS_METRIC: egress_packets,
                fw.LB_EGRESS_BYTES_METRIC: egress_bytes,
            }
            self.lb_calls = 0

        def get(self, url, params=None, timeout=None):
            metric = params["filter"].split('"')[1]
            if metric not in self.lb:
                return self.inner.get(url, params=params, timeout=timeout)
            self.lb_calls += 1
            return FakeResponse(200, {"timeSeries": [lb_series("rule-a", self.lb[metric])]})

    def collect(self, *, ingress=500000, egress_packets=400000, egress_bytes=24000000, projects=("acme",)):
        idle = IdleWorkloadTest()
        items = [
            idle.pod(),
            obj("Deployment", "hello-world", ns=idle.NS, **{"spec.replicas": 1}),
            obj(
                "Service",
                "hello-world",
                ns=idle.NS,
                **{
                    "spec.type": "LoadBalancer",
                    "spec.selector": {"app": "hello-world"},
                    "status.loadBalancer.ingress": [{"ip": self.IP}],
                },
            ),
        ]
        seen = []

        def run(argv, **kwargs):
            seen.append(argv)
            if argv[:2] == ["gcloud", "projects"] and "list" in argv:
                return run_of(0, "".join(f"{p}\n" for p in projects))
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                cluster = {"name": "c1", "location": "us-central1", "status": "RUNNING", "autopilot": {"enabled": False}}
                return run_of(0, json.dumps([cluster]))
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(*items)))
            if argv[:4] == ["gcloud", "compute", "forwarding-rules", "list"]:
                return run_of(0, json.dumps([self.RULE]))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        session = self.Session(
            usage_session((IdleWorkloadTest.NS, IdleWorkloadTest.POD, 0.0021, 6.0)),
            ingress,
            egress_packets,
            egress_bytes,
        )
        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet(
                    None if len(projects) > 1 else projects[0], run=run, session=session, now=NOW
                )
        return manifest, seen, session

    def cluster(self, manifest):
        return next(c for c in manifest["clusters"] if c["name"] == "acme/us-central1/c1")

    def idle_finding(self, manifest):
        return next(c for c in self.cluster(manifest)["candidates"] if c["check"] == "idle-workload")

    def test_the_rule_list_is_read_once_per_project(self):
        _, seen, _ = self.collect()
        self.assertEqual(len([a for a in seen if a[:4] == ["gcloud", "compute", "forwarding-rules", "list"]]), 1)

    def test_each_project_gets_its_own_read_and_its_own_traffic(self):
        _, seen, session = self.collect(projects=("acme", "beta"))
        reads = [a for a in seen if a[:4] == ["gcloud", "compute", "forwarding-rules", "list"]]
        self.assertEqual(sorted(a[a.index("--project") + 1] for a in reads), ["acme", "beta"])
        # Three metrics per project, and no fourth read for the second
        # consumer: §3.6 reuses the same list rather than re-querying.
        self.assertEqual(session.lb_calls, 6)

    def test_the_traffic_read_is_recorded_under_its_own_slug(self):
        """Not under `idle-workload`. That key holds the usage read, and
        `adopt_collector_evidence` publishes it as the command behind every
        finding on the check -- overwriting it would leave each sizing claim
        citing a load-balancer query."""
        manifest, _, _ = self.collect()
        commands = {c["check"]: c["command"] for c in self.cluster(manifest)["commands"]}
        self.assertIn("cpu/core_usage_time", commands["idle-workload"])
        self.assertIn("timeSeries", commands["idle-workload-traffic"])
        self.assertIn(fw.LB_RULE_LABEL, commands["idle-workload-traffic"])

    def test_the_traffic_slug_is_the_only_command_outside_the_sop_roster(self):
        """It is a read, not a check, and the distinction has teeth.

        `commands` is a lookup keyed by slug, so an extra entry costs nothing.
        The document's `checks_run` is validated against §3's roster and would
        reject the whole run for a slug that is not in it. Every other command
        this collector records is a roster check; this one is deliberately not,
        and the SOP tells the model so.
        """
        import audit_report

        manifest, _, _ = self.collect()
        roster = set(audit_report.audit_checks("fleet-wide-cost-analysis"))
        self.assertTrue(roster)
        slugs = {c["check"] for c in self.cluster(manifest)["commands"]}
        self.assertIn("idle-workload-traffic", slugs)
        self.assertEqual(slugs - roster, {"idle-workload-traffic"})

    def test_the_measured_traffic_reaches_the_published_excerpt(self):
        """End to end: rules read before the pool, joined to the Service's
        assigned address, and the answer written into the evidence the ledger
        carries. 24,000,000 bytes over 400,000 packets is 60 each -- headers
        with no payload, the shape of the two rules whose findings merged a
        stand-down on 2026-09-07."""
        manifest, _, _ = self.collect()
        excerpt = self.idle_finding(manifest)["excerpt"]
        self.assertIn("metered 500,000 inbound packets", excerpt)
        self.assertIn("60 bytes each", excerpt)
        self.assertIn("without ever sending a payload", excerpt)

    def test_a_rule_serving_payload_warns_in_the_published_excerpt(self):
        manifest, _, _ = self.collect(egress_bytes=400000 * 1500)
        self.assertIn("something is being served", self.idle_finding(manifest)["excerpt"])

    def test_the_finding_is_still_gated_for_triage_whatever_the_traffic_said(self):
        """Traffic informs the reader; it does not release the sweep. A rule
        under the noise floor is not evidence nothing needs the workload, and
        that population is exactly what merged PRs 184-186."""
        manifest, _, _ = self.collect(ingress=10, egress_packets=0, egress_bytes=0)
        finding = self.idle_finding(manifest)
        self.assertIn("nothing measurable reached it", finding["excerpt"])
        self.assertEqual(finding["needs_triage"], "service-fronted")


class MonitoringReadsGoThroughTheRelayTest(unittest.TestCase):
    """In the shell sandbox the only path to Monitoring is the broker's relay.

    The pod holds no Google identity, so a read the relay's table does not
    admit comes back 403 and the three usage checks go quiet fleet-wide with a
    limitation that reads like a Monitoring outage. Both halves are checked
    here: the session is the relay's when the broker is configured, and every
    URL a collection actually requests is one `api_policy` admits.
    """

    @classmethod
    def setUpClass(cls):
        for directory in fw.SHARED_SCRIPT_DIRS:
            if directory not in sys.path:
                sys.path.append(directory)

    def test_the_broker_endpoint_selects_the_relay_session(self):
        import credential_proxy_client

        # `ApiSession()` builds its default transport from `requests`, which
        # the test requirements do not declare; the sibling test in
        # `test_credential_proxy_client.py` skips on the same condition.
        try:
            import requests  # noqa: F401
        except ImportError:  # pragma: no cover - environment without requests
            self.skipTest("requests is not installed here; the sandbox image has it")
        with patch.dict(os.environ, {"CREDENTIAL_PROXY_URL": "http://broker:8765"}):
            session = fw.default_monitoring_session()
        self.assertIsInstance(session, credential_proxy_client.ApiSession)
        self.assertEqual(
            session.relay_url("https://monitoring.googleapis.com/v3/projects/acme/timeSeries"),
            "http://broker:8765/v1/gcp/monitoring.googleapis.com/v3/projects/acme/timeSeries",
        )

    def test_every_read_a_collection_makes_is_one_the_relay_admits(self):
        import api_policy
        from urllib.parse import urlsplit

        requested = []
        case = LbTrafficReachesTheIdleCheckTest()
        base = case.Session

        class Recording(base):
            def get(self, url, params=None, timeout=None):
                requested.append(url)
                return super().get(url, params=params, timeout=timeout)

        case.Session = Recording
        # A real-shaped id: the relay holds the project segment to Google's
        # grammar, which the four-letter fixture name elsewhere does not meet.
        case.collect(projects=("acme-prod",))
        self.assertTrue(requested)
        for url in sorted(set(requested)):
            parts = urlsplit(url)
            with self.subTest(url=url):
                decision = api_policy.evaluate("GET", parts.netloc, parts.path.lstrip("/"), parts.query)
                self.assertTrue(decision.allowed, decision.message)


class ManifestComposesWithAuditReportTest(unittest.TestCase):
    def test_checks_run_copied_from_a_collected_cluster_survives_cross_check(self):
        import audit_report

        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]) or "node-pools" in argv:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)

        cluster_entry = next(c for c in manifest["clusters"] if c["name"] == "acme/us-central1/c1")
        project_entry = next(c for c in manifest["clusters"] if c["name"] == "project/acme")
        data = {
            "audit": "fleet-wide-cost-analysis",
            "scope": {
                "clusters": [
                    {"name": "acme/us-central1/c1", "checks_run": [{"check": c["check"], "command": c["command"]} for c in cluster_entry["commands"]]},
                    {"name": "project/acme", "checks_run": [{"check": c["check"], "command": c["command"]} for c in project_entry["commands"]]},
                ],
                # `--project` skipped discovery, so the rest of the fleet is a
                # target the document has to account for.
                "skipped": [{"cluster": fw.UNENUMERATED_PROJECTS_TARGET, "reason": "scope narrowed by --project"}],
            },
        }
        audit_report.cross_check_manifest(data, manifest)  # must not raise

    def test_every_collected_command_passes_finish_s_command_check(self):
        """§2 says to copy `commands` verbatim into `checks_run`, and `finish`
        validates each one before it opens the manifest. A label it refuses --
        the Monitoring reads carry no argv -- would fail every run that read
        metrics, so each command the collector publishes must pass it."""
        import audit_report

        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if "forwarding-rules" in argv:
                # An external rule, so the traffic read is really sent and the
                # label validated here describes a request that was made.
                return run_of(0, json.dumps(FetchLbTrafficTest.RULES))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]) or "node-pools" in argv:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)

        checked = set()
        for entry in manifest["clusters"]:
            for c in entry.get("commands", []):
                with self.subTest(target=entry["name"], check=c["check"]):
                    audit_report.validate_check_command(c["command"], entry["name"], c["check"])
                checked.add(c["check"])
        self.assertIn("overrequest", checked)
        self.assertIn("idle-workload-traffic", checked)

    def test_a_project_with_no_external_rule_records_no_traffic_read(self):
        """No request is sent for a project with nothing to measure, so no
        cluster in it may carry an `idle-workload-traffic` command."""
        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]) or "node-pools" in argv:
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)

        cluster_entry = next(c for c in manifest["clusters"] if c["name"] == "acme/us-central1/c1")
        slugs = {c["check"] for c in cluster_entry["commands"]}
        self.assertIn("idle-workload", slugs)
        self.assertNotIn("idle-workload-traffic", slugs)

    def test_a_project_with_no_clusters_evaluates_orphan_lb(self):
        """No cluster means no Service can still claim a forwarding rule, so the
        project is fully read rather than withheld every week."""
        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, "[]")
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)
        project_entry = next(c for c in manifest["clusters"] if c["name"] == "project/acme")
        self.assertIn("orphan-lb", [c["check"] for c in project_entry["commands"]])
        self.assertNotIn("checks_unevaluated", project_entry)

    def test_a_check_absent_from_the_manifest_is_rejected(self):
        import audit_report

        clusters_json = json.dumps([{"name": "c1", "location": "us-central1", "status": "RUNNING"}])

        def run(argv, **kwargs):
            if argv[:3] == ["gcloud", "container", "clusters"] and "list" in argv:
                return run_of(0, clusters_json)
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of()))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with TemporaryDirectory() as tmp:
            with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
                manifest = fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW)

        # Faithful to the manifest in every other respect, so the one claim it
        # does not back is what the rejection is about: the pool read failed,
        # and idle-nodepool is not among c1's commands.
        cluster_entry = next(c for c in manifest["clusters"] if c["name"] == "acme/us-central1/c1")
        project_entry = next(c for c in manifest["clusters"] if c["name"] == "project/acme")
        self.assertNotIn("idle-nodepool", {c["check"] for c in cluster_entry["commands"]})
        claimed = [{"check": c["check"], "command": c["command"]} for c in cluster_entry["commands"]]
        claimed.append({"check": "idle-nodepool", "command": "gcloud container node-pools list --cluster c1"})
        data = {
            "audit": "fleet-wide-cost-analysis",
            "scope": {
                "clusters": [
                    {"name": "acme/us-central1/c1", "checks_run": claimed},
                    {"name": "project/acme", "checks_run": [{"check": c["check"], "command": c["command"]} for c in project_entry["commands"]]},
                ],
                "skipped": [{"cluster": fw.UNENUMERATED_PROJECTS_TARGET, "reason": "scope narrowed by --project"}],
            },
        }
        with self.assertRaisesRegex(audit_report.ValidationError, "idle-nodepool"):
            audit_report.cross_check_manifest(data, manifest)


class DeclarationIndexIsACopyTest(unittest.TestCase):
    """`workload_declarations` and `declaration_for` are duplicated from
    `collect.py` rather than imported. Every collector here runs standalone
    under `python3 <file>`; `fleet_stockout.py` imports a few leaf helpers
    from its siblings, but this one keeps its own copy, for the reason
    `fleet_waste.workload_declarations` gives.

    Duplication is the convention, and drift is what it costs: two collectors
    resolving the same object to different files, or one of them keeping a bug
    the other fixed, with nothing failing to say so. These tests are the guard
    the convention does not otherwise have -- they compare the executable code
    and ignore the docstrings, which differ on purpose because each copy argues
    from the findings its own stream published.
    """

    def test_the_internal_load_balancer_annotations_match_collect(self):
        """The §3.13 excerpt's copy of collect.py's internal-LB test reads the
        same annotations for the same value, so the two never disagree about
        whether a Service has an external IP."""
        import collect

        self.assertEqual(fw.INTERNAL_LB_ANNOTATIONS, collect._INTERNAL_LB_ANNOTATIONS)
        self.assertEqual(fw.INTERNAL_LB_ANNOTATION_VALUE, collect._INTERNAL_LB_ANNOTATION_VALUE)

    def bodies(self, name):
        """Both copies of one function, as ASTs with the docstring dropped."""
        import ast
        import importlib
        import inspect
        import textwrap

        out = []
        for module in (fw, importlib.import_module("collect")):
            tree = ast.parse(textwrap.dedent(inspect.getsource(getattr(module, name))))
            fn = tree.body[0]
            if (
                fn.body
                and isinstance(fn.body[0], ast.Expr)
                and isinstance(fn.body[0].value, ast.Constant)
                and isinstance(fn.body[0].value.value, str)
            ):
                fn.body = fn.body[1:]
            out.append(ast.dump(fn))
        return out

    def test_workload_declarations_matches_collect_py(self):
        mine, theirs = self.bodies("workload_declarations")
        self.assertEqual(mine, theirs, "the copy has drifted from collect.py's original")

    def test_declaration_for_matches_collect_py(self):
        mine, theirs = self.bodies("declaration_for")
        self.assertEqual(mine, theirs, "the copy has drifted from collect.py's original")

    def test_reconciler_of_matches_collect_py(self):
        # Under the same convention and with the same cost of drift: the phrase
        # this returns is published verbatim in a remediation note, so two
        # collectors naming the same Helm release differently is visible to a
        # reader and explicable to nobody.
        mine, theirs = self.bodies("reconciler_of")
        self.assertEqual(mine, theirs, "the copy has drifted from collect.py's original")

    def test_the_release_resolver_matches_collect_py(self):
        # Every half of the branch that resolves a rendered workload, under the
        # same convention. Drift here is worse than in the pair above, because
        # what these resolve to is a file a pull request rewrites: two
        # collectors disagreeing about which Application declares a release
        # means one of them opens a PR against the wrong chart.
        for name in ("release_of", "_argocd_chart_source", "_argocd_kustomize_source", "_argocd_values_field", "release_declarations", "release_declaration_for"):
            with self.subTest(function=name):
                mine, theirs = self.bodies(name)
                self.assertEqual(mine, theirs, "the copy has drifted from collect.py's original")

    def test_the_constants_it_reads_match_too(self):
        """An identical function over a different constant is a different
        function. `GITOPS_CLUSTER_TREE_DEPTH` in particular decides which files
        are indexed at all, so a copy that drifted here would index the whole
        repository or none of it and still pass the two tests above.
        """
        import importlib

        collect = importlib.import_module("collect")
        for const in (
            "GITOPS_CLUSTER_TREE_ROOT",
            "GITOPS_CLUSTER_TREE_DEPTH",
            "GIT_DIR_NAME",
            "KCC_API_GROUP_SUFFIX",
            "_HELM_RELEASE_ANNOTATION",
            "_HELM_NAMESPACE_ANNOTATION",
            "_ARGOCD_TRACKING_ANNOTATION",
            "_MANAGED_BY_LABEL",
            "_HELM_MANAGED_BY",
            "ARGOCD_APPLICATION_KIND",
            "ARGOCD_CLUSTER_SECRET_LABEL",
            "ARGOCD_CLUSTER_SECRET_VALUE",
            "ARGOCD_IN_CLUSTER_SERVER",
            "FLUX_HELM_RELEASE_KIND",
            "FLUX_HELM_REPOSITORY_KIND",
            "RELEASE_KEY_APPLICATION",
            "RELEASE_KEY_RELEASE",
            "RELEASE_KEY_NAMESPACE",
            "ARGOCD_VALUES_OBJECT_FIELD",
            "ARGOCD_VALUES_STRING_FIELD",
            "FLUX_VALUES_FIELD",
            "ARGOCD_KUSTOMIZE_PATCHES_FIELD",
            "RENDERER_HELM",
            "RENDERER_KUSTOMIZE",
            "KUSTOMIZATION_FILE_NAMES",
        ):
            with self.subTest(const=const):
                self.assertEqual(getattr(fw, const), getattr(collect, const))


class CandidatesCarryTheirDeclarationTest(unittest.TestCase):
    """The annotation has to reach the candidate, or the model never sees it.

    The index itself is `collect.py`'s, and tested there. What is new here is
    the trip to a *cost* candidate: the index is built in `collect_fleet`,
    handed across a thread pool to `collect_cluster`, and attached in the
    `emit` closure. A break at any of those three joints leaves the index
    correct and every candidate unannotated -- indistinguishable, from the
    model's side, from a fleet whose objects are genuinely undeclared, and the
    answer that sends a shrinkable request to `kind: manual`.
    """

    CLUSTER = {"name": "prod-usc1", "project": "acme", "location": "us-central1", "autopilot": False}
    DECLARED = "clusters/prod-usc1/workloads/data.yaml"

    def clone(self, tmp):
        """A GitOps tree declaring PersistentVolumeClaim/data, and nothing else.

        `scratch` is deliberately absent, so one run covers both arms: a change
        that annotates unconditionally fails here rather than passing a test
        that only ever looks at declared objects.
        """
        path = Path(tmp) / "infra"
        (path / "clusters/prod-usc1/workloads").mkdir(parents=True)
        (path / self.DECLARED).write_text(
            "apiVersion: v1\n"
            "kind: PersistentVolumeClaim\n"
            "metadata:\n"
            "  name: data\n"
            "  namespace: default\n"
        )
        return path

    def pvc(self, name):
        """Bound, unreferenced, 212 days old — one `unconsumed-pvc` candidate."""
        return obj(
            "PersistentVolumeClaim", name, ns="default",
            **{"status.phase": "Bound", "status.capacity": {"storage": "10Gi"}},
        )

    def fleet(self, tmp, *, workspace):
        """`collect_fleet` over one cluster holding two unconsumed PVCs."""
        def run(argv, **kwargs):
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                return run_of(0, json.dumps([{"name": "prod-usc1", "location": "us-central1", "status": "RUNNING"}]))
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(self.pvc("data"), self.pvc("scratch"))))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
            return fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW, workspace=workspace)

    def candidates(self, manifest):
        return [c for cluster in manifest["clusters"] for c in cluster.get("candidates") or []]

    def by_object(self, manifest, name):
        return [c for c in self.candidates(manifest) if c["object"] == f"PersistentVolumeClaim/{name}"]

    def test_a_declared_object_carries_its_path_and_directory(self):
        with TemporaryDirectory() as tmp:
            manifest = self.fleet(tmp, workspace=self.clone(tmp))

        declared = self.by_object(manifest, "data")
        undeclared = self.by_object(manifest, "scratch")
        # Guard the guard: two empty lists would satisfy every assertion below.
        self.assertTrue(declared, "no candidate for the declared object")
        self.assertTrue(undeclared, "no candidate for the undeclared object")

        for candidate in declared:
            self.assertEqual(
                candidate["declaration"],
                {"path": self.DECLARED, "directory": "clusters/prod-usc1/workloads"},
                candidate["check"],
            )
        # `scratch` is in the same dump, same cluster, same namespace, and
        # differs only in being absent from the clone.
        for candidate in undeclared:
            self.assertNotIn("declaration", candidate, candidate["check"])

    def test_the_path_is_relative_to_the_clone_not_the_filesystem(self):
        """What ships is what a PR branch has to check out. An absolute path
        leaks the agent's scratch directory into the ledger and names a file
        the GitOps repository does not contain."""
        with TemporaryDirectory() as tmp:
            manifest = self.fleet(tmp, workspace=self.clone(tmp))
            path = self.by_object(manifest, "data")[0]["declaration"]["path"]
            self.assertNotIn(tmp, path)
        self.assertFalse(Path(path).is_absolute())

    def test_without_a_workspace_nothing_is_annotated(self):
        """Omitting `--workspace` has to stay the no-op it was before this flag
        existed, on a run that actually produces candidates."""
        with TemporaryDirectory() as tmp:
            manifest = self.fleet(tmp, workspace=None)
        candidates = self.candidates(manifest)
        self.assertTrue(candidates, "the fixture produced no candidates to check")
        for candidate in candidates:
            self.assertNotIn("declaration", candidate, candidate["check"])

    def test_a_workspace_that_declares_nothing_annotates_nothing(self):
        """An empty clone is not an error and must not be a wrong answer: an
        absent annotation is not a claim that no declaration exists, so the
        SOP's grep fallback still holds."""
        with TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            manifest = self.fleet(tmp, workspace=empty)
        candidates = self.candidates(manifest)
        self.assertTrue(candidates, "the fixture produced no candidates to check")
        for candidate in candidates:
            self.assertNotIn("declaration", candidate, candidate["check"])

    def test_a_project_scoped_candidate_is_never_annotated(self):
        """A disk, a reserved address and a forwarding rule are GCP resources
        with no cluster tree and no namespace, so there is nothing for any
        index to key on, and annotating one would name a Kubernetes manifest
        as the place to delete a persistent disk. `_emit` itself cannot tell a
        disk from a Deployment, so what keeps them apart is the caller:
        `collect_project_compute`, the only emitter of the project-scoped
        checks, takes no index and hands `_emit` no cluster or index."""
        params = set(inspect.signature(fw.collect_project_compute).parameters)
        self.assertFalse(params & {"cluster", "declarations", "workspace", "reconcilers", "releases"}, params)
        hit = {"object": "Address/us-central1:idle-ip", "severity": "minor", "excerpt": "x"}
        facts = {"pv_handles": set(), "referenced_addresses": set(), "service_names": set()}
        with patch.object(fw, "check_idle_address", return_value=[hit]), \
                patch.object(fw, "_emit", wraps=fw._emit) as emit:
            target = fw.collect_project_compute("acme", True, facts, run=lambda argv, **kw: run_of(0, "[]"), now=NOW)
        self.assertEqual([c["object"] for c in target["candidates"]], [hit["object"]])
        self.assertTrue(emit.call_args_list)
        for call in emit.call_args_list:
            self.assertEqual(call.kwargs, {}, call)


class CandidatesCarryTheirReconcilerTest(unittest.TestCase):
    """The other annotation `_emit` attaches, over the same three joints.

    `reconciler_of` is tested in `test_collect.py`; what is new here is that a
    cost candidate carries what it returns. Without it a `manual` remediation on
    a reconciled object reads as a fix a human can just apply, and the next sync
    puts it back -- so a break here is invisible in exactly the way the
    declaration one is.
    """

    def pvc(self, name, **meta):
        return obj(
            "PersistentVolumeClaim", name, ns="default",
            **{"status.phase": "Bound", "status.capacity": {"storage": "10Gi"}, **meta},
        )

    def fleet(self, tmp, *objects):
        def run(argv, **kwargs):
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                return run_of(0, json.dumps([{"name": "prod-usc1", "location": "us-central1", "status": "RUNNING"}]))
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(*objects)))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
            return fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW, workspace=None)

    def by_object(self, manifest, name):
        return [
            c
            for cluster in manifest["clusters"]
            for c in cluster.get("candidates") or []
            if c["object"] == f"PersistentVolumeClaim/{name}"
        ]

    def test_a_reconciled_object_carries_the_phrase(self):
        with TemporaryDirectory() as tmp:
            manifest = self.fleet(
                tmp,
                self.pvc("data", **{"metadata.annotations": {"argocd.argoproj.io/tracking-id": "apps:/PersistentVolumeClaim:default/data"}}),
                self.pvc("scratch"),
            )
        reconciled = self.by_object(manifest, "data")
        plain = self.by_object(manifest, "scratch")
        # Guard the guard: two empty lists would satisfy every assertion below.
        self.assertTrue(reconciled, "no candidate for the reconciled object")
        self.assertTrue(plain, "no candidate for the unreconciled object")
        for candidate in reconciled:
            self.assertEqual(
                candidate["reconciler"], "the Argo CD Application `apps`", candidate["check"]
            )
        for candidate in plain:
            self.assertNotIn("reconciler", candidate, candidate["check"])

    def test_a_project_scoped_candidate_is_never_annotated(self):
        # Same argument as the declaration index: a persistent disk has no
        # Kubernetes controller holding its spec, and those checks call `_emit`
        # directly. Handed an index that would resolve it, the signature cannot.
        hit = {"object": "Disk/orphaned-pd", "severity": "major", "excerpt": "x"}
        self.assertNotIn("reconciler", fw._emit("unattached-disk", hit))
        index = {("", "Disk/orphaned-pd"): "the Helm release `x`"}
        self.assertNotIn("reconciler", fw._emit("unattached-disk", hit, reconcilers=index))


class ReleaseDeclarationsSurviveMalformedDocumentsTest(unittest.TestCase):
    """One malformed file in the clone crashed the run before the manifest
    printed; each is skipped and the well-formed release still indexes."""

    GOOD = (
        "kind: HelmRelease\nmetadata: {name: web, namespace: apps}\n"
        "spec: {chart: {spec: {chart: web-chart, version: 1.0.0, sourceRef: {name: charts}}}}\n"
    )
    # Each shape, and the release key it must not produce (None for a
    # document that declares no release of its own).
    MALFORMED = {
        "chart.spec scalar": ("kind: HelmRelease\nmetadata: {name: a, namespace: apps}\nspec: {chart: {spec: oops}}\n", "a"),
        "chart.spec list": ("kind: HelmRelease\nmetadata: {name: b, namespace: apps}\nspec: {chart: {spec: [x]}}\n", "b"),
        "chart list": ("kind: HelmRelease\nmetadata: {name: c, namespace: apps}\nspec: {chart: [x]}\n", "c"),
        "chart scalar": ("kind: HelmRelease\nmetadata: {name: d, namespace: apps}\nspec: {chart: oops}\n", "d"),
        "repository spec list": ("kind: HelmRepository\nmetadata: {name: charts, namespace: apps}\nspec: [x]\n", None),
        "secret labels list": ("kind: Secret\nmetadata: {name: s, labels: [x]}\nstringData: {server: https://x, name: y}\n", None),
    }

    def index_with(self, release_declarations, text):
        with TemporaryDirectory() as tmp:
            tree = Path(tmp) / "clusters" / "prod-usc1"
            tree.mkdir(parents=True)
            (tree / "good.yaml").write_text(self.GOOD)
            (tree / "bad.yaml").write_text(text)
            return release_declarations(Path(tmp))

    def assert_skips_every_malformed_document(self, release_declarations, key_prefix):
        for label, (text, release) in self.MALFORMED.items():
            with self.subTest(label):
                index = self.index_with(release_declarations, text)
                good = index[(*key_prefix, "web")]
                self.assertEqual(good["chart"], "web-chart")
                # Neither malformed source document resolves a repository.
                self.assertEqual(good["repo"], "")
                if release:
                    self.assertNotIn((*key_prefix, release), index)

    def test_each_malformed_document_is_skipped(self):
        self.assert_skips_every_malformed_document(fw.release_declarations, ("prod-usc1", fw.RELEASE_KEY_RELEASE, "apps"))

    def test_the_chart_ref_form_still_indexes_on_an_empty_chart(self):
        """No `chart` at all is `spec.chartRef`, not a malformed template."""
        text = "kind: HelmRelease\nmetadata: {name: e, namespace: apps}\nspec: {chartRef: {kind: OCIRepository, name: e}}\n"
        entry = self.index_with(fw.release_declarations, text)[("prod-usc1", fw.RELEASE_KEY_RELEASE, "apps", "e")]
        self.assertEqual((entry["chart"], entry["values_field"]), ("", "spec.values"))


class ClusterDumpKindsTest(unittest.TestCase):
    """The SOP tells the model which kinds the dump already holds so it does
    not read them again; that list has to be the one the collector runs."""

    SOP = Path(__file__).resolve().parents[3] / "governance" / "fleet_wide_cost_analysis_sop.md"

    def test_the_sop_quotes_the_kinds_the_collector_dumps(self):
        self.assertIn(f"`fleet_waste.py` dumps `{fw.CLUSTER_DUMP_KINDS}`", self.SOP.read_text())


class CandidatesCarryTheirReleaseDeclarationTest(unittest.TestCase):
    """The third annotation, over the same three joints, on the arm that
    matters most to this stream.

    A right-size is the fix a pull request carries best, and `resources` is the
    key nearly every chart publishes -- so a chart-rendered workload resolving
    to the file declaring its release is the difference between a diff and a
    paragraph. `release_declarations` is a copy of `collect.py`'s
    (`DeclarationIndexIsACopyTest`), tested on malformed documents in both
    files; what is new here is the trip from `collect_fleet` through the thread
    pool, into the per-cluster index `_releases_by_object` builds off the dump,
    and out through the `emit` closure.
    """

    APPLICATION = "argocd/apps/data.yaml"
    DECLARED = "clusters/prod-usc1/workloads/data.yaml"
    ENTRY = {
        "path": APPLICATION,
        "kind": "Application",
        "renderer": "helm",
        "chart": "data-chart",
        "repo": "https://charts.example.com",
        "version": "1.2.3",
        "values_field": "spec.source.helm.valuesObject",
    }

    def clone(self, tmp, *, declare_object=False):
        """A GitOps tree declaring the chart release, and optionally the
        object itself, so one fixture covers the precedence rule."""
        path = Path(tmp) / "infra"
        (path / "argocd/apps").mkdir(parents=True)
        (path / self.APPLICATION).write_text(
            "apiVersion: argoproj.io/v1alpha1\n"
            "kind: Application\n"
            "metadata:\n"
            "  name: data-app\n"
            "spec:\n"
            "  source:\n"
            "    repoURL: https://charts.example.com\n"
            "    chart: data-chart\n"
            "    targetRevision: 1.2.3\n"
            "    helm:\n"
            "      releaseName: data\n"
            "  destination:\n"
            "    name: prod-usc1\n"
            "    namespace: default\n"
        )
        if declare_object:
            (path / "clusters/prod-usc1/workloads").mkdir(parents=True)
            (path / self.DECLARED).write_text(
                "apiVersion: v1\n"
                "kind: PersistentVolumeClaim\n"
                "metadata:\n"
                "  name: data\n"
                "  namespace: default\n"
            )
        return path

    def pvc(self, name, **meta):
        """Bound, unreferenced — one `unconsumed-pvc` candidate."""
        return obj(
            "PersistentVolumeClaim", name, ns="default",
            **{"status.phase": "Bound", "status.capacity": {"storage": "10Gi"}, **meta},
        )

    def tracked(self, name):
        """The marker Argo CD leaves: it renders with `helm template`, so there
        is no `meta.helm.sh` pair to read."""
        return self.pvc(
            name,
            **{"metadata.annotations": {"argocd.argoproj.io/tracking-id": f"data-app:/PersistentVolumeClaim:default/{name}"}},
        )

    def released(self, name):
        """The marker `helm install` leaves, which Flux also produces."""
        return self.pvc(
            name,
            **{"metadata.annotations": {"meta.helm.sh/release-name": "data", "meta.helm.sh/release-namespace": "default"}},
        )

    def fleet(self, tmp, *objects, workspace):
        def run(argv, **kwargs):
            if argv[:4] == ["gcloud", "container", "clusters", "list"]:
                return run_of(0, json.dumps([{"name": "prod-usc1", "location": "us-central1", "status": "RUNNING"}]))
            if "get-credentials" in argv:
                return run_of(0)
            if argv[:2] == ["kubectl", "get"]:
                return run_of(0, json.dumps(dump_of(*objects)))
            if argv[:2] in (["gcloud", "compute"], ["gcloud", "artifacts"]):
                return run_of(0, "[]")
            return run_of(0, "")

        with patch.object(fw, "KUBECONFIG_DIR", Path(tmp)):
            return fw.collect_fleet("acme", run=run, session=usage_session(), now=NOW, workspace=workspace)

    def by_object(self, manifest, name):
        return [
            c
            for cluster in manifest["clusters"]
            for c in cluster.get("candidates") or []
            if c["object"] == f"PersistentVolumeClaim/{name}"
        ]

    def test_a_chart_rendered_object_carries_the_release_it_came_from(self):
        with TemporaryDirectory() as tmp:
            manifest = self.fleet(tmp, self.tracked("data"), self.pvc("scratch"), workspace=self.clone(tmp))
        rendered = self.by_object(manifest, "data")
        plain = self.by_object(manifest, "scratch")
        # Guard the guard: two empty lists would satisfy every assertion below.
        self.assertTrue(rendered, "no candidate for the chart-rendered object")
        self.assertTrue(plain, "no candidate for the unrendered object")
        for candidate in rendered:
            self.assertEqual(candidate["release_declaration"], self.ENTRY, candidate["check"])
        for candidate in plain:
            self.assertNotIn("release_declaration", candidate, candidate["check"])

    def test_the_helm_annotation_pair_resolves_to_the_same_file(self):
        """One Application is indexed under both key shapes, so a fleet where
        Argo drives Helm through a plugin -- or where Flux installed the same
        release -- resolves through the release coordinates too."""
        with TemporaryDirectory() as tmp:
            manifest = self.fleet(tmp, self.released("data"), workspace=self.clone(tmp))
        rendered = self.by_object(manifest, "data")
        self.assertTrue(rendered, "no candidate for the chart-rendered object")
        for candidate in rendered:
            self.assertEqual(candidate["release_declaration"], self.ENTRY, candidate["check"])

    def test_an_object_the_repo_declares_outright_is_not_given_a_values_override(self):
        """Precedence, and the reason for it: a workload with its own manifest
        is edited there. Offering a values override beside it would give one
        fix two files, and the values one would not even be applied on a
        cluster where the object is not actually chart-rendered."""
        with TemporaryDirectory() as tmp:
            manifest = self.fleet(tmp, self.tracked("data"), workspace=self.clone(tmp, declare_object=True))
        rendered = self.by_object(manifest, "data")
        self.assertTrue(rendered, "no candidate for the declared object")
        for candidate in rendered:
            self.assertEqual(candidate["declaration"]["path"], self.DECLARED, candidate["check"])
            self.assertNotIn("release_declaration", candidate, candidate["check"])

    def test_a_release_the_clone_does_not_declare_is_not_annotated(self):
        """`helm install` run by hand, or an ApplicationSet this deliberately
        does not index. Absent is the `manual` verdict that shipped before."""
        with TemporaryDirectory() as tmp:
            manifest = self.fleet(
                tmp,
                self.pvc("data", **{"metadata.annotations": {"argocd.argoproj.io/tracking-id": "other-app:/PersistentVolumeClaim:default/data"}}),
                workspace=self.clone(tmp),
            )
        rendered = self.by_object(manifest, "data")
        self.assertTrue(rendered, "no candidate for the object")
        for candidate in rendered:
            self.assertNotIn("release_declaration", candidate, candidate["check"])

    def test_without_a_workspace_nothing_is_annotated(self):
        with TemporaryDirectory() as tmp:
            manifest = self.fleet(tmp, self.tracked("data"), workspace=None)
        candidates = self.by_object(manifest, "data")
        self.assertTrue(candidates, "the fixture produced no candidates to check")
        for candidate in candidates:
            self.assertNotIn("release_declaration", candidate, candidate["check"])

    def test_a_project_scoped_candidate_is_never_annotated(self):
        # Same argument as the other two indexes: a persistent disk is not
        # rendered by a chart, and those checks call `_emit` directly.
        hit = {"object": "Disk/orphaned-pd", "severity": "major", "excerpt": "x"}
        self.assertNotIn("release_declaration", fw._emit("unattached-disk", hit))
        index = {("", "Disk/orphaned-pd"): dict(self.ENTRY)}
        self.assertNotIn("release_declaration", fw._emit("unattached-disk", hit, releases=index))


class RefusedProjectIdTest(unittest.TestCase):
    """Which project id a refusal names, read only from gcloud's phrasings."""

    def owner(self, stderr):
        def run(argv, **kwargs):
            raise AssertionError(argv)

        return fw.refusal_owner("acme", stderr, run=run)

    def test_english_words_after_project_are_not_project_ids(self):
        for stderr in (
            "ERROR: Kubernetes Engine API is not enabled on this project either.",
            "ERROR: the quota project should be set",
        ):
            with self.subTest(stderr=stderr):
                self.assertEqual(self.owner(stderr), (False, "the refusal names no project, so it cannot be tied to 'acme'"))

    def test_a_capitalised_project_keyword_names_its_id(self):
        owned, reason = self.owner("ERROR: Project acme-prod is not found")
        self.assertFalse(owned)
        self.assertIn("names another project ('acme-prod')", reason)

    def test_a_capitalised_project_keyword_naming_this_project_owns_it(self):
        """The ownership test read only a lowercase keyword, so this refusal
        was "names no project" although it names acme."""
        self.assertEqual(self.owner("ERROR: SERVICE_DISABLED: Project acme is not found"), (True, ""))

    def test_a_quoted_project_id_naming_this_project_owns_it(self):
        """`REFUSED_PROJECT_ID_RE` admits a quote or bracket before the id and
        the ownership test did not, so the id was subtracted as this
        project's and the refusal read as naming none."""
        def run(argv, **kwargs):
            raise AssertionError(argv)

        for stderr in (
            "ERROR: SERVICE_DISABLED: project 'acme-prod' is not found",
            'ERROR: SERVICE_DISABLED: project "acme-prod" is not found',
            "ERROR: SERVICE_DISABLED: project [acme-prod] is not found",
        ):
            with self.subTest(stderr=stderr):
                self.assertEqual(fw.refusal_owner("acme-prod", stderr, run=run), (True, ""))


if __name__ == "__main__":
    unittest.main()
