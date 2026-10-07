"""The series the usage counters poller reads are the ones the two listeners export.

The operator sums the credential broker's tool-invocation counter over its
success and error outcomes (the commands it ran and the requests it rejected or
failed on before running) and the event watcher's injected-event counter, and
reads process_start_time_seconds from both. Each series name, the status label
key the broker writes and the operator filters on, and each counted outcome
value is a constant on the producer's side and a second copy on the operator's,
and a rename on either side would freeze a status counter silently: the scrape
would still succeed, fold nothing, and read the sample as a fall. Nothing else
holds the copies in step, so this does.

Run: python3 -m unittest discover -s tests -p 'test_usage_counters_series_names.py' -v
"""

import pathlib
import re
import unittest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SCRAPE_GO = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "usage_counters_scrape.go"
_WATCHER_METRICS_GO = _REPO_ROOT / "k8s-operator" / "cmd" / "k8s-event-watcher" / "metrics.go"
_BROKER_PY = _REPO_ROOT / "agents" / "platform" / "scripts" / "credential_proxy.py"


def _go_const(path, name):
    match = re.search(rf'^\s*(?:const\s+)?{name}\s*=\s*"([^"]+)"', path.read_text(), re.M)
    assert match, f"{name} not found in {path}"
    return match.group(1)


def _py_const(name):
    match = re.search(rf'^{name}\s*=\s*"([^"]+)"', _BROKER_PY.read_text(), re.M)
    assert match, f"{name} not found in {_BROKER_PY}"
    return match.group(1)


def _watcher_family(field):
    """The Name the watcher registers for the CounterVec stored in field."""
    match = re.search(rf'{field}:\s*prometheus\.NewCounterVec\(prometheus\.CounterOpts\{{\s*Name:\s*"([^"]+)"', _WATCHER_METRICS_GO.read_text())
    assert match, f"{field} has no Name in {_WATCHER_METRICS_GO}"
    return match.group(1)


class UsageCountersSeriesNamesTest(unittest.TestCase):
    def test_the_brokers_counter_is_the_one_the_poller_sums(self):
        self.assertEqual(_py_const("TOOL_INVOCATIONS_METRIC"), _go_const(_SCRAPE_GO, "toolInvocationsSeries"))

    def test_the_watchers_injected_family_is_the_one_the_poller_sums(self):
        self.assertEqual(_watcher_family("eventsInjected"), _go_const(_SCRAPE_GO, "eventsInjectedSeries"))

    def test_both_listeners_export_the_start_time_gauge_the_poller_reads(self):
        name = _go_const(_SCRAPE_GO, "processStartTimeSeries")
        self.assertEqual(name, _py_const("PROCESS_START_TIME_METRIC"))
        self.assertEqual(name, _go_const(_WATCHER_METRICS_GO, "processStartTimeMetric"))

    def test_the_counted_outcomes_are_the_brokers_success_and_error(self):
        source = _SCRAPE_GO.read_text()
        match = re.search(r"toolInvocationsCountedStatuses\s*=\s*map\[string\]bool\{([^}]*)\}", source)
        self.assertIsNotNone(match, "toolInvocationsCountedStatuses not found")
        counted = set(re.findall(r'"([^"]+)":\s*true', match.group(1)))
        self.assertEqual({_py_const("TOOL_STATUS_SUCCESS"), _py_const("TOOL_STATUS_ERROR")}, counted)
        for excluded in ("TOOL_STATUS_BLOCKED", "TOOL_STATUS_BUSY", "TOOL_STATUS_ABANDONED"):
            self.assertNotIn(_py_const(excluded), counted)

    def test_the_status_label_key_the_broker_writes_is_the_one_the_poller_filters_on(self):
        self.assertEqual(_py_const("TOOL_STATUS_LABEL"), _go_const(_SCRAPE_GO, "toolInvocationsStatusLabel"))


if __name__ == "__main__":
    unittest.main()
