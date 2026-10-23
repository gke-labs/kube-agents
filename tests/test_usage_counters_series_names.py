"""The series the usage counters poller reads are the ones the four listeners export.

The operator sums the credential broker's tool-invocation counter over its
success and error outcomes (the commands it ran and the requests it rejected or
failed on before running), and the same counter over blocked, busy and abandoned
into toolExecutionsBlockedTotal, and the event watcher's injected-event counter, and
reads process_start_time_seconds from all four, and counts the watcher's per-cluster
up gauge into the two cluster gauges, and sums the broker's version-control
counter under one verb and status into remediationsProposedTotal, and its
merged-proposals counter whole into remediationsMergedTotal, and the chat plugin's inbound-message
counter over its platform label into chatMessagesInboundTotal, and into sessionsTotal the watcher's
triage-sessions counter, the chat plugin's sessions counter and the A2A gateway's. Each series name, the status label
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
_CHAT_METRICS_PY = _REPO_ROOT / "agents" / "chat" / "defaults" / "plugins" / "chat_metrics" / "metrics.py"
_MANIFESTS_GO = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "platformagent_manifests.go"
_A2A_METRICS_GO = _REPO_ROOT / "a2a" / "gateway" / "metrics.go"
_A2A_LIB_PAYLOAD_GO = _REPO_ROOT / "a2a" / "lib" / "payload.go"


def _go_const(path, name):
    # A bare constant or a typed one (`StateFailed TaskState = "failed"`).
    match = re.search(rf'^\s*(?:const\s+)?{name}(?:\s+\w+)?\s*=\s*"([^"]+)"', path.read_text(), re.M)
    assert match, f"{name} not found in {path}"
    return match.group(1)


def _py_const(name, path=None):
    source = (path or _BROKER_PY)
    match = re.search(rf'^{name}\s*=\s*"([^"]+)"', source.read_text(), re.M)
    assert match, f"{name} not found in {source}"
    return match.group(1)


def _watcher_family(field):
    """The Name the watcher registers for the CounterVec stored in field."""
    match = re.search(rf'{field}:\s*prometheus\.NewCounterVec\(prometheus\.CounterOpts\{{\s*Name:\s*"([^"]+)"', _WATCHER_METRICS_GO.read_text())
    assert match, f"{field} has no Name in {_WATCHER_METRICS_GO}"
    return match.group(1)


def _watcher_gauge_family(field):
    """The Name the watcher registers for the GaugeVec stored in field."""
    match = re.search(rf'{field}:\s*prometheus\.NewGaugeVec\(prometheus\.GaugeOpts\{{\s*Name:\s*"([^"]+)"', _WATCHER_METRICS_GO.read_text())
    assert match, f"{field} has no Name in {_WATCHER_METRICS_GO}"
    return match.group(1)


class UsageCountersSeriesNamesTest(unittest.TestCase):
    def test_the_brokers_vcs_counter_is_the_one_the_poller_sums_for_proposals(self):
        self.assertEqual(_py_const("VCS_REQUESTS_METRIC"), _go_const(_SCRAPE_GO, "vcsRequestsSeries"))
        self.assertEqual(_py_const("VCS_VERB_LABEL"), _go_const(_SCRAPE_GO, "vcsVerbLabel"))
        self.assertEqual(_py_const("TOOL_STATUS_SUCCESS"), _go_const(_SCRAPE_GO, "vcsStatusSuccess"))
        verb = _go_const(_SCRAPE_GO, "vcsProposalCreateVerb")
        vcs_broker = (_REPO_ROOT / "agents" / "platform" / "scripts" / "vcs_broker.py").read_text()
        match = re.search(r"^VCS_VERBS = frozenset\(\s*\{(.*?)\}\s*\)", vcs_broker, re.M | re.S)
        self.assertIsNotNone(match, "VCS_VERBS not found")
        self.assertIn(verb, set(re.findall(r'"([^"]+)"', match.group(1))))

    def test_the_brokers_merged_counter_is_the_one_the_poller_sums(self):
        self.assertEqual(_py_const("VCS_MERGED_METRIC"), _go_const(_SCRAPE_GO, "vcsMergedSeries"))

    def test_the_a2a_gateways_inbound_counter_is_the_chat_counters_other_family(self):
        """On a next-mode install the A2A gateway counts the chat messages it accepts;
        the poller folds that family into chatMessagesInboundTotal beside the plugin's,
        and the platforms the two share are spelled the same."""
        name = "_".join(_go_const(_A2A_METRICS_GO, c) for c in ("metricsNamespace", "metricsSubsystem", "messagesInboundName"))
        self.assertEqual(name, _go_const(_SCRAPE_GO, "a2aMessagesInboundSeries"))
        self.assertEqual("platform", _go_const(_A2A_METRICS_GO, "labelPlatform"))
        helpers = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "manifest_helpers.go"
        for gateway_const, operator_const in (("platformGoogleChat", "interfaceGoogleChat"), ("platformSlack", "interfaceSlack")):
            self.assertEqual(_go_const(_A2A_METRICS_GO, gateway_const), _go_const(helpers, operator_const))

    def test_the_sessions_families_are_the_ones_the_poller_sums(self):
        """sessionsTotal is read from three families: the watcher's triage sessions
        (opened and delivered, not its create attempts), the chat plugin's sessions
        counter on its listener, and the A2A gateway's on a next-mode install."""
        self.assertEqual(_watcher_family("triageSessions"), _go_const(_SCRAPE_GO, "triageSessionsSeries"))
        self.assertNotEqual(_watcher_family("sessionCreates"), _go_const(_SCRAPE_GO, "triageSessionsSeries"))
        self.assertEqual(_py_const("CHAT_SESSIONS_METRIC", _CHAT_METRICS_PY), _go_const(_SCRAPE_GO, "chatSessionsSeries"))
        name = "_".join(_go_const(_A2A_METRICS_GO, c) for c in ("metricsNamespace", "metricsSubsystem", "sessionsName"))
        self.assertEqual(name, _go_const(_SCRAPE_GO, "a2aSessionsSeries"))

    def test_the_chat_plugins_counter_is_the_one_the_poller_sums(self):
        plugin = _CHAT_METRICS_PY.read_text()
        self.assertEqual(_py_const("CHAT_INBOUND_METRIC", _CHAT_METRICS_PY), _go_const(_SCRAPE_GO, "chatInboundSeries"))
        self.assertEqual(_py_const("METRICS_PORT_ENV", _CHAT_METRICS_PY), _go_const(_MANIFESTS_GO, "chatMetricsPortEnv"))
        mapping = re.search(r"^HERMES_PLATFORM_LABELS = \{(.*?)\}", plugin, re.M | re.S).group(1)
        labels = set(re.findall(r':\s*"([a-z]+)"', mapping))
        # The chat platforms are the operator's own interface names (its
        # activeInterfaces vocabulary); `api` is Hermes' api_server, which the
        # operator has no name for.
        helpers = _REPO_ROOT / "k8s-operator" / "internal" / "controller" / "manifest_helpers.go"
        self.assertEqual({_go_const(helpers, "interfaceGoogleChat"), _go_const(helpers, "interfaceSlack"), _go_const(helpers, "interfaceTeams"), "api"}, labels)

    def test_the_watchers_cluster_gauge_is_the_one_the_poller_counts(self):
        self.assertEqual(_watcher_gauge_family("clusterUp"), _go_const(_SCRAPE_GO, "clusterUpSeries"))

    def test_the_brokers_counter_is_the_one_the_poller_sums(self):
        self.assertEqual(_py_const("TOOL_INVOCATIONS_METRIC"), _go_const(_SCRAPE_GO, "toolInvocationsSeries"))

    def test_the_watchers_injected_family_is_the_one_the_poller_sums(self):
        self.assertEqual(_watcher_family("eventsInjected"), _go_const(_SCRAPE_GO, "eventsInjectedSeries"))

    def test_every_listener_exports_the_start_time_gauge_the_poller_reads(self):
        name = _go_const(_SCRAPE_GO, "processStartTimeSeries")
        self.assertEqual(name, _py_const("PROCESS_START_TIME_METRIC"))
        self.assertEqual(name, _go_const(_WATCHER_METRICS_GO, "processStartTimeMetric"))
        self.assertEqual(name, _py_const("PROCESS_START_TIME_METRIC", _CHAT_METRICS_PY))
        self.assertEqual(name, _go_const(_A2A_METRICS_GO, "processStartTimeName"))

    def test_the_counted_outcomes_are_the_brokers_success_and_error(self):
        source = _SCRAPE_GO.read_text()
        match = re.search(r"toolInvocationsCountedStatuses\s*=\s*map\[string\]bool\{([^}]*)\}", source)
        self.assertIsNotNone(match, "toolInvocationsCountedStatuses not found")
        counted = set(re.findall(r'"([^"]+)":\s*true', match.group(1)))
        self.assertEqual({_py_const("TOOL_STATUS_SUCCESS"), _py_const("TOOL_STATUS_ERROR")}, counted)
        excluded = {_py_const(name) for name in ("TOOL_STATUS_BLOCKED", "TOOL_STATUS_BUSY", "TOOL_STATUS_ABANDONED")}
        self.assertFalse(counted & excluded)
        # The broker declares its whole vocabulary once; the summed and the
        # left-out statuses together must be exactly it, so a status the
        # broker adds and this test does not know fails here rather than
        # landing silently outside every status counter.
        match = re.search(r"^TOOL_STATUSES\s*=\s*frozenset\(\{([^}]*)\}\)", _BROKER_PY.read_text(), re.M)
        self.assertIsNotNone(match, "TOOL_STATUSES not found")
        vocabulary = {_py_const(name.strip()) for name in match.group(1).split(",") if name.strip()}
        self.assertEqual(vocabulary, counted | excluded)

    def test_the_brokers_failure_family_is_the_one_the_poller_sums_into_broker_failures(self):
        self.assertEqual(_py_const("BROKER_FAILURES_METRIC"), _go_const(_SCRAPE_GO, "brokerFailuresSeries"))

    def test_the_refused_outcomes_are_the_brokers_blocked_busy_and_abandoned(self):
        source = _SCRAPE_GO.read_text()
        match = re.search(r"toolInvocationsRefusedStatuses\s*=\s*map\[string\]bool\{([^}]*)\}", source)
        self.assertIsNotNone(match, "toolInvocationsRefusedStatuses not found")
        refused = set(re.findall(r'"([^"]+)":\s*true', match.group(1)))
        self.assertEqual(
            {_py_const("TOOL_STATUS_BLOCKED"), _py_const("TOOL_STATUS_BUSY"), _py_const("TOOL_STATUS_ABANDONED")},
            refused,
        )
        for excluded in ("TOOL_STATUS_SUCCESS", "TOOL_STATUS_ERROR"):
            self.assertNotIn(_py_const(excluded), refused)

    def test_the_two_status_sets_are_the_brokers_whole_vocabulary(self):
        """A status the broker adds and neither set names would land in neither counter."""
        source = _SCRAPE_GO.read_text()
        sets = set()
        for name in ("toolInvocationsCountedStatuses", "toolInvocationsRefusedStatuses"):
            match = re.search(rf"{name}\s*=\s*map\[string\]bool\{{([^}}]*)\}}", source)
            self.assertIsNotNone(match, f"{name} not found")
            sets |= set(re.findall(r'"([^"]+)":\s*true', match.group(1)))
        # Any module-level declaration shape: a bare name or an annotated one
        # (NAME: Final = ...), either quote style, so a constant written in a
        # shape this test did not foresee is still read rather than skipped.
        vocabulary = set(re.findall(r'^TOOL_STATUS_(?!LABEL\b)\w+(?:\s*:[^=\n]+)?\s*=\s*["\']([^"\']+)["\']', _BROKER_PY.read_text(), re.M))
        self.assertTrue(vocabulary, "no TOOL_STATUS_* constants found in the broker")
        self.assertEqual(vocabulary, sets)

    def test_the_chat_plugins_errored_family_and_the_a2a_terminals_are_the_errored_counters_families(self):
        self.assertEqual(_py_const("CHAT_TURNS_ERRORED_METRIC", _CHAT_METRICS_PY), _go_const(_SCRAPE_GO, "chatTurnsErroredSeries"))
        name = "_".join(_go_const(_A2A_METRICS_GO, c) for c in ("metricsNamespace", "metricsSubsystem", "taskTerminalsName"))
        self.assertEqual(name, _go_const(_SCRAPE_GO, "a2aTaskTerminalsSeries"))
        self.assertEqual(_go_const(_A2A_METRICS_GO, "labelState"), _go_const(_SCRAPE_GO, "a2aTerminalStateLabel"))

    def test_the_chat_plugins_negative_feedback_family_is_the_one_the_poller_sums_into_negative_feedback(self):
        self.assertEqual(_py_const("CHAT_FEEDBACK_NEGATIVE_METRIC", _CHAT_METRICS_PY), _go_const(_SCRAPE_GO, "chatFeedbackNegativeSeries"))

    def test_the_errored_states_the_poller_filters_on_are_the_a2a_libs_failed_and_rejected(self):
        """The state values are the A2A library's constants; the gateway emits them
        through its terminal list, and the operator's map is the second copy."""
        match = re.search(r"a2aErroredTerminalStates\s*=\s*map\[string\]bool\{([^}]*)\}", _SCRAPE_GO.read_text())
        self.assertIsNotNone(match, "a2aErroredTerminalStates not found")
        states = set(re.findall(r'"([^"]+)":\s*true', match.group(1)))
        self.assertEqual({_go_const(_A2A_LIB_PAYLOAD_GO, "StateFailed"), _go_const(_A2A_LIB_PAYLOAD_GO, "StateRejected")}, states)
        terminals = re.search(r"metricsTerminalStates\s*=\s*\[\]lib\.TaskState\{([^}]*)\}", _A2A_METRICS_GO.read_text())
        self.assertIsNotNone(terminals, "metricsTerminalStates not found")
        for name in ("StateFailed", "StateRejected"):
            self.assertIn(f"lib.{name}", terminals.group(1))

    def test_the_chat_plugins_gave_up_family_is_the_one_the_poller_sums_into_kanban_tasks_gave_up(self):
        self.assertEqual(_py_const("KANBAN_GAVE_UP_METRIC", _CHAT_METRICS_PY), _go_const(_SCRAPE_GO, "kanbanGaveUpSeries"))

    def test_the_status_label_key_the_broker_writes_is_the_one_the_poller_filters_on(self):
        self.assertEqual(_py_const("TOOL_STATUS_LABEL"), _go_const(_SCRAPE_GO, "toolInvocationsStatusLabel"))


if __name__ == "__main__":
    unittest.main()
