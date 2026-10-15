#!/usr/bin/env python3
"""Unit tests for readiness_rules/audit_log.py, the two paged Cloud Logging reads the
audit-log rules share, over `testdata/audit_log_sample.json`.

The sample is in the shape of the entries `gcloud logging read --format=json` returns for
GKE's Kubernetes audit log, built from the two captures in bench/upgrade-scenarios/evidence
(06/removed-api.txt: a `flowcontrol/v1beta3` FlowSchema writer stamped
`k8s.io/removed-release=1.32` on a 1.31 control plane; 09/final.txt: an Endpoints v1 writer
stamped `k8s.io/deprecated=true`), with kube-system's endpoint-controller, a stale kubectl,
a current kubectl and a client-go controller added beside them. No test runs gcloud: the
context carries a fake runner that answers the removed-release read and the
deprecated-or-kubectl read by their filters.
"""

import json
import os
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
import readiness_rules as shared  # noqa: E402
import upgrade_shape_tables as tables  # noqa: E402
from readiness_rules import audit_log  # noqa: E402
from unittest.mock import patch  # noqa: E402

SAMPLE_PATH = os.path.join(os.path.dirname(__file__), "testdata", "audit_log_sample.json")
TUNER = "system:serviceaccount:kubeagents-system:legacy-flowcontrol-tuner"
WRITER = "system:serviceaccount:seeded-deprecation:legacy-endpoints-writer"
STALE_KUBECTL = "system:serviceaccount:seeded-shapes:stale-kubectl-client"
ENDPOINT_CONTROLLER = "system:serviceaccount:kube-system:endpoint-controller"
# Two hours after the sample's last write.
AT = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
FAIL = "FAIL"
DEPRECATED_MARKER = 'k8s.io/deprecated"="true"'
PAGE_BOUND = 'timestamp<="'


def paged(filter_text):
    """Whether a filter carries a page bound: the window clause has one `timestamp<=`, a later page two."""
    return filter_text.count(PAGE_BOUND) > 1
TIMEOUT = 45


def sample_entries():
    with open(SAMPLE_PATH, encoding="utf-8") as f:
        return json.load(f)


def split_sample():
    entries = sample_entries()
    removed = [e for e in entries if audit_log.LABEL_REMOVED_RELEASE in e["labels"]]
    return removed, [e for e in entries if e not in removed]


def full_page(entry, count, start=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)):
    """`count` copies of `entry` with distinct insertIds and distinct, descending timestamps from `start`."""
    page = []
    for i in range(count):
        copy = json.loads(json.dumps(entry))
        copy["insertId"] = f"{copy.get('insertId', 'entry')}-{start:%H%M%S}-{i}"
        copy["timestamp"] = (start - timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        page.append(copy)
    return page


class FakeRunner:
    """Answers the two reads by filter: canned lists, or scripted pages per read. Takes
    the timeout the context passes, as the report's `run_cmd` does, and records it."""

    def __init__(self, removed=(), deprecated=(), rc=0, stderr="", stdout=None, pages=None):
        self.removed, self.deprecated = list(removed), list(deprecated)
        self.rc, self.stderr, self.stdout = rc, stderr, stdout
        self.pages = {k: list(v) for k, v in (pages or {}).items()}
        self.calls = []
        self.timeouts = []

    def __call__(self, cmd, timeout=None, env=None):
        self.calls.append(cmd)
        self.timeouts.append(timeout)
        filter_text = cmd[3]
        which = audit_log.READ_DEPRECATED if DEPRECATED_MARKER in filter_text else audit_log.READ_REMOVED
        if self.stdout is not None:
            return self.rc, self.stdout, self.stderr
        if self.rc != 0:
            return self.rc, "", self.stderr
        if which in self.pages:
            queue = self.pages[which]
            page = queue.pop(0) if queue else []
            if page == FAIL:
                return 1, "", "ERROR: (gcloud.logging.read) boom"
            return 0, json.dumps(page), ""
        if paged(filter_text):
            return 0, "[]", ""
        return 0, json.dumps(self.removed if which == audit_log.READ_REMOVED else self.deprecated), ""


def context_with(runner=None, at=AT, clock=None, timeout=TIMEOUT):
    context = {
        shared.CONTEXT_PROJECT: "example-project",
        shared.CONTEXT_LOCATION: "us-central1-a",
        shared.CONTEXT_CLUSTER_NAME: "upg-06",
        shared.CONTEXT_RUN_CMD: runner or FakeRunner(),
        shared.CONTEXT_AT: at,
        shared.CONTEXT_CACHE: {},
    }
    if clock is not None:
        context[shared.CONTEXT_CLOCK] = clock
    if timeout is not None:
        context[shared.CONTEXT_TIMEOUT_SECONDS] = timeout
    return context


def sample_context(at=AT):
    removed, deprecated = split_sample()
    return context_with(FakeRunner(removed, deprecated), at=at)


class AuditLogReadTest(unittest.TestCase):
    def test_filters_carry_the_population_the_window_and_the_provider_exclusions(self):
        removed = audit_log.build_filter(audit_log.READ_REMOVED, "seeded-a", "us-central1-a", "2026-09-22T16:00:00Z", "2026-09-29T16:00:00Z")
        deprecated = audit_log.build_filter(audit_log.READ_DEPRECATED, "seeded-a", "us-central1-a", "2026-09-22T16:00:00Z", "2026-09-29T16:00:00Z")
        for text in (removed, deprecated):
            # The activity log by name: GKE's opt-in Data Access log shares the resource type
            # and carries every read, which the docstring's "writes only" would otherwise not hold against.
            self.assertIn('resource.type="k8s_cluster" AND logName:"cloudaudit.googleapis.com%2Factivity" AND resource.labels.cluster_name="seeded-a" AND resource.labels.location="us-central1-a"', text)
            self.assertNotIn("data_access", text)
            self.assertIn('timestamp>="2026-09-22T16:00:00Z" AND timestamp<="2026-09-29T16:00:00Z"', text)
            self.assertIn('(protoPayload.authenticationInfo.principalEmail!~"^system:" OR protoPayload.authenticationInfo.principalEmail=~"^system:serviceaccount:")', text)
            # The namespaces come from the one table every rule reads, so the clause and
            # `caller_class` cannot disagree; the prefixes are the table's two.
            names = "|".join([re.escape(n) for n in sorted(tables.SYSTEM_NAMESPACES)] + [p + "[a-z0-9-]*" for p in tables.SYSTEM_NAMESPACE_PREFIXES])
            self.assertIn(f'principalEmail!~"^system:serviceaccount:({names}):"', text)
            self.assertIn("istio\\-system", text)
            self.assertIn("configconnector\\-operator\\-system", text)
            self.assertNotIn("gmp-[a-z0-9-]*", text)
            self.assertIn('principalEmail!~"@(gcp-sa-[a-z0-9-]+[.]iam|container-engine-robot[.]iam|cloudservices|system)[.]gserviceaccount[.]com$"', text)
            self.assertNotIn("\\.", text.replace("\\-", ""))
        self.assertIn(' AND labels."k8s.io/removed-release":* AND ', removed)
        self.assertNotIn("k8s.io/deprecated", removed)
        self.assertIn('((labels."k8s.io/deprecated"="true" AND NOT labels."k8s.io/removed-release":*) OR protoPayload.requestMetadata.callerSuppliedUserAgent:"kubectl/")', deprecated)
        cmd = audit_log.build_command("example-project", removed)
        self.assertEqual(cmd[:3], ["gcloud", "logging", "read"])
        self.assertEqual(cmd[4:], ["--project=example-project", "--limit=1000", audit_log.JSON_FORMAT_FLAG])
        # The page is projected to what caller_records reads, nothing less.
        self.assertTrue(audit_log.JSON_FORMAT_FLAG.startswith("--format=json(") and audit_log.JSON_FORMAT_FLAG.endswith(")"))
        for field in ("insertId", "timestamp", "labels", "protoPayload.authenticationInfo", "protoPayload.requestMetadata.callerSuppliedUserAgent", "protoPayload.resourceName", "protoPayload.methodName"):
            self.assertIn(field, audit_log.PROJECTED_FIELDS)
        self.assertFalse(any(flag.startswith("--freshness") or flag.startswith("--page-size") for flag in cmd))
        self.assertEqual((tables.AUDIT_LOG_WINDOW_DAYS, tables.AUDIT_LOG_PAGE_LIMIT, tables.AUDIT_LOG_MAX_PAGES, tables.AUDIT_LOG_READ_BUDGET_SECONDS), (7, 1000, 3, 60))

    def test_a_quote_in_a_name_cannot_close_the_filter(self):
        self.assertIn('cluster_name="a\\"b"', audit_log.build_filter(audit_log.READ_REMOVED, 'a"b', "loc", "s", "e"))

    def test_api_from_resource_name_and_from_method_name(self):
        self.assertEqual(audit_log.api_of("core/v1/namespaces/seeded-deprecation/endpoints/legacy-endpoints-lane", None), "core/v1 endpoints")
        self.assertEqual(audit_log.api_of("flowcontrol.apiserver.k8s.io/v1beta3/flowschemas/legacy-batch-lane", None), "flowcontrol.apiserver.k8s.io/v1beta3 flowschemas")
        self.assertEqual(audit_log.api_of("apps/v1/namespaces/shop/deployments/web/status", None), "apps/v1 deployments")
        self.assertEqual(audit_log.api_of("core/v1/nodes/n1", None), "core/v1 nodes")
        self.assertEqual(audit_log.api_of("core/v1/namespaces/foo", None), "core/v1 namespaces")
        # The Namespace's own subresources name the Namespace; a name-less create inside one
        # names the created kind, as the drift detector's parser reads the same paths.
        self.assertEqual(audit_log.api_of("core/v1/namespaces/stuck/finalize", None), "core/v1 namespaces")
        self.assertEqual(audit_log.api_of("core/v1/namespaces/payments/status", None), "core/v1 namespaces")
        self.assertEqual(audit_log.api_of("core/v1/namespaces/default/pods", None), "core/v1 pods")
        self.assertEqual(audit_log.api_of("core/v1/namespaces/status/pods", None), "core/v1 pods")
        self.assertEqual(audit_log.api_of("apps/v1/namespaces/shop/deployments/web/status", None), "apps/v1 deployments")
        self.assertEqual(audit_log.api_of(None, "io.k8s.core.v1.endpoints.patch"), "core/v1 endpoints")
        self.assertEqual(audit_log.api_of(None, "io.k8s.apiserver.flowcontrol.v1beta3.flowschemas.create"), "flowcontrol.apiserver/v1beta3 flowschemas")
        self.assertEqual(audit_log.api_of(None, ""), audit_log.UNKNOWN_API)

    def test_caller_class_places_the_operator_the_provider_and_the_rest(self):
        operator = (TUNER, WRITER, "operator@example.com", "deployer@my-project.iam.gserviceaccount.com", "system:serviceaccount:kubeagents-system:platform")
        provider = (
            ENDPOINT_CONTROLLER,
            "system:kube-controller-manager",
            "system:cloud-controller-manager",
            "system:l7-lb-controller",
            "system:node:gke-a-1",
            "system:anonymous",
            "system:serviceaccount:gke-managed-system:x",
            "system:serviceaccount:gmp-system:collector",
            "system:serviceaccount:gmp-public:x",
            "system:serviceaccount:istio-system:istiod",
            "system:serviceaccount:configconnector-operator-system:x",
            "system:serviceaccount:config-management-system:reconciler",
            "system:serviceaccount:gatekeeper-system:gatekeeper-admin",
            "service-123@container-engine-robot.iam.gserviceaccount.com",
            "service-123@gcp-sa-gkenode.iam.gserviceaccount.com",
            "123@cloudservices.gserviceaccount.com",
            "x@system.gserviceaccount.com",
        )
        unplaced = ("", None, "legacy-flowcontrol-tuner", "system:serviceaccount:", "system:serviceaccount:ns-only")
        for principal in operator:
            self.assertEqual(audit_log.caller_class(principal), audit_log.CLASS_OPERATOR, principal)
        for principal in provider:
            self.assertEqual(audit_log.caller_class(principal), audit_log.CLASS_PROVIDER, principal)
        for principal in unplaced:
            self.assertEqual(audit_log.caller_class(principal), audit_log.CLASS_UNPLACED, principal)

    def test_sample_groups_by_caller_and_api(self):
        callers = {(c["principal"], c["api"]): c for c in audit_log.caller_records(sample_entries())}
        tuner = callers[(TUNER, "flowcontrol.apiserver.k8s.io/v1beta3 flowschemas")]
        self.assertEqual(tuner["count"], 2)
        self.assertEqual(tuner["removed_release"], "1.32")
        self.assertTrue(tuner["deprecated"])
        self.assertEqual(tuner["user_agent"], "legacy-flowcontrol-tuner/0.3")
        self.assertEqual(tuner["first_seen"], "2026-09-29T13:54:19.378013Z")
        self.assertEqual(tuner["last_seen"], "2026-09-29T14:04:19.112Z")
        self.assertEqual(tuner["caller_class"], audit_log.CLASS_OPERATOR)
        writer = callers[(WRITER, "core/v1 endpoints")]
        self.assertEqual(writer["count"], 2)
        self.assertIsNone(writer["removed_release"])
        self.assertTrue(writer["deprecated"])
        self.assertEqual(callers[(ENDPOINT_CONTROLLER, "core/v1 endpoints")]["caller_class"], audit_log.CLASS_PROVIDER)
        self.assertTrue(callers[(STALE_KUBECTL, "core/v1 configmaps")]["kubectl"])
        self.assertFalse(callers[(STALE_KUBECTL, "core/v1 configmaps")]["deprecated"])

    def test_malformed_entries_are_counted_under_the_unknown_principal_and_agent(self):
        entries = [
            {"timestamp": "2026-09-29T12:00:00Z", "labels": {}, "protoPayload": {"authenticationInfo": "not-a-dict", "requestMetadata": ["x"], "resourceName": "core/v1/nodes/n1"}},
            {"timestamp": "2026-09-29T12:00:01Z", "labels": [], "protoPayload": {"authenticationInfo": {"principalEmail": {"nested": 1}}, "requestMetadata": {"callerSuppliedUserAgent": 7}, "methodName": "io.k8s.core.v1.nodes.patch"}},
            {"timestamp": "2026-09-29T12:00:02Z", "protoPayload": "nothing"},
        ]
        callers = audit_log.caller_records(entries)
        self.assertEqual([(c["principal"], c["user_agent"], c["api"], c["count"]) for c in callers], [(audit_log.UNKNOWN_PRINCIPAL, audit_log.UNKNOWN_USER_AGENT, audit_log.UNKNOWN_API, 1), (audit_log.UNKNOWN_PRINCIPAL, audit_log.UNKNOWN_USER_AGENT, "core/v1 nodes", 2)])
        self.assertTrue(all(c["caller_class"] == audit_log.CLASS_UNPLACED for c in callers))

    def test_a_read_that_raises_is_cached_as_its_error_not_as_a_clean_window(self):
        context = sample_context()
        with patch.object(audit_log, "caller_records", side_effect=ValueError("boom")):
            first = audit_log.read_removed(context)
        self.assertIn("the removed read raised ValueError('boom')", first["error"])
        self.assertEqual(first["callers"], [])
        # The next rule asking for the same read gets the error, not an empty, clean page.
        second = audit_log.read_removed(context)
        self.assertEqual(second, first)  # the same cached error, as the caller's own copy
        self.assertIsNotNone(second["error"])
        self.assertEqual(len(context["run_cmd"].calls), 1)
        # An exception whose repr raises is still the read's error, named by its type, never a
        # clean cached window.
        class Unprintable:
            def __repr__(self):
                raise RuntimeError("no repr")

        other = sample_context()
        with patch.object(audit_log, "caller_records", side_effect=ValueError(Unprintable())):
            result = audit_log.read_removed(other)
        self.assertEqual(result["error"], "the removed read raised ValueError; the pages read before it are not graded")
        self.assertEqual(result["callers"], [])
        # A `SystemExit` from under the read is the read's error too, never a clean cached
        # window and never the run's end; the interpreter's own interrupt passes through.
        exiting = sample_context()
        with patch.object(audit_log, "caller_records", side_effect=SystemExit(2)):
            result = audit_log.read_removed(exiting)
        self.assertEqual(result["error"], "the removed read raised SystemExit(2); the pages read before it are not graded")
        self.assertEqual(result["callers"], [])
        self.assertIsNotNone(audit_log.read_removed(exiting)["error"])
        with self.assertRaises(KeyboardInterrupt), patch.object(audit_log, "caller_records", side_effect=KeyboardInterrupt):
            audit_log.read_removed(sample_context())

    def test_a_page_the_proxy_cut_is_the_reads_error_naming_the_cap(self):
        removed, _ = split_sample()
        first = full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT)

        class CuttingRunner(FakeRunner):
            def __call__(self, cmd, timeout=None, env=None):
                rc, stdout, stderr = super().__call__(cmd, timeout, env)
                if paged(cmd[3]):
                    return 0, stdout[: len(stdout) // 2], "credential proxy output truncated\n"
                return rc, stdout, stderr

        runner = CuttingRunner(pages={audit_log.READ_REMOVED: [first, first]})
        result = audit_log.read_removed(context_with(runner))
        self.assertEqual((result["pages"], result["entries"], result["sampled"]), (2, 1000, False))
        self.assertEqual(result["error"], "`gcloud logging read` (removed read, page 2) output was cut at the credential proxy's output cap, so the page did not fit in one read")
        self.assertNotIn("not a JSON list", result["error"])
        self.assertEqual(result["callers"][0]["count"], 1000)
        # A first page cut: nothing graded, the cap named rather than the JSON.
        cut_first = audit_log.read_removed(context_with(FakeRunner(rc=0, stdout='[{"timestamp": "2026-', stderr="credential proxy output truncated")))
        self.assertIn("(removed read, page 1) output was cut at the credential proxy's output cap", cut_first["error"])
        self.assertEqual((cut_first["entries"], cut_first["callers"]), (0, []))

    def test_each_read_runs_once_per_context_and_is_cached(self):
        context = sample_context()
        first = audit_log.read_removed(context)
        again = audit_log.read_removed(context)
        self.assertEqual(first, again)
        self.assertIsNot(first, again)
        second = audit_log.read_deprecated(context)
        self.assertEqual(second, audit_log.read_deprecated(context))
        self.assertEqual(len(context["run_cmd"].calls), 2)
        # The caller's copy is its own: the cache is written by `read` alone.
        first["callers"].sort(key=lambda c: -c["count"])
        first["callers"].pop()
        self.assertEqual(audit_log.read_removed(context), again)
        self.assertEqual(len(context["run_cmd"].calls), 2)
        # A read name that is not one of the reads is refused before any command runs.
        with self.assertRaises(ValueError) as refused:
            audit_log.read(context, "deprecate")
        self.assertEqual(str(refused.exception), "audit_log.read('deprecate'): not one of the reads ('removed', 'deprecated')")
        self.assertEqual(len(context["run_cmd"].calls), 2)
        self.assertEqual((first["entries"], first["pages"], first["sampled"], first["error"]), (2, 1, False, None))
        self.assertEqual((second["entries"], second["pages"], second["sampled"], second["error"]), (6, 1, False, None))
        self.assertEqual((first["window_start"], first["window_end"]), ("2026-09-22T16:00:00Z", "2026-09-29T16:00:00Z"))
        self.assertIn('timestamp>="2026-09-22T16:00:00Z"', first["commands"][0])

    def test_a_stored_read_is_the_one_shape_the_probe_requires(self):
        # `read` and the probe share STORED_VALUE_CLASSES, a caller record and the probe share
        # CALLER_VALUE_CLASSES: what `read` stores, with callers and without, carries every
        # stored key in that order with a value of its class and passes the probe, and the
        # summary's keys are a subset of them. An entry of the summary's shape alone (which a
        # rule reading `callers` cannot use), a `callers` that is not a list, a count that is a
        # bool, and a caller record missing a key or holding a value of another class are not
        # read results, by the probe, so none of them is handed to a rule or summarised.
        with_callers = audit_log.read_removed(sample_context())
        without = audit_log.read_removed(context_with())
        self.assertGreater(len(with_callers["callers"]), 0)
        for result in (with_callers, without):
            self.assertEqual(tuple(result), audit_log.STORED_KEYS)
            self.assertIsNone(audit_log.cache_defect({shared.CONTEXT_CACHE: {audit_log.CACHE_KEY: {audit_log.READ_REMOVED: result}}}))
        self.assertEqual(tuple(with_callers["callers"][0]), audit_log.CALLER_RECORD_KEYS)
        self.assertTrue(set(audit_log.SUMMARY_KEYS) < set(audit_log.STORED_KEYS))
        caller = with_callers["callers"][0]
        summary_only = {key: with_callers[key] for key in (audit_log.READ_KEY,) + audit_log.SUMMARY_KEYS}
        defects = {
            "summary-only": (summary_only, "missing ['callers', 'window_days', 'page_limit']"),
            "callers-int": ({**with_callers, "callers": 3}, "'callers' is int, not list"),
            "entries-bool": ({**with_callers, "entries": True}, "'entries' is bool, not int"),
            "error-int": ({**with_callers, "error": 0}, "'error' is int, not str or NoneType"),
            "caller-int": ({**with_callers, "callers": [caller, 3]}, "`callers`[1] is int, not a dict"),
            "caller-short": ({**with_callers, "callers": [{"principal": caller["principal"]}]}, "`callers`[0] missing ['user_agent', 'api'"),
            "caller-count-str": ({**with_callers, "callers": [{**caller, "count": "9"}]}, "`callers`[0] 'count' is str, not int"),
        }
        for name, (entry, detail) in defects.items():
            context = {shared.CONTEXT_CACHE: {audit_log.CACHE_KEY: {audit_log.READ_REMOVED: entry}}}
            defect = audit_log.cache_defect(context)
            self.assertIsNotNone(defect, name)
            self.assertIn(detail, defect, name)
            self.assertEqual(audit_log.summaries(context), {}, name)

    def test_failed_timed_out_and_unparsable_reads_are_errors_on_one_line(self):
        two_lines = "ERROR: (gcloud.logging.read) PERMISSION_DENIED: Cloud Logging API has not been used\n- '@type': type.googleapis.com/google.rpc.ErrorInfo\n  reason: SERVICE_DISABLED"
        failed = audit_log.read_removed(context_with(FakeRunner(rc=1, stderr=two_lines)))
        self.assertIn("(removed read, page 1) failed (rc=1)", failed["error"])
        self.assertIn("PERMISSION_DENIED", failed["error"])
        self.assertIn("reason: SERVICE_DISABLED", failed["error"])
        self.assertNotIn("\n", failed["error"])
        timed_out = audit_log.read_deprecated(context_with(FakeRunner(rc=-1, stderr="timed out after 60 seconds")))
        self.assertIn("(deprecated read, page 1) failed (rc=-1): timed out after 60 seconds", timed_out["error"])
        garbled = audit_log.read_removed(context_with(FakeRunner(stdout="{not json")))
        self.assertIn("not a JSON list of entries", garbled["error"])
        not_a_list = audit_log.read_removed(context_with(FakeRunner(stdout=json.dumps({"entries": []}))))
        self.assertIn("not a JSON list of entries", not_a_list["error"])
        for result in (failed, timed_out, garbled, not_a_list):
            self.assertEqual(result["callers"], [])
            self.assertEqual(result["pages"], 1)

    def test_empty_output_is_a_clean_window(self):
        clean = audit_log.read_removed(context_with(FakeRunner(stdout="")))
        self.assertIsNone(clean["error"])
        self.assertEqual((clean["entries"], clean["pages"], clean["sampled"]), (0, 1, False))

    def test_pages_follow_the_oldest_timestamp_until_a_short_page(self):
        removed, _ = split_sample()
        first = full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT)
        second = full_page(removed[0], 400, start=datetime(2026, 9, 29, 11, 0, tzinfo=timezone.utc))
        runner = FakeRunner(pages={audit_log.READ_REMOVED: [first, second]})
        result = audit_log.read_removed(context_with(runner))
        self.assertEqual((result["entries"], result["pages"], result["sampled"], result["error"]), (1400, 2, False, None))
        self.assertFalse(paged(runner.calls[0][3]))
        self.assertTrue(paged(runner.calls[1][3]))
        self.assertIn(' AND timestamp<="' + first[-1]["timestamp"] + '"', runner.calls[1][3])
        self.assertIn('timestamp>="2026-09-22T16:00:00Z"', runner.calls[1][3])
        self.assertEqual(result["callers"][0]["count"], 1400)

    def test_the_page_bound_is_inclusive_and_the_entries_already_read_are_dropped(self):
        # The API server stamps several writes with one microsecond. A strict `timestamp<`
        # bound on the page's oldest timestamp dropped every entry at that timestamp that
        # did not fit the page, and the read still said complete. The bound is inclusive
        # and the entries already read are dropped by insertId: the first page ends on three
        # entries at T, the next page returns them again with two more at T and the older
        # ones, and every entry is counted once.
        removed, _ = split_sample()
        boundary = "2026-09-29T11:43:21.000000Z"
        first = full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT)
        for entry in first[-3:]:
            entry["timestamp"] = boundary
        second = full_page(removed[0], 5, start=datetime(2026, 9, 29, 11, 0, tzinfo=timezone.utc))
        for entry in second[:2]:
            entry["timestamp"] = boundary
        runner = FakeRunner(pages={audit_log.READ_REMOVED: [first, first[-3:] + second]})
        result = audit_log.read_removed(context_with(runner))
        self.assertIn(' AND timestamp<="' + boundary + '"', runner.calls[1][3])
        self.assertEqual((result["entries"], result["pages"], result["sampled"], result["error"]), (1005, 2, False, None))
        self.assertEqual(result["callers"][0]["count"], 1005)
        # A full page that adds nothing (more entries share the boundary timestamp than a
        # page holds) is sampled, not read again.
        same = full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT)
        for entry in same:
            entry["timestamp"] = boundary
        stuck = audit_log.read_removed(context_with(FakeRunner(pages={audit_log.READ_REMOVED: [same, same, same]})))
        self.assertEqual((stuck["entries"], stuck["pages"], stuck["sampled"], stuck["error"]), (1000, 2, True, None))
        # An entry with no insertId is never dropped.
        self.assertEqual(len(audit_log._unseen([{"timestamp": boundary}, {"timestamp": boundary}], set())), 2)

    def test_page_ceiling_and_time_budget_sample_rather_than_fail(self):
        removed, _ = split_sample()
        pages = [full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT, start=datetime(2026, 9, 29, 12 - i, 0, tzinfo=timezone.utc)) for i in range(4)]
        ceiling = audit_log.read_removed(context_with(FakeRunner(pages={audit_log.READ_REMOVED: pages})))
        self.assertEqual((ceiling["entries"], ceiling["pages"], ceiling["sampled"], ceiling["error"]), (3000, 3, True, None))
        self.assertEqual(audit_log.sampled_note(ceiling), "sampled 3000 entries over 3 page(s) of the removed read; more callers possible")
        ticks = iter([0, 61, 61, 61])
        budget = audit_log.read_removed(context_with(FakeRunner(pages={audit_log.READ_REMOVED: [full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT)]}), clock=lambda: next(ticks)))
        self.assertEqual((budget["entries"], budget["pages"], budget["sampled"]), (1000, 1, True))

    def test_paging_bound_and_seen_times_compare_parsed_timestamps(self):
        # Cloud Logging emits 0, 3, 6 or 9 fractional digits; as text "...19.112Z" sorts before
        # "...19Z" although it is later, so the bound and the seen times are chosen by parsed time.
        removed, _ = split_sample()
        page = full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT)
        page[0]["timestamp"] = "2026-09-29T12:00:19.112Z"
        page[1]["timestamp"] = "2026-09-29T12:00:19Z"
        for entry in page[2:]:
            entry["timestamp"] = "2026-09-29T12:00:20.500000Z"
        runner = FakeRunner(pages={audit_log.READ_REMOVED: [page, []]})
        result = audit_log.read_removed(context_with(runner))
        self.assertEqual((result["pages"], result["entries"], result["error"]), (2, 1000, None))
        self.assertIn(' AND timestamp<="2026-09-29T12:00:19Z"', runner.calls[1][3])
        self.assertNotIn("19.112Z", runner.calls[1][3])
        caller = result["callers"][0]
        self.assertEqual((caller["first_seen"], caller["last_seen"]), ("2026-09-29T12:00:19Z", "2026-09-29T12:00:20.500000Z"))
        self.assertEqual(audit_log.caller_records([{"timestamp": "junk", "protoPayload": {}}])[0]["first_seen"], None)

    def test_a_failed_later_page_keeps_the_pages_before_it(self):
        removed, _ = split_sample()
        runner = FakeRunner(pages={audit_log.READ_REMOVED: [full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT), FAIL]})
        result = audit_log.read_removed(context_with(runner))
        self.assertIn("(removed read, page 2) failed (rc=1): ERROR: (gcloud.logging.read) boom", result["error"])
        self.assertEqual((result["entries"], result["pages"]), (1000, 2))
        self.assertEqual(result["callers"][0]["principal"], TUNER)

    def test_no_runner_or_scope_is_an_error(self):
        self.assertEqual(audit_log.read_removed({"project": "p", "location": "l", "cluster_name": "c"})["error"], audit_log.NO_RUNNER_REASON)
        self.assertEqual(audit_log.read_removed({"run_cmd": lambda cmd: (0, "[]", ""), "project": "p"})["error"], audit_log.NO_SCOPE_REASON)

    def test_parse_timestamp(self):
        self.assertEqual(audit_log.parse_timestamp("2026-09-29T14:04:19.112Z"), datetime(2026, 9, 29, 14, 4, 19, 112000, tzinfo=timezone.utc))
        self.assertEqual(audit_log.parse_timestamp("2026-09-29T13:54:19.378013123Z"), datetime(2026, 9, 29, 13, 54, 19, 378013, tzinfo=timezone.utc))
        self.assertEqual(audit_log.parse_timestamp("2026-09-29T15:00:00+01:00"), datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc))
        for bad in ("2026-09-29", "yesterday", "", None):
            self.assertIsNone(audit_log.parse_timestamp(bad), bad)

    def test_the_context_timeout_is_the_per_call_cap(self):
        runner = FakeRunner()
        audit_log.read_removed(context_with(runner))
        self.assertEqual(runner.timeouts, [TIMEOUT])
        # Without one the runner's own default applies: the call carries no timeout.
        bare = FakeRunner()
        audit_log.read_removed(context_with(bare, timeout=None))
        self.assertEqual(bare.timeouts, [None])


if __name__ == "__main__":
    unittest.main()
