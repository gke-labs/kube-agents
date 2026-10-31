"""Unit tests for watcher_policy: what the broker relays for the gateway's watchers."""

import json
import unittest

import watcher_policy as w

SUB = "projects/kagents-dev/subscriptions/platform-agent-drift-audit-sub"
PUBSUB = w.PUBSUB_HOST
TARGET = "kagents-dev/us-east4/fleet-a/"


class GcpTest(unittest.TestCase):
    def test_the_drift_detectors_four_calls_on_its_subscription_are_allowed(self):
        cases = [
            ("GET", "v1/" + SUB, b""),
            ("POST", "v1/" + SUB + ":pull", json.dumps({"maxMessages": 10}).encode()),
            ("POST", "v1/" + SUB + ":acknowledge", json.dumps({"ackIds": ["a", "b"]}).encode()),
            ("POST", "v1/" + SUB + ":modifyAckDeadline", json.dumps({"ackIds": ["a"], "ackDeadlineSeconds": 60}).encode()),
        ]
        for method, path, body in cases:
            with self.subTest(path=path):
                self.assertTrue(w.evaluate_gcp(method, PUBSUB, path, body, SUB).allowed)

    def test_a_subscription_named_by_project_number_or_domain_project_is_allowed(self):
        for sub in ("projects/123456789/subscriptions/drift-sub",
                    "projects/example.com:my-proj/subscriptions/drift-sub"):
            with self.subTest(sub=sub):
                self.assertTrue(w.evaluate_gcp("POST", PUBSUB, "v1/" + sub + ":pull", b"{}", sub).allowed)

    def test_any_other_subscription_is_refused(self):
        for other in (
            "projects/kagents-dev/subscriptions/someone-elses-sub",
            "projects/another-project/subscriptions/platform-agent-drift-audit-sub",
        ):
            with self.subTest(other=other):
                decision = w.evaluate_gcp("POST", PUBSUB, "v1/" + other + ":pull", b"{}", SUB)
                self.assertEqual(decision.rule_id, w.RULE_GCP_SUBSCRIPTION)

    def test_pubsub_is_refused_outright_when_the_detector_is_off(self):
        decision = w.evaluate_gcp("POST", PUBSUB, "v1/" + SUB + ":pull", b"{}", "")
        self.assertEqual(decision.rule_id, w.RULE_GCP_SUBSCRIPTION)

    def test_other_pubsub_verbs_and_topics_are_refused(self):
        for method, path in (
            ("POST", "v1/" + SUB + ":seek"),
            ("DELETE", "v1/" + SUB),
            ("PATCH", "v1/" + SUB),
            ("POST", "v1/projects/kagents-dev/topics/t:publish"),
            ("GET", "v1/projects/kagents-dev/subscriptions"),
        ):
            with self.subTest(method=method, path=path):
                self.assertFalse(w.evaluate_gcp(method, PUBSUB, path, b"{}", SUB).allowed)

    def test_a_post_body_with_an_unexpected_key_or_type_is_refused(self):
        for verb, body in (
            (":pull", {"maxMessages": 10, "subscription": "x"}),
            (":pull", {"maxMessages": "10"}),
            (":pull", {"maxMessages": True}),
            (":acknowledge", {"ackIds": [1]}),
            (":modifyAckDeadline", {"ackIds": ["a"], "ackDeadlineSeconds": 60, "pushConfig": {}}),
        ):
            with self.subTest(verb=verb, body=body):
                decision = w.evaluate_gcp("POST", PUBSUB, "v1/" + SUB + verb, json.dumps(body).encode(), SUB)
                self.assertEqual(decision.rule_id, w.RULE_GCP_BODY)

    def test_gke_allows_one_cluster_read_and_nothing_else(self):
        self.assertTrue(
            w.evaluate_gcp("GET", w.GKE_HOST, "v1/projects/kagents-dev/locations/us-east4/clusters/fleet-a", b"", SUB).allowed
        )
        for method, path in (
            ("GET", "v1/projects/kagents-dev/locations/us-east4/clusters"),
            ("POST", "v1/projects/kagents-dev/locations/us-east4/clusters"),
            ("DELETE", "v1/projects/kagents-dev/locations/us-east4/clusters/fleet-a"),
            ("POST", "v1/projects/kagents-dev/locations/us-east4/clusters/fleet-a:setMasterAuth"),
        ):
            with self.subTest(method=method, path=path):
                self.assertFalse(w.evaluate_gcp(method, w.GKE_HOST, path, b"", SUB).allowed)

    def test_a_cluster_is_projected_to_the_fields_the_watchers_read(self):
        body = json.dumps({
            "name": "c", "location": "us-east4", "status": "RUNNING", "endpoint": "1.2.3.4",
            "controlPlaneEndpointsConfig": {"dnsEndpointConfig": {"endpoint": "c.example"}},
            "autopilot": {"enabled": True},
            "masterAuth": {"clusterCaCertificate": "CA", "clientCertificate": "CERT", "clientKey": "KEY",
                           "username": "admin", "password": "pw"},
            "nodePools": [{}], "legacyAbac": {"enabled": True},
        }).encode()
        projected = json.loads(w.project_cluster(body))
        self.assertEqual(projected["masterAuth"], {"clusterCaCertificate": "CA"})
        self.assertEqual(set(projected), set(w.CLUSTER_FIELDS) | {"masterAuth"})

    def test_an_answer_that_is_not_a_cluster_object_is_not_projected(self):
        for body in (b"not json", b"[]", b'"x"'):
            with self.subTest(body=body):
                self.assertIsNone(w.project_cluster(body))

    def test_a_cluster_named_by_project_number_is_read_and_targeted(self):
        # GKE takes a project number in a cluster's resource name, and a profile
        # may carry one; the Go side's identity check admits it.
        self.assertTrue(
            w.evaluate_gcp("GET", w.GKE_HOST, "v1/projects/123456789012/locations/us-east4/clusters/fleet-a", b"", SUB).allowed
        )
        self.assertTrue(w.evaluate_k8s("GET", "123456789012/us-east4/fleet-a/api/v1/events", {}).allowed)

    def test_no_other_host(self):
        self.assertEqual(
            w.evaluate_gcp("GET", "iamcredentials.googleapis.com", "v1/x", b"", SUB).rule_id, w.RULE_GCP_ROUTE
        )


class K8sTest(unittest.TestCase):
    def evaluate(self, path, method="GET", query=None):
        return w.evaluate_k8s(method, TARGET + path, query or {})

    def test_events_list_and_watch_are_allowed(self):
        self.assertTrue(self.evaluate("api/v1/events", query={"limit": ["500"]}).allowed)
        decision = self.evaluate("api/v1/events", query={"watch": ["true"], "resourceVersion": ["7"]})
        self.assertTrue(decision.allowed)
        self.assertTrue(w.is_watch({"watch": ["true"]}))

    def test_named_reads_of_the_listed_kinds_are_allowed(self):
        for path in (
            "api/v1/namespaces/default/configmaps/app",
            "api/v1/namespaces/team-a",
            "apis/apps/v1/namespaces/default/deployments/web",
            "apis/rbac.authorization.k8s.io/v1/clusterroles/admin",
            "apis/argoproj.io/v1alpha1/namespaces/argocd/applications/guestbook",
            "apis/configsync.gke.io/v1beta1/namespaces/config-management-system/rootsyncs/root-sync",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.evaluate(path).rule_id, "kubernetes.watcher.named-get")
        self.assertEqual(self.evaluate("api/v1/namespaces/default/secrets/db").rule_id, w.RULE_K8S_SECRETS)

    def test_every_table_entry_is_a_named_read(self):
        for group, resource in sorted(w.WATCHER_NAMED_READS):
            prefix = "api/v1" if not group else f"apis/{group}/v1"
            for path in (f"{prefix}/{resource}/x", f"{prefix}/namespaces/default/{resource}/x"):
                with self.subTest(path=path):
                    self.assertEqual(self.evaluate(path).rule_id, "kubernetes.watcher.named-get")

    def test_an_unlisted_kind_is_refused_by_the_kind_rule(self):
        for path in (
            "api/v1/nodes/node-1",
            "api/v1/namespaces/default/pods/web-0",
            "apis/coordination.k8s.io/v1/namespaces/kube-system/leases/kube-scheduler",
            "apis/cert-manager.io/v1/namespaces/default/certificates/tls",
            "apis/networking.gke.io/v1/namespaces/default/managedcertificates/c",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.evaluate(path).rule_id, w.RULE_K8S_KIND)

    def test_a_listed_plural_under_another_group_is_refused(self):
        # The group used to be captured and never checked.
        for path in (
            "apis/apps/v1/namespaces/default/configmaps/app",
            "apis/example.com/v1/namespaces/default/deployments/web",
            "apis/evil.argoproj.io/v1/namespaces/argocd/applications/a",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.evaluate(path).rule_id, w.RULE_K8S_KIND)

    def test_a_secret_keeps_its_own_rule_under_any_group(self):
        for path in ("api/v1/namespaces/default/secrets/db", "apis/example.com/v1/namespaces/default/secrets/db"):
            with self.subTest(path=path):
                self.assertEqual(self.evaluate(path).rule_id, w.RULE_K8S_SECRETS)

    def test_the_reconciler_kinds_are_the_six_the_agent_can_read(self):
        # The agent's minimal ClusterRole grants get/list/watch on these six
        # (gke-labs/kube-agents#2987, buildPlatformMinimalClusterRole). A change
        # here without the same change there, or the reverse, is the drift the
        # RECONCILER_NAMED_READS comment warns about.
        self.assertEqual(
            w.RECONCILER_NAMED_READS,
            {
                ("argoproj.io", "applications"),
                ("kustomize.toolkit.fluxcd.io", "kustomizations"),
                ("source.toolkit.fluxcd.io", "gitrepositories"),
                ("source.toolkit.fluxcd.io", "ocirepositories"),
                ("configsync.gke.io", "rootsyncs"),
                ("configsync.gke.io", "reposyncs"),
            },
        )
        self.assertLessEqual(w.RECONCILER_NAMED_READS, w.WATCHER_NAMED_READS)

    def test_a_malformed_table_is_refused_at_import(self):
        for table in (
            frozenset({("", "secrets")}),
            frozenset({("example.com", "secrets")}),
            frozenset({("", "watch")}),
            frozenset({("", "Deployments")}),
            frozenset({("Apps", "deployments")}),
            frozenset({("apps", "deployments/scale")}),
        ):
            with self.subTest(table=table):
                with self.assertRaises(ValueError):
                    w._validate_named_reads(table)

    def test_every_write_verb_is_refused(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for path in ("api/v1/events", "api/v1/namespaces/default/pods/x"):
                with self.subTest(method=method, path=path):
                    self.assertEqual(self.evaluate(path, method=method).rule_id, w.RULE_K8S_METHOD)

    def test_other_lists_watches_and_subresources_are_refused(self):
        for path in (
            "api/v1/pods",
            "api/v1/namespaces/default/pods",
            "api/v1/namespaces/default/events",
            "api/v1/namespaces/default/pods/x/exec",
            "api/v1/namespaces/default/pods/x/log",
            "api/v1/namespaces/default/serviceaccounts/x/token",
            "apis/apps/v1/deployments",
            "api/v1/secrets",
            "api",
            "version",
            "api/v1/namespaces/../secrets/x",
        ):
            with self.subTest(path=path):
                self.assertFalse(self.evaluate(path).allowed)

    def test_the_legacy_watch_and_proxy_paths_are_refused(self):
        # The API server still serves watch/<resource>; read as a named GET it
        # would be a cluster-wide watch of Secrets.
        for path in ("api/v1/watch/secrets", "api/v1/watch/events", "apis/apps/v1/watch/deployments",
                     "api/v1/namespaces/default/watch/pods", "api/v1/proxy/nodes",
                     "api/v1/namespaces/default/configmaps/..", "api/v1/nodes/."):
            with self.subTest(path=path):
                self.assertEqual(self.evaluate(path).rule_id, w.RULE_K8S_LEGACY)

    def test_rbac_names_with_colons_and_capitals_are_named_reads(self):
        for path in ("apis/rbac.authorization.k8s.io/v1/clusterroles/system:aggregate-to-edit",
                     "apis/rbac.authorization.k8s.io/v1/namespaces/team/rolebindings/MyBinding"):
            with self.subTest(path=path):
                self.assertEqual(self.evaluate(path).rule_id, "kubernetes.watcher.named-get")

    def test_watch_is_decided_like_the_api_server_decides_it(self):
        # apimachinery's Convert_Slice_string_To_bool: false only for "0" or a
        # case-insensitive "false"; every other first value streams, the empty
        # string included.
        for value in ("t", "1", "true", "yes", "on", "f", "F", "2", "tRuE", ""):
            with self.subTest(value=value):
                self.assertTrue(w.is_watch({"watch": [value, "0"]}))
        for value in ("0", "false", "FALSE", "False"):
            with self.subTest(value=value):
                self.assertFalse(w.is_watch({"watch": [value, "1"]}))
        self.assertFalse(w.is_watch({}))

    def test_the_watches_client_go_actually_sends_are_allowed(self):
        # Copied from the broker's access log on a live run of the k8s-event-watcher
        # (ka-next-4, 2026-10-14): the watch-list request, the paginated list
        # fallback, and its continuation.
        import urllib.parse
        for query in (
            "allowWatchBookmarks=true&resourceVersionMatch=NotOlderThan&sendInitialEvents=true"
            "&timeout=5m49s&timeoutSeconds=349&watch=true",
            "limit=500&resourceVersion=0",
            "continue=eyJ2IjoibWV0YS5rOHMuaW8vdjEifQ&limit=500",
            "allowWatchBookmarks=true&resourceVersion=12345&timeout=7m2s&timeoutSeconds=422&watch=true",
        ):
            with self.subTest(query=query):
                parsed = urllib.parse.parse_qs(query, keep_blank_values=True)
                self.assertTrue(self.evaluate("api/v1/events", query=parsed).allowed)

    def test_query_keys_are_held_to_what_the_clients_send(self):
        self.assertEqual(
            self.evaluate("api/v1/events", query={"watch": ["1"], "dryRun": ["All"]}).rule_id, w.RULE_K8S_QUERY
        )
        self.assertEqual(
            self.evaluate("api/v1/namespaces/default/configmaps/app", query={"watch": ["1"]}).rule_id, w.RULE_K8S_QUERY
        )

    def test_a_malformed_target_is_refused(self):
        for rest in ("kagents-dev/us-east4/Fleet_A/api/v1/events", "KAGENTS/us-east4/c/api/v1/events", "a/b/c"):
            with self.subTest(rest=rest):
                self.assertEqual(w.evaluate_k8s("GET", rest, {}).rule_id, w.RULE_K8S_TARGET)

    def test_no_refusal_repeats_caller_text(self):
        for decision in (
            w.evaluate_k8s("GET", TARGET + "api/v1/MARKER", {}),
            w.evaluate_k8s("MARKER", TARGET + "api/v1/events", {}),
            w.evaluate_gcp("GET", "MARKER.googleapis.com", "MARKER", b"", SUB),
        ):
            with self.subTest(rule=decision.rule_id):
                self.assertNotIn("MARKER", decision.message)
                self.assertRegex(decision.rule_id, r"^[a-z0-9]+(\.[a-z0-9-]+)+$")


ALERTS = "projects/kagents-dev/subscriptions/gke-stockout-alerts-sub"
OTHER = "projects/kagents-dev/subscriptions/second-route-sub"
PLATFORM_SET = frozenset({ALERTS, OTHER})


class PlatformPubsubTest(unittest.TestCase):
    """The platform adapter's relay: the same four calls, on a set of subscriptions."""

    def test_the_four_calls_are_allowed_on_every_listed_subscription(self):
        for sub in (ALERTS, OTHER):
            cases = [
                ("GET", "v1/" + sub, b"", "gcp.pubsub.subscription-get"),
                ("POST", "v1/" + sub + ":pull", json.dumps({"maxMessages": 10}).encode(), "gcp.pubsub.subscription-pull"),
                ("POST", "v1/" + sub + ":acknowledge", json.dumps({"ackIds": ["a"]}).encode(), "gcp.pubsub.subscription-acknowledge"),
                ("POST", "v1/" + sub + ":modifyAckDeadline", json.dumps({"ackIds": ["a"], "ackDeadlineSeconds": 0}).encode(),
                 "gcp.pubsub.subscription-modifyAckDeadline"),
            ]
            for method, path, body, rule in cases:
                with self.subTest(sub=sub, path=path):
                    decision = w.evaluate_pubsub(method, PUBSUB, path, body, PLATFORM_SET)
                    self.assertTrue(decision.allowed, decision)
                    self.assertEqual(decision.rule_id, rule)

    def test_a_subscription_outside_the_set_is_refused(self):
        for other in (SUB, "projects/another-project/subscriptions/gke-stockout-alerts-sub"):
            with self.subTest(other=other):
                decision = w.evaluate_pubsub("POST", PUBSUB, "v1/" + other + ":pull", b"{}", PLATFORM_SET)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.rule_id, w.RULE_PUBSUB_SUBSCRIPTION)

    def test_an_empty_set_refuses_every_pubsub_call(self):
        decision = w.evaluate_pubsub("POST", PUBSUB, "v1/" + ALERTS + ":pull", b"{}", frozenset())
        self.assertEqual(decision.rule_id, w.RULE_PUBSUB_SUBSCRIPTION)

    def test_the_gke_host_and_every_other_host_are_refused(self):
        for host in (w.GKE_HOST, "iamcredentials.googleapis.com", "container.googleapis.com"):
            with self.subTest(host=host):
                decision = w.evaluate_pubsub("GET", host, "v1/projects/p/locations/l/clusters/c", b"", PLATFORM_SET)
                self.assertEqual(decision.rule_id, w.RULE_PUBSUB_ROUTE)

    def test_other_verbs_topics_and_bodies_are_refused_as_for_the_watcher(self):
        for method, path, body, rule in (
            ("POST", "v1/" + ALERTS + ":seek", b"{}", w.RULE_PUBSUB_ROUTE),
            ("DELETE", "v1/" + ALERTS, b"", w.RULE_PUBSUB_METHOD),
            ("POST", "v1/projects/kagents-dev/topics/t:publish", b"{}", w.RULE_PUBSUB_ROUTE),
            ("POST", "v1/" + ALERTS, b"{}", w.RULE_PUBSUB_METHOD),
            ("POST", "v1/" + ALERTS + ":pull", json.dumps({"maxMessages": "10"}).encode(), w.RULE_PUBSUB_BODY),
            ("POST", "v1/" + ALERTS + ":acknowledge", json.dumps({"ackIds": [1]}).encode(), w.RULE_PUBSUB_BODY),
        ):
            with self.subTest(method=method, path=path):
                decision = w.evaluate_pubsub(method, PUBSUB, path, body, PLATFORM_SET)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.rule_id, rule)

    def test_the_platform_relay_names_itself_and_repeats_no_caller_text(self):
        decision = w.evaluate_pubsub("POST", PUBSUB, "v1/projects/MARKER/subscriptions/MARKER:pull", b"{}", PLATFORM_SET)
        self.assertFalse(decision.allowed)
        self.assertNotIn("MARKER", decision.message)
        self.assertNotIn("watcher", decision.message)

    def test_the_watcher_evaluator_is_unchanged(self):
        decision = w.evaluate_gcp("POST", PUBSUB, "v1/" + ALERTS + ":pull", b"{}", SUB)
        self.assertEqual(decision.rule_id, w.RULE_GCP_SUBSCRIPTION)
        self.assertEqual(decision.message, "The watcher relay reaches only the drift detector's configured subscription.")
        self.assertEqual(w.evaluate_gcp("GET", PUBSUB, "v1/" + SUB, b"", SUB).rule_id, "gcp.watcher.subscription-get")


if __name__ == "__main__":
    unittest.main()
